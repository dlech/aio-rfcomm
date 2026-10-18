# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS backend, driving IOBluetooth through a helper process.

IOBluetooth will only work from the main thread of a process running a
CFRunLoop, and macOS ends any process that touches Bluetooth without an
application bundle saying why it wants to. A library cannot impose either
condition on the program that imports it, so it brings its own process that
meets both. See ``aio_rfcomm.backend.iobluetooth.helper``.

Nothing on this side imports rubicon or the framework. What comes back from
the helper is an ordinary connected file descriptor, which is exactly what the
other backends produce, so everything after the connection is shared code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from collections.abc import AsyncGenerator, Callable, Collection
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    suppress,
)
from typing import TYPE_CHECKING
from uuid import UUID

# Type checking for Windows skips the rest of each module of this backend
# that starts this way. The helper's native layer is checked on macOS only.
# It does nothing at run time.
assert not TYPE_CHECKING or sys.platform != "win32"

from typing_extensions import override

from aio_rfcomm._teardown import unwrap_lone_error
from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth.codes import CBManagerAuthorization, CBManagerState
from aio_rfcomm.backend.iobluetooth.process import (
    Ending,
    Helper,
    HelperFailedError,
    Request,
    run_helper,
)
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    BackendRequest,
    BackendService,
)
from aio_rfcomm.backend.stream import StreamChannel, StreamProtocol, open_stream
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ConnectionFailedError,
    RfcommError,
    UnsupportedOperationError,
)

__all__ = ["IOBluetoothBackend"]

logger = logging.getLogger(__name__)


class IOBluetoothChannel(StreamChannel):
    """
    An RFCOMM channel over the socket the helper handed back.
    """

    def __init__(
        self,
        transport: asyncio.Transport,
        protocol: StreamProtocol,
        ending: Ending,
    ) -> None:
        """
        Args:
            transport: Carries the socket the helper handed back.
            protocol: What the transport reports to.
            ending: Where the helper's report of the channel's end arrives.
        """
        super().__init__(transport, protocol)
        self._ending = ending

    @override
    async def settle_end(self) -> None:
        """
        Wait for the helper's report of the end.

        The helper reports the end on the same link as Bluetooth's state, and
        before it closes the socket. So once the report is here, any state
        change that came before it is here too, and a radio switched off on
        this side is known before the end is put down to the connection.
        """
        await self._ending.wait()

    @override
    def abort(self) -> None:
        # The helper reports an end only when the device ended the channel.
        # Ended from this side, nothing would ever answer settle_end.
        self._ending.say()
        super().abort()


@asynccontextmanager
async def _carried(
    helper: Helper, handle: int, fd: int, ending: Ending
) -> AsyncGenerator[BackendChannel, None]:
    """
    Wrap the socket the helper handed back for a channel.

    Args:
        helper: The helper carrying the channel.
        handle: What the helper knows the channel by.
        fd: This end of the socket.
        ending: Where the helper's report of the channel's end arrives.

    Yields:
        The channel, closed on exit.
    """
    async with AsyncExitStack() as stack:
        stack.callback(helper.forget, handle)
        # Pushed before the socket, so that it runs after it: closing this end
        # is what tells the helper to let the channel go, and until it has, a
        # device is held that another channel may want.
        stack.push_async_callback(helper.wait_until_finished, handle)

        transport, protocol = await open_stream(socket.socket(fileno=fd))
        opened = IOBluetoothChannel(transport, protocol, ending)
        stack.push_async_callback(opened.close)

        yield opened


