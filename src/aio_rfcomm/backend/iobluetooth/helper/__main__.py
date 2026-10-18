# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS helper process.

Run as ``python -m aio_rfcomm.backend.iobluetooth.helper`` from inside a generated
application bundle, with the control socket already on the descriptor named by
``AIO_RFCOMM_CONTROL_FD``. Not useful to start by hand.

The whole process exists to give IOBluetooth what it insists on and cannot be
given any other way: a main thread running a CFRunLoop, inside an application
bundle that declares why it wants Bluetooth. Everything else here is
plumbing -- requests in, bytes across a socket, and the reason a channel ended
sent before the socket carrying it closes.
"""

import asyncio
import itertools
import logging
import os
import socket
import sys
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

assert not TYPE_CHECKING or sys.platform == "darwin"

from rubicon.objc.runtime import load_library

# rubicon's event loop refers to NSEvent when it is imported, which lives in
# AppKit, so AppKit has to be loaded before the import rather than after it.
load_library("AppKit")

from rubicon.objc.eventloop import RubiconEventLoop

from aio_rfcomm.backend.iobluetooth.codes import CBManagerState
from aio_rfcomm.backend.iobluetooth.helper import native
from aio_rfcomm.backend.iobluetooth.protocol import Link
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    DeviceNotFoundError,
    RfcommError,
)

logger = logging.getLogger(__name__)

_handles = itertools.count(1)

# How much is taken from the socket at a time. The channel splits whatever it
# is given to fit its own maximum unit, so this is only a buffer size.
_PIECE_BYTES = 4096


class _ParentGone(Exception):
    """
    The library end of the control socket has closed.
    """


class _Session:
    """
    What one helper's requests share.
    """

    def __init__(self, link: Link, group: asyncio.TaskGroup) -> None:
        """
        Args:
            link: The control socket.
            group: Where work that outlives its request runs: a channel a
                service handed over carries on after the service is withdrawn.
        """
        self.link = link
        self.group = group
        # Set once IOBluetooth can be called without holding this thread
        # until a permission dialog is answered. See native.report_states.
        self.reached = asyncio.Event()
        self.serving: asyncio.Task[None] | None = None
        # Set once the serve request's task has finished, its service
        # withdrawn. Withdrawing waits on this rather than on the task.
        self.served = asyncio.Event()


async def _serve(sock: socket.socket) -> None:
    """
    Answer requests until the parent goes away.

    Args:
        sock: The control socket.
    """
    link = Link(sock)
    working: dict[int, asyncio.Task[None]] = {}
    try:
        try:
            async with asyncio.TaskGroup() as group:
                session = _Session(link, group)
                group.create_task(_report_states(link, session.reached))
                while True:
                    frame = await link.receive()
                    if frame is None:
                        # Raised rather than broken out of, so that the task
                        # group cancels everything still in flight. Waiting
                        # for it instead would leave this process alive for as
                        # long as a device that never answers keeps one
                        # request waiting -- which, since this socket is the
                        # only thing telling us the parent has gone, means
                        # forever.
                        raise _ParentGone
                    message, _fds = frame

                    if message.get("op") == "abandon":
                        giving_up = working.get(message["of"])
                        if giving_up is not None:
                            giving_up.cancel()
                        continue

                    number = message["id"]
                    working[number] = group.create_task(_answer(session, message))
                    working[number].add_done_callback(partial(_forget, working, number))
        except* _ParentGone:
            pass
    finally:
        link.close()


async def _report_states(link: Link, reached: asyncio.Event) -> None:
    """
    Tell the library the manager's state, now and on every change.

    Sent as numbers and read on the other side, which decides what each one
    means for whether Bluetooth is usable.

    Args:
        link: The control socket.
        reached: Set once IOBluetooth can be called from this thread.
    """

    def report(state: int, authorization: int) -> None:
        link.send({"event": "state", "state": state, "authorization": authorization})

    try:
        await native.report_states(report, reached)
    except Exception:
        # Losing the watch costs the reports, not the helper and its channels,
        # but the library waits for a first one: the power state stands in.
        logger.exception("could not watch the manager's state")
        reached.set()
        try:
            powered = native.read_power_state()
            report(
                CBManagerState.POWERED_ON if powered else CBManagerState.POWERED_OFF,
                native.read_authorization(),
            )
        except Exception:
            logger.exception("could not read the power state either")


def _forget(
    working: dict[int, asyncio.Task[None]], number: int, _task: asyncio.Task[None]
) -> None:
    """
    Drop a finished request from the registry of abandonable ones.

    Args:
        working: The registry.
        number: The request's number.
        _task: The finished task, which the callback is handed and ignores.
    """
    working.pop(number, None)


async def _answer(session: _Session, message: dict[str, Any]) -> None:
    """
    Carry out one request and reply to it.

    Args:
        session: What this helper's requests share.
        message: The request.
    """
    link = session.link
    number = message["id"]
    # Every request calls IOBluetooth, which could hold this thread, and with
    # it the end of file that says the library has gone.
    await session.reached.wait()
    try:
        match message["op"]:
            case "ready":
                # Deliberately touches the radio. macOS does not refuse
                # Bluetooth when a process starts, it refuses at the first
                # call that needs it -- by ending the process -- so a
                # handshake that made none would report a healthy helper that
                # is about to die.
                native.read_power_state()
                link.send({"id": number, "ok": None})

            case "devices":
                services: list[str] = message.get("service") or []
                wanted = [UUID(u) for u in services]
                link.send(
                    {
                        "id": number,
                        "ok": [
                            [d.address, d.name]
                            for d in native.list_known_devices(wanted or None)
                        ],
                    }
                )

            case "open":
                await _open(link, number, message)

            case "serve":
                if session.serving is not None:
                    # A second registration would replace the first in
                    # bluetoothd and leak its slot: see native.serve.
                    raise RfcommError("this helper is already serving")
                session.serving = asyncio.current_task()
                try:
                    await _publish(session, number, message)
                finally:
                    session.served.set()

            case "withdraw":
                if session.serving is not None:
                    session.serving.cancel()
                    await session.served.wait()
                link.send({"id": number, "ok": None})

            case unknown:
                raise RfcommError(f"the helper does not understand {unknown!r}")
    except RfcommError as error:
        reply = {"id": number, "error": type(error).__name__, "message": str(error)}
        if isinstance(error, BluetoothUnavailableError):
            reply["reason"] = error.reason.name
        if isinstance(error, DeviceNotFoundError):
            reply["address"] = error.address
        link.send(reply)
    except Exception as error:
        logger.exception("helper failed to answer %r", message.get("op"))
        link.send(
            {
                "id": number,
                "error": "ConnectionFailedError",
                "message": f"the macOS Bluetooth helper failed: {error}",
            }
        )


async def _open(link: Link, number: int, message: dict[str, Any]) -> None:
    """
    Open a channel and carry bytes over it until it ends.

    The reply hands the parent one end of a fresh socket pair, so that
    everything after this point is an ordinary stream on both sides.

    Args:
        link: The control socket.
        number: The request's number, for the reply.
        message: The request.
    """
    handle = next(_handles)
    try:
        await _carry_channel(link, handle, number, message)
    finally:
        # Only now is the device free: open_channel holds its claim until it
        # has finished closing, and refuses another channel to that device
        # meanwhile. The parent waits for this before its close returns.
        link.send({"event": "finished", "handle": handle})


async def _carry_channel(
    link: Link, handle: int, number: int, message: dict[str, Any]
) -> None:
    """
    Hold one channel open and carry bytes over it until it ends.

    Args:
        link: The control socket.
        handle: The handle this channel is known by.
        number: The request's number, for the reply.
        message: The request.
    """
    service = message.get("service")
    async with native.open_channel(
        message["address"],
        service=UUID(service) if service else None,
        channel=message.get("channel"),
    ) as channel:
        await _hand_over(
            link, handle, channel, partial(_reply_opened, link, number, handle)
        )


def _reply_opened(link: Link, number: int, handle: int, fd: int) -> None:
    link.send({"id": number, "ok": handle}, [fd])


async def _hand_over(
    link: Link,
    handle: int,
    channel: native.Channel,
    announce: Callable[[int], None],
) -> None:
    """
    Give the parent one end of a fresh socket pair and carry the channel's
    bytes over the other until it ends.

    Args:
        link: The control socket.
        handle: The handle this channel is known by.
        channel: The open channel.
        announce: Sends the parent the descriptor it is to have.
    """
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    try:
        with theirs:
            announce(theirs.fileno())
    except BaseException:
        ours.close()
        raise

    reader, writer = await asyncio.open_connection(sock=ours)
    try:
        if await _carry(channel, reader, writer):
            # Sent before the socket closes, so that the parent has the report
            # in hand by the time it reads end of file and comes looking.
            link.send({"event": "gone", "handle": handle})
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def _publish(session: _Session, number: int, message: dict[str, Any]) -> None:
    """
    Publish a service and hand the parent each channel opened to it, until
    withdrawn.

    Args:
        session: What this helper's requests share.
        number: The request's number, for the reply.
        message: The request.
    """
    async with native.serve(
        UUID(message["service"]),
        name=message["name"],
        channel=message.get("channel"),
        avoiding=message.get("avoiding") or (),
    ) as service:
        session.link.send({"id": number, "ok": service.channel})

        while True:
            try:
                channel = await service.accept()
            except Exception:
                # That channel is closed. The service carries on.
                logger.exception("could not take a channel a peer opened")
                continue

            session.group.create_task(_take(session.link, channel))


async def _take(link: Link, channel: native.Channel) -> None:
    """
    Offer the parent a channel a peer opened, and carry it until it ends.

    Args:
        link: The control socket.
        channel: The channel, already open.
    """
    handle = next(_handles)

    def announce(fd: int) -> None:
        peer = channel.peer()
        link.send(
            {
                "event": "request",
                "handle": handle,
                "address": peer.address,
                "name": peer.name,
            },
            [fd],
        )

    # Caught here, since this runs beside every other channel the helper
    # carries, and failing would end them all.
    try:
        try:
            await _hand_over(link, handle, channel, announce)
        finally:
            await channel.close()
    except Exception:
        logger.exception("could not carry a channel a peer opened")
    finally:
        link.send({"event": "finished", "handle": handle})


async def _carry(
    channel: native.Channel,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> bool:
    """
    Move bytes between the socket and the channel until one of them ends.

    Args:
        channel: The open RFCOMM channel.
        reader: Reads what the parent sent.
        writer: Writes what the device sent.

    Returns:
        Whether the channel ended. If not, the caller closed its end, and
        there is nobody left to tell.
    """
    over: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def settle(ended: bool) -> None:
        if not over.done():
            over.set_result(ended)

    async def to_device() -> None:
        try:
            while True:
                data = await reader.read(_PIECE_BYTES)
                if not data:
                    settle(False)
                    return
                await channel.send(data)
        except (OSError, native.MachError):
            settle(True)

    async def from_device() -> None:
        try:
            while True:
                writer.write(await channel.arrived.get())
                await writer.drain()
        except OSError:
            settle(True)

    async def watch_channel() -> None:
        await channel.wait_until_ended()

        # The last bytes can arrive with the end, while from_device still
        # waits to pass on what came before. They go out before the end does.
        try:
            while not channel.arrived.empty():
                writer.write(channel.arrived.get_nowait())

            await writer.drain()
        except OSError:
            pass

        settle(True)

    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(each())
            for each in (to_device, from_device, watch_channel)
        ]
        try:
            return await over
        finally:
            for task in tasks:
                task.cancel()


def main() -> int:
    """
    Run the helper.

    Returns:
        The process's exit status.
    """
    try:
        fd = int(os.environ["AIO_RFCOMM_CONTROL_FD"])
    except (KeyError, ValueError):
        print(
            "This is aio-rfcomm's macOS helper. It is started by the library "
            "and is not useful on its own.",
            file=sys.stderr,
        )
        return 2

    # The helper has no console of its own and its failures are invisible
    # from the library, so this is the only way to see inside it.
    #
    # REVISIT: these records go to whatever stderr this process inherited,
    # which bypasses the application's own logging entirely -- its handlers,
    # its level and its log files have no effect on another process, and in a
    # bundled application stderr may go nowhere at all. The variable is
    # therefore a debugging aid rather than an interface, and is deliberately
    # undocumented. Sending records back over the control link instead would
    # put them wherever the application already sends its logs, at the level
    # it already chose, and would remove the need for a variable at all.
    logging.basicConfig(
        level=os.environ.get("AIO_RFCOMM_HELPER_LOG", "WARNING").upper(),
        format="helper: %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    sock = socket.socket(fileno=fd)
    native.hear_open_results()
    native.keep_unread_bytes()
    # Every IOBluetooth call below runs on this thread, driven by the
    # CFRunLoop this loop is built on. Anywhere else, asynchronous calls
    # return success and then never complete. The one exception is the first
    # reach of the manager, which is synchronous: see native.report_states.
    loop = RubiconEventLoop()
    try:
        loop.run_until_complete(_serve(sock))
    finally:
        loop.close()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
