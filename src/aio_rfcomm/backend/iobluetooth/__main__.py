# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS helper process.

Run as ``python -m aio_rfcomm.backend.iobluetooth`` from inside a generated
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
from functools import partial
from typing import Any
from uuid import UUID

from rubicon.objc.runtime import load_library

# rubicon's event loop refers to NSEvent when it is imported, which lives in
# AppKit, so AppKit has to be loaded before the import rather than after it.
load_library("AppKit")

from rubicon.objc.eventloop import RubiconEventLoop

from aio_rfcomm.backend.iobluetooth import _native
from aio_rfcomm.backend.iobluetooth._protocol import Link
from aio_rfcomm.errors import CloseReason, RfcommError

logger = logging.getLogger(__name__)

_handles = itertools.count(1)

# How much is taken from the socket at a time. The channel splits whatever it
# is given to fit its own maximum unit, so this is only a buffer size.
_PIECE_BYTES = 4096


class _ParentGone(Exception):
    """
    The library end of the control socket has closed.
    """


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
                    working[number] = group.create_task(_answer(link, message))
                    working[number].add_done_callback(partial(_forget, working, number))
        except* _ParentGone:
            pass
    finally:
        link.close()


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


async def _answer(link: Link, message: dict[str, Any]) -> None:
    """
    Carry out one request and reply to it.

    Args:
        link: The control socket.
        message: The request.
    """
    number = message["id"]
    try:
        match message["op"]:
            case "ready":
                # Deliberately touches the radio. macOS does not refuse
                # Bluetooth when a process starts, it refuses at the first
                # call that needs it -- by ending the process -- so a
                # handshake that made none would report a healthy helper that
                # is about to die.
                _native.read_power_state()
                link.send({"id": number, "ok": None})

            case "adapters":
                link.send(
                    {
                        "id": number,
                        "ok": [
                            [a.id, a.address, a.name] for a in _native.list_adapters()
                        ],
                    }
                )

            case "powered":
                # Both, because macOS reports an unauthorised radio as off
                # and the caller cannot otherwise tell which it is looking at.
                link.send(
                    {
                        "id": number,
                        "ok": {
                            "powered": _native.read_power_state(),
                            "authorization": _native.read_authorization(),
                        },
                    }
                )

            case "devices":
                wanted = [UUID(u) for u in message.get("service") or []]
                link.send(
                    {
                        "id": number,
                        "ok": [
                            [d.address, d.name]
                            for d in _native.list_known_devices(wanted or None)
                        ],
                    }
                )

            case "open":
                await _open(link, number, message)

            case unknown:
                raise RfcommError(f"the helper does not understand {unknown!r}")
    except RfcommError as error:
        link.send(
            {
                "id": number,
                "error": type(error).__name__,
                "message": str(error),
            }
        )
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
    service = message.get("service")
    async with _native.open_channel(
        message["address"],
        service=UUID(service) if service else None,
        channel=message.get("channel"),
    ) as channel:
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with theirs:
            link.send({"id": number, "ok": handle}, [theirs.fileno()])

        reader, writer = await asyncio.open_connection(sock=ours)
        try:
            reason = await _carry(channel, reader, writer)
            if reason is not None:
                # Sent before the socket closes, so that the parent has the
                # reason in hand by the time it reads end of file and comes
                # looking for one.
                link.send({"event": "gone", "handle": handle, "reason": reason.name})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _carry(
    channel: _native.Channel,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> CloseReason | None:
    """
    Move bytes between the socket and the channel until one of them ends.

    Args:
        channel: The open RFCOMM channel.
        reader: Reads what the parent sent.
        writer: Writes what the device sent.

    Returns:
        Why the channel ended, or ``None`` if it did not -- the caller closed
        its end, and there is nobody left to tell.
    """
    over: asyncio.Future[CloseReason | None] = (
        asyncio.get_running_loop().create_future()
    )

    def settle(reason: CloseReason | None) -> None:
        if not over.done():
            over.set_result(reason)

    async def to_device() -> None:
        try:
            while True:
                data = await reader.read(_PIECE_BYTES)
                if not data:
                    settle(None)
                    return
                await channel.send(data)
        except (OSError, _native.MachError):
            settle(CloseReason.LINK_LOST)

    async def from_device() -> None:
        try:
            while True:
                writer.write(await channel.arrived.get())
                await writer.drain()
        except OSError:
            settle(CloseReason.LINK_LOST)

    async def watch_channel() -> None:
        settle(await channel.wait_until_ended())

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
    # Every IOBluetooth call below runs on this thread, driven by the
    # CFRunLoop this loop is built on. Anywhere else, asynchronous calls
    # return success and then never complete.
    loop = RubiconEventLoop()
    try:
        loop.run_until_complete(_serve(sock))
    finally:
        loop.close()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