class _Served:
    """
    The helper a service runs in, which ends when the service has been
    withdrawn and every channel it handed over has closed.
    """

    # A helper per service because bluetoothd frees a registration only when
    # the process that made it ends, and a second from one process leaks the
    # first's slot in its RFCOMM stack until Bluetooth is next switched off
    # (see helper.native.serve).

    def __init__(self) -> None:
        self.helper: Helper | None = None
        self.task: asyncio.Task[None] | None = None
        """
        The task running the helper.
        """
        self._failure: BaseException | None = None
        self._started = asyncio.Event()
        self._holding = 0
        self._withdrawn = False
        self._done = asyncio.Event()

    async def run(
        self, start: Callable[[], AbstractAsyncContextManager[Helper]]
    ) -> None:
        """
        Run the helper until it is no longer needed.

        Args:
            start: Starts a helper.
        """
        try:
            async with start() as helper:
                # Asked before anything else, so a helper still waiting on a
                # permission dialog is canceled rather than asked to withdraw.
                await helper.ask("ready")
                self.helper = helper
                self._started.set()
                await self._done.wait()
        except Exception as error:
            # Belongs to the block that asked for the service, not to the
            # backend that happens to run this, but that block may be over.
            logger.debug("a service's helper failed", exc_info=True)
            self._failure = error
        finally:
            self._started.set()

    async def wait_started(self) -> Helper:
        """
        Returns:
            The running helper.

        Raises:
            Exception: Whatever starting it raised.
        """
        await self._started.wait()

        if self.helper is None:
            raise self._failure or HelperFailedError(
                "the macOS Bluetooth helper stopped"
            )
        return self.helper

    def hold(self) -> None:
        self._holding += 1

    def release(self) -> None:
        self._holding -= 1
        self._settle()

    async def withdraw(self) -> None:
        """
        Stop serving. The helper goes once the last channel has closed.
        """
        helper = self.helper

        try:
            if helper is None:
                # Still starting, and it may wait on a permission dialog.
                if self.task is not None:
                    self.task.cancel()
            elif not helper.stopped:
                await helper.ask("withdraw")
        except RfcommError:
            logger.debug("could not withdraw a service", exc_info=True)
        finally:
            if helper is not None:
                helper.stop_requests()
            self._withdrawn = True
            self._settle()

    def _settle(self) -> None:
        if self._withdrawn and not self._holding:
            self._done.set()


class IOBluetoothRequest(BackendRequest):
    """
    A channel a peer opened, which the helper is already carrying.
    """

    def __init__(self, helper: Helper, request: Request, served: _Served) -> None:
        self._helper = helper
        self._request = request
        self._served = served
        self._fd: int | None = request.fd

    @property
    @override
    def info(self) -> RfcommDeviceInfo:
        return self._request.info

    @override
    def open(self) -> AbstractAsyncContextManager[BackendChannel]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncGenerator[BackendChannel, None]:
        fd, self._fd = self._fd, None
        assert fd is not None, "a request is opened once"

        self._served.hold()
        try:
            async with _carried(
                self._helper, self._request.handle, fd, self._request.ending
            ) as opened:
                yield opened
        finally:
            self._served.release()

    @override
    def close(self) -> None:
        # Closing this end is what tells the helper to close the channel.
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)
            self._helper.forget(self._request.handle)

    @override
    async def decline(self) -> None:
        self.close()


class IOBluetoothService(BackendService):
    """
    A service published by a helper of its own.
    """

    def __init__(self, helper: Helper, number: int, served: _Served) -> None:
        self._helper = helper
        self._number = number
        self._served = served

    @property
    @override
    def channel(self) -> int:
        return self._number

    @override
    async def accept(self) -> BackendRequest:
        request = await self._helper.next_request()
        return IOBluetoothRequest(self._helper, request, self._served)


