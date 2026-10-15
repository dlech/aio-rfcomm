# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS backend, driving IOBluetooth through a helper process.

IOBluetooth will only work from the main thread of a process running a
CFRunLoop, and macOS ends any process that touches Bluetooth without an
application bundle saying why it wants to. A library cannot impose either
condition on the program that imports it, so it brings its own process that
meets both; see :mod:`aio_rfcomm.backend.iobluetooth.__main__`.

Nothing on this side imports rubicon or the framework. What comes back from
the helper is an ordinary connected file descriptor, which is exactly what the
other backends produce, so everything after the connection is shared code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import AsyncGenerator, Callable, Collection
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from uuid import UUID

from typing_extensions import override

from aio_rfcomm._teardown import unwrap_lone_error
from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth._codes import CBManagerAuthorization, CBManagerState
from aio_rfcomm.backend.iobluetooth._helper import (
    Ending,
    Helper,
    HelperFailedError,
    run_helper,
)
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    BackendService,
)
from aio_rfcomm.backend.stream import StreamChannel, StreamProtocol, open_stream
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    CloseReason,
    ConnectionFailedError,
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
    async def explain_end(self) -> CloseReason:
        """
        Say why the channel ended, as the helper saw it.

        macOS can tell a peer closing one channel from the whole link
        dropping, by looking at whether the baseband connection is still up.
        The helper sends that reason before it closes the socket, so by the
        time the stream here reads empty the answer is already on its way and
        this waits for it rather than guessing.

        Returns:
            Why the channel went away.
        """
        return _reason(await self._ending.wait())

    @override
    def abort(self) -> None:
        # The helper says why a channel ended only when the device ended it.
        # Ended from this side, nothing would ever answer explain_end.
        self._ending.say(CloseReason.LINK_LOST.name)
        super().abort()


class IOBluetoothBluetooth(BackendBluetooth):
    """
    Bluetooth on this Mac, worked through whichever helper is running.
    """

    def __init__(self) -> None:
        self.helper: Helper | None = None
        """
        The running helper, or ``None`` between one and the next.
        """

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
        # Measured, not assumed: a service record publishes correctly and a
        # peer's connection to it is accepted, but IOBluetooth never delivers
        # the opened channel -- registerForChannelOpenNotifications does not
        # fire, filtered or not, exactly as registerForChannelCloseNotification
        # does not. So the connection is taken and then dropped, and there is
        # nothing here to hand a caller.
        raise UnsupportedOperationError(
            "serving is not implemented on macOS. A service record can be "
            "published and a peer can reach it, but IOBluetooth never hands "
            "over the opened channel, so nothing can answer. Connecting to "
            "another machine's service from here works."
        )

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

        async with AsyncExitStack() as stack:
            ending = helper.expect(handle)
            stack.callback(helper.forget, handle)
            # Pushed before the socket, so that it runs after it: closing this
            # end is what tells the helper to let the device go, and until it
            # has, another channel to that device is refused.
            stack.push_async_callback(helper.wait_until_finished, handle)

            sock = socket.socket(fileno=fds[0])
            transport, protocol = await open_stream(sock)
            opened = IOBluetoothChannel(transport, protocol, ending)
            stack.push_async_callback(opened.close)

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
        bluetooth = IOBluetoothBluetooth()
        with unwrap_lone_error():
            async with asyncio.TaskGroup() as group:
                supervising = group.create_task(
                    self._supervise(bluetooth, state), name="aio-rfcomm helper"
                )
                try:
                    yield bluetooth
                finally:
                    supervising.cancel()

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
            HelperFailedError: The first helper could not be started. A later
                one that cannot be is reported as unavailable instead, since
                the caller has long since been told Bluetooth was there.
        """
        replacing = False

        while True:
            running = False
            try:
                async with self._start() as helper:
                    running = True
                    bluetooth.helper = helper

                    # A helper that stops takes its channels with it, and they
                    # have to hear that the service went, not that their link
                    # dropped, so this is said before they are told anything.
                    def stopped() -> None:
                        state.set(
                            Availability.SERVICE_UNAVAILABLE,
                            "the macOS Bluetooth helper stopped",
                        )

                    helper.on_stop = stopped
                    if helper.stopped:
                        # It stopped before there was anyone to hear it.
                        stopped()
                    try:
                        await _relay(helper, state)
                    finally:
                        helper.on_stop = None
                        bluetooth.helper = None
            except BluetoothUnavailableError as refused:
                # macOS refused Bluetooth to the helper and ended it, and would
                # refuse a new one the same way.
                state.set(refused.reason, str(refused))
                return
            except (HelperFailedError, OSError) as failed:
                if not running and not replacing:
                    raise
                if not running:
                    # A replacement that will not start would not start next
                    # time either; trying again would only spin.
                    state.set(
                        Availability.SERVICE_UNAVAILABLE,
                        f"the macOS Bluetooth helper could not be restarted: {failed}",
                    )
                    return
                logger.info("the macOS Bluetooth helper stopped: %s", failed)
            else:
                logger.info("the Bluetooth service restarted; starting a new helper")

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
                    "this Mac's Bluetooth is switched off; turn it on",
                )
            case Availability.NOT_ALLOWED:
                state.set(Availability.NOT_ALLOWED, _not_allowed(authorization))
            case available:
                state.set(available)


def _availability(manager: int, authorization: int) -> Availability | None:
    """
    Say what a ``CBManagerState`` means for whether Bluetooth is usable.

    Args:
        manager: The manager's state.
        authorization: The ``CBManagerAuthorization`` at the time, since macOS
            reports a radio this program may not use as switched off.

    Returns:
        The availability, or ``None`` for a state that is not an answer.
    """
    match manager:
        case CBManagerState.POWERED_ON:
            return Availability.USABLE
        case CBManagerState.POWERED_OFF if (
            authorization == CBManagerAuthorization.ALLOWED_ALWAYS
        ):
            return Availability.SWITCHED_OFF
        case CBManagerState.POWERED_OFF | CBManagerState.UNAUTHORIZED:
            return Availability.NOT_ALLOWED
        case CBManagerState.RESETTING:
            # Set only when the connection to bluetoothd is lost, never on an
            # ordinary toggle (follow-ups §13.2).
            return Availability.SERVICE_UNAVAILABLE
        case CBManagerState.UNSUPPORTED:
            return Availability.UNSUPPORTED
        case CBManagerState.UNKNOWN:
            return None
        case _:
            logger.warning("CoreBluetooth reported state %d, unknown here", manager)
            return None


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
        case CBManagerAuthorization.NOT_DETERMINED:
            return (
                "Bluetooth permission for this program has not been answered. "
                "A dialog should have appeared; if it was dismissed, run the "
                "program again and allow it."
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
    Normalise the service filter into something the protocol can carry.

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


def _reason(name: str) -> CloseReason:
    """
    Turn the helper's word for a close back into one of ours.

    Args:
        name: The reason's name, as the helper sent it.

    Returns:
        Why the channel went away, or a lost link if the name is not one we
        know -- which can only mean the helper is a different version.
    """
    try:
        return CloseReason[name]
    except KeyError:
        return CloseReason.LINK_LOST