class IOBluetoothBluetooth(BackendBluetooth):
    """
    Bluetooth on this Mac, worked through whichever helper is running.
    """

    def __init__(
        self, serve_in: Callable[[_Served], asyncio.Task[None]] | None = None
    ) -> None:
        """
        Args:
            serve_in: Runs a service's helper somewhere that outlives the
                service, since its channels do. Without one, nothing is
                served.
        """
        self.helper: Helper | None = None
        """
        The running helper, or ``None`` between one and the next.
        """
        self._serve_in = serve_in
        # Every channel this program has served on, kept for good, since its
        # record outlives the service (see
        # helper.native.serving._choose_channel). A helper reads the others'
        # records for itself, but not one being published at the same moment,
        # nor any if that private path has gone.
        self._served_on: set[int] = set()
        self._choosing = asyncio.Lock()

    def _current(self) -> Helper:
        """
        Returns:
            The running helper.

        Raises:
            BluetoothUnavailableError: There is none, because the system's
                Bluetooth service is restarting.
        """
        if self.helper is None:
            raise BluetoothUnavailableError(Availability.SERVICE_UNAVAILABLE)
        return self.helper

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        rows, _fds = await self._current().ask("devices", service=_wanted(service))
        return [RfcommDeviceInfo(address, name) for address, name in rows]

    @override
    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, service=str(service))

    @override
    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, channel=channel)

    @override
    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[BackendService]:
        return self._serve(service, name, channel)

    @asynccontextmanager
    async def _serve(
        self, service: UUID, name: str, channel: int | None
    ) -> AsyncGenerator[BackendService, None]:
        if self._serve_in is None:
            raise UnsupportedOperationError("this Bluetooth object cannot serve")

        served = _Served()
        served.task = self._serve_in(served)

        try:
            helper = await served.wait_started()
            # One at a time, so that two services starting together cannot
            # both choose the channel neither has published on yet.
            async with self._choosing:
                number, _fds = await helper.ask(
                    "serve",
                    service=str(service),
                    name=name,
                    channel=channel,
                    avoiding=sorted(self._served_on),
                )
                self._served_on.add(number)
            yield IOBluetoothService(helper, number, served)
        finally:
            await served.withdraw()

    @asynccontextmanager
    async def _connect(
        self,
        device: RfcommDeviceInfo,
        *,
        service: str | None = None,
        channel: int | None = None,
    ) -> AsyncGenerator[BackendChannel, None]:
        """
        Ask the helper for a channel and wrap what comes back.

        Args:
            device: The device to reach.
            service: The service UUID to look up, if connecting by service.
            channel: The channel number, if connecting by number.

        Yields:
            The open channel, closed on exit.
        """
        # The helper that opened a channel is the one it belongs to, even once
        # another has replaced it.
        helper = self._current()
        handle, fds = await helper.ask(
            "open", address=device.address, service=service, channel=channel
        )
        if len(fds) != 1:
            for fd in fds:
                os.close(fd)
            raise ConnectionFailedError(
                "the macOS Bluetooth helper opened a channel but did not "
                "hand back a socket for it"
            )

        async with _carried(helper, handle, fds[0], helper.expect(handle)) as opened:
            yield opened


class IOBluetoothBackend(BackendProvider):
    """
    The macOS implementation.
    """

    def __init__(
        self,
        start: Callable[[], AbstractAsyncContextManager[Helper]] = run_helper,
    ) -> None:
        """
        Args:
            start: Starts a helper and keeps it running for a block. Only a
                test has reason to pass anything else.
        """
        self._start = start

    @override
    def open(
        self, state: AvailabilityState
    ) -> AbstractAsyncContextManager[BackendBluetooth]:
        return self._open(state)

    @asynccontextmanager
    async def _open(
        self, state: AvailabilityState
    ) -> AsyncGenerator[BackendBluetooth, None]:
        """
        Keep a helper running for as long as the block lasts.

        The helper lives in a task of this block's own, because it may have to
        be replaced: after bluetoothd restarts, every device object in it
        stays bound to the old daemon for the life of that process, and every
        open is dropped. Only a new helper recovers.

        Args:
            state: Where to report availability.

        Yields:
            Bluetooth, worked through whichever helper is running.
        """
        with unwrap_lone_error():
            async with asyncio.TaskGroup() as group:
                services: set[asyncio.Task[None]] = set()

                def serve_in(served: _Served) -> asyncio.Task[None]:
                    task = group.create_task(
                        served.run(self._start), name="aio-rfcomm service"
                    )
                    services.add(task)
                    task.add_done_callback(services.discard)
                    return task

                bluetooth = IOBluetoothBluetooth(serve_in)
                supervising = group.create_task(
                    self._supervise(bluetooth, state), name="aio-rfcomm helper"
                )
                try:
                    yield bluetooth
                finally:
                    supervising.cancel()
                    # Anything still served goes with Bluetooth.
                    for task in services:
                        task.cancel()

    async def _supervise(
        self, bluetooth: IOBluetoothBluetooth, state: AvailabilityState
    ) -> None:
        """
        Run a helper, report what it says, and replace it when it is finished.

        A helper is finished when the manager says it is resetting, which it
        does only when its connection to bluetoothd has gone, or when the
        helper itself stops. Either way the system's Bluetooth service is
        unavailable until a new helper says otherwise.

        Args:
            bluetooth: Where the running helper is kept.
            state: Where to report availability.

        Raises:
            HelperFailedError: The first helper stopped before it said
                anything. A later failure is reported as unavailable instead,
                since the caller has long since been told about Bluetooth.
        """
        replacing = False

        while True:
            ready = False
            try:
                async with self._start() as helper:
                    bluetooth.helper = helper

                    # A helper that stops takes its channels with it, and they
                    # have to hear why, not that their link dropped, so this
                    # is said before they are told anything.
                    def stopped(error: RfcommError) -> None:
                        match error:
                            case BluetoothUnavailableError(reason=reason):
                                state.set(reason, str(error))
                            case _ if state.is_known:
                                state.set(
                                    Availability.SERVICE_UNAVAILABLE,
                                    "the macOS Bluetooth helper stopped",
                                )
                            case _:
                                # Nothing has been said yet. The failure is
                                # raised instead.
                                pass

                    async def confirm() -> None:
                        # The helper records the answer itself, the moment it
                        # arrives, and a failure is the relay's to report.
                        with suppress(RfcommError):
                            await helper.ask("ready")

                    helper.on_stop = stopped
                    if helper.stopped:
                        # It stopped before there was anyone to hear it.
                        stopped(HelperFailedError("the macOS Bluetooth helper stopped"))
                    try:
                        # The helper answers "ready" once it has touched the
                        # radio and lived. Until then it can be held for as
                        # long as a permission dialog is open, which is
                        # forever on a machine nobody is at. What it said
                        # before that is reported meanwhile.
                        with unwrap_lone_error():
                            async with asyncio.TaskGroup() as group:
                                confirming = group.create_task(confirm())
                                try:
                                    await _relay(helper, state)
                                finally:
                                    confirming.cancel()
                    finally:
                        ready = helper.ready
                        helper.on_stop = None
                        bluetooth.helper = None
            except BluetoothUnavailableError as refused:
                # macOS refused Bluetooth to the helper and ended it, and would
                # refuse a new one the same way.
                state.set(refused.reason, str(refused))
                return
            except (HelperFailedError, OSError) as failed:
                if not ready:
                    if not state.is_known:
                        raise
                    # A helper that will not start would not start next time
                    # either. Trying again would only spin.
                    doing = "restarted" if replacing else "started"
                    state.set(
                        Availability.SERVICE_UNAVAILABLE,
                        f"the macOS Bluetooth helper could not be {doing}: {failed}",
                    )
                    return
                logger.info("the macOS Bluetooth helper stopped: %s", failed)
            else:
                logger.info("the Bluetooth service restarted. Starting a new helper")

            replacing = True


async def _relay(helper: Helper, state: AvailabilityState) -> None:
    """
    Report each state the helper sends, until the service resets.

    Args:
        helper: The running helper.
        state: Where to report availability.

    Raises:
        HelperFailedError: The helper stopped.
    """
    while True:
        manager, authorization = await helper.next_state()

        match _availability(manager, authorization):
            case None:
                pass
            case Availability.SERVICE_UNAVAILABLE:
                # This helper's channels are dead and will never be told, and
                # its device objects are bound to the old daemon. Said before
                # the helper goes, so its channels end for this reason.
                state.set(Availability.SERVICE_UNAVAILABLE)
                return
            case Availability.SWITCHED_OFF:
                state.set(
                    Availability.SWITCHED_OFF,
                    "this Mac's Bluetooth is switched off. Turn it on",
                )
            case Availability.NOT_ALLOWED:
                state.set(Availability.NOT_ALLOWED, _not_allowed(authorization))
            case Availability.AWAITING_PERMISSION:
                state.set(Availability.AWAITING_PERMISSION, _AWAITING_PERMISSION)
            case available:
                state.set(available)


def _availability(manager: int, authorization: int) -> Availability | None:
    """
    Say what a ``CBManagerState`` means for whether Bluetooth is usable.

    Args:
        manager: The manager's state.
        authorization: The ``CBManagerAuthorization`` at the time, since macOS
            reports a radio this program may not use as switched off or
            unsupported.

    Returns:
        The availability, or ``None`` for a state that is not an answer.
    """
    match manager:
        case CBManagerState.POWERED_ON:
            return Availability.USABLE
        case CBManagerState.RESETTING:
            # Set only when the connection to bluetoothd is lost, never on an
            # ordinary toggle (follow-ups §13.2).
            return Availability.SERVICE_UNAVAILABLE
        case _ if authorization in (
            CBManagerAuthorization.DENIED,
            CBManagerAuthorization.RESTRICTED,
        ):
            # Whatever the state says: after the user refused the dialog on
            # macOS 26.6.2, the manager said unsupported.
            return Availability.NOT_ALLOWED
        case (
            CBManagerState.UNKNOWN
            | CBManagerState.POWERED_OFF
            | CBManagerState.UNAUTHORIZED
        ) if authorization == CBManagerAuthorization.NOT_DETERMINED:
            # The helper says this before it reaches the manager, which macOS
            # holds up for as long as its permission dialog is open. A helper
            # going by a program macOS cannot ask reads it too, and macOS ends
            # it without asking: on macOS 26.6.2, 15 to 44 ms later. That is
            # the programmer's to fix, and the error then says how.
            return Availability.AWAITING_PERMISSION
        case CBManagerState.POWERED_OFF if (
            authorization == CBManagerAuthorization.ALLOWED_ALWAYS
        ):
            return Availability.SWITCHED_OFF
        case CBManagerState.POWERED_OFF | CBManagerState.UNAUTHORIZED:
            return Availability.NOT_ALLOWED
        case CBManagerState.UNSUPPORTED:
            return Availability.UNSUPPORTED
        case CBManagerState.UNKNOWN:
            return None
        case _:
            logger.warning("CoreBluetooth reported state %d, unknown here", manager)
            return None


_AWAITING_PERMISSION = (
    "macOS is asking whether this program may use Bluetooth. Answer the "
    "permission dialog."
)


def _not_allowed(authorization: int) -> str:
    """
    Say why this program may not use Bluetooth, which is not always the same.

    Args:
        authorization: A ``CBManagerAuthorization`` from the helper.

    Returns:
        What to tell the user.
    """
    match authorization:
        case CBManagerAuthorization.RESTRICTED:
            return (
                "Bluetooth is not allowed on this Mac, by a profile or parental "
                "controls rather than by a choice this program can undo."
            )
        case _:
            return (
                "Bluetooth was refused for this program. Allow it under "
                "Privacy & Security > Bluetooth in System Settings. Note that "
                "the name passed to aio_rfcomm.macos.prompt_under_name() is "
                "what the permission is remembered against, so changing it "
                "asks again."
            )


def _wanted(service: UUID | Collection[UUID] | None) -> list[str]:
    """
    Normalize the service filter into something the protocol can carry.

    Args:
        service: One service, several, or ``None`` for no filter.

    Returns:
        The UUIDs to match, empty for no filter.
    """
    if service is None:
        return []
    if isinstance(service, UUID):
        return [str(service)]
    return [str(u) for u in service]
