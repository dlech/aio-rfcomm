# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

# Bluetooth, RfcommChannel and RfcommService work as one unit across modules,
# calling members underscored only to keep them out of the public API.
# pyright: reportPrivateUsage=false

"""
The object that stands for Bluetooth on this machine.

It owns the connection to the operating system's Bluetooth service for as long
as its block lasts, reports whether Bluetooth is usable, and is what channels
are opened and services published through. It outlives Bluetooth going off
and on and the service restarting. What it opened does not, and each of those
says why it ended.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Collection, Generator
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)
from typing import Self
from uuid import UUID

from aio_rfcomm._scope import Scope
from aio_rfcomm.availability import Availability, UnavailableReason
from aio_rfcomm.backend import get_backend
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    check_channel,
)
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothAlreadyOpenError,
    BluetoothUnavailableError,
    RfcommError,
)
from aio_rfcomm.service import RfcommService

__all__ = ["AvailabilityWatcher", "Bluetooth", "open_bluetooth"]

logger = logging.getLogger(__name__)

# Whether a Bluetooth object is open in this process. See open_bluetooth.
_open = False


class AvailabilityWatcher:
    """
    Gives whether Bluetooth can be used: the current state first, then each change.

    Made by [`Bluetooth.watch_availability`][Bluetooth.watch_availability].
    """

    def __init__(self, state: AvailabilityState, scope: Scope) -> None:
        """
        Not called directly. Watchers come from
        [`Bluetooth.watch_availability`][Bluetooth.watch_availability].

        Args:
            state: Where the backend reports availability.
            scope: The Bluetooth object's scope.
        """
        self._state = state
        self._scope = scope
        self._last: Availability | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Availability:
        """
        Wait for availability to differ from what this watcher last said.

        Returns:
            The availability now. The first call returns at once.

        Raises:
            ScopeClosedError: The Bluetooth object's block has ended.
        """
        while True:
            self._scope.check()
            # Taken before reading the value, so a change in between is not
            # missed.
            changed = self._state.changed()
            current = self._state.value
            if current is not self._last:
                self._last = current
                return current
            await changed.wait()


class _Interruption:
    """
    Ends one wait when Bluetooth stops being usable, the way
    ``asyncio.timeout`` ends one at a deadline.
    """

    def __init__(self, task: asyncio.Task[object]) -> None:
        self.task = task
        self.cancelling = task.cancelling()
        self.error: BluetoothUnavailableError | None = None
        self.canceled = False

    def fire(self, error: BluetoothUnavailableError) -> None:
        if self.error is not None:
            return

        self.error = error
        # A task reporting the change itself is not waiting on anything, and
        # on 3.11 a cancel taken back is still delivered at its next await.
        if self.task is not asyncio.current_task():
            self.task.cancel()
            self.canceled = True

    def settle(self) -> bool:
        """
        Take back the cancel, if one was made.

        Returns:
            Whether nothing else canceled the task meanwhile.
        """
        if not self.canceled:
            return True
        return self.task.uncancel() <= self.cancelling


class Bluetooth:
    """
    Bluetooth on this computer.

    Made by [`open_bluetooth`][open_bluetooth]. Everything else is opened through it.
    """

    def __init__(
        self, backend: BackendBluetooth, state: AvailabilityState, scope: Scope
    ) -> None:
        """
        Not called directly. Use [`open_bluetooth`][open_bluetooth].

        Args:
            backend: The platform's implementation.
            state: Where the backend reports availability.
            scope: This object's scope.
        """
        self._backend = backend
        self._state = state
        self._scope = scope
        self._channels: set[RfcommChannel] = set()
        self._services: set[RfcommService] = set()
        self._interruptions: set[_Interruption] = set()

    def watch_availability(self) -> AvailabilityWatcher:
        """
        Watch whether Bluetooth can be used.

        The first value is the current state, given straight away. After that, a new
        value comes each time it changes. A watcher always gives the latest value, so
        a change that is undone before you ask for the next value is not seen.

        Each call makes a separate watcher. If two tasks need to watch, give each one
        its own.

        Returns:
            An async iterator of [`Availability`][aio_rfcomm.Availability]. It never ends by
            itself.
        """
        self._scope.check()
        return AvailabilityWatcher(self._state, self._scope)

    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        """
        List the devices the system knows, which means the paired ones.

        Pair devices in the system's own settings.

        Args:
            service: List only the devices with a service record of this service
                class, or of any of these. A UUID the record only mentions, such
                as a profile it follows, does not count. ``None`` lists every
                known device.

        Returns:
            The matching devices.

        Raises:
            BluetoothUnavailableError: Bluetooth cannot be used.
        """
        self._scope.check()
        with self._interrupted():
            return await self._backend.list_known_devices(service=service)

    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[RfcommChannel]:
        """
        Open a channel to a service on a device.

        The library looks up which channel the device offers the service on.

        Args:
            device: The device. To start from an address, make one with
                ``RfcommDeviceInfo("00:1A:7D:DA:71:0C")``.
            service: The service's UUID.

        Returns:
            An async context manager that gives the open channel, and closes it when
            the block ends.

        Raises:
            BluetoothUnavailableError: Bluetooth cannot be used.
            aio_rfcomm.errors.DeviceNotFoundError: The device did not answer.
            aio_rfcomm.errors.ServiceNotFoundError: The device does not offer the
                service.
            aio_rfcomm.errors.ConnectionFailedError: The channel could not be
                opened.
            aio_rfcomm.errors.UnsupportedOperationError: On macOS, the device
                already has a channel open.
        """
        self._scope.check()
        return self._open(lambda: self._backend.open_service(device, service))

    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[RfcommChannel]:
        """
        Open a channel to a device by its number, without looking up a service.

        Use it when the device's channel number is fixed and known. It is faster than
        [`open_service`][open_service].

        Args:
            device: The device.
            channel: The RFCOMM channel number, from 1 to 30.

        Returns:
            An async context manager that gives the open channel, and closes it when
            the block ends.

        Raises:
            ValueError: ``channel`` is not from 1 to 30.
            BluetoothUnavailableError: Bluetooth cannot be used.
            aio_rfcomm.errors.DeviceNotFoundError: The device did not answer.
            aio_rfcomm.errors.ServiceNotFoundError: Nothing on the device is
                listening on that channel.
            aio_rfcomm.errors.ConnectionFailedError: The channel could not be
                opened.
            aio_rfcomm.errors.UnsupportedOperationError: On macOS, the device
                already has a channel open. On Linux, Python lacks Bluetooth
                socket support.
        """
        self._scope.check()
        check_channel(channel)
        return self._open(lambda: self._backend.open_channel(device, channel))

    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[RfcommService]:
        """
        Publish a service and listen for devices that connect to it.

        ```python
        async with bluetooth.serve(CHAT, name="chat") as service:
            async for request in service.connection_requests():
                ...
        ```

        Leaving the block stops new connections. Channels already accepted carry on
        until their own blocks end.

        Args:
            service: The service's UUID. Devices look it up to find the channel.
            name: The service's name, which people see when they browse this
                computer's services.
            channel: The RFCOMM channel to listen on, from 1 to 30. Leave it out to
                let the library choose a free one.

        Returns:
            An async context manager that gives the service, and stops it when the
            block ends.

        Raises:
            ValueError: ``channel`` is not from 1 to 30.
            BluetoothUnavailableError: Bluetooth cannot be used.
            aio_rfcomm.errors.ChannelInUseError: The channel is already in use.
            aio_rfcomm.errors.ConnectionFailedError: The service could not be
                published.
            aio_rfcomm.errors.UnsupportedOperationError: On Linux, choosing a
                channel needs Bluetooth socket support, and Python lacks it.
        """
        self._scope.check()
        if channel is not None:
            check_channel(channel)
        return self._serve(service, name, channel)

    # -- what the handles share --------------------------------------------

    def _unusable(self) -> BluetoothUnavailableError | None:
        """
        Say why Bluetooth cannot be used, if it cannot.

        Returns:
            The error to raise, or ``None`` while Bluetooth is usable.
        """
        match self._state.value:
            case Availability.USABLE:
                return None
            case reason:
                return BluetoothUnavailableError(reason, self._state.detail)

    def _require_usable(self) -> None:
        """
        Raises:
            BluetoothUnavailableError: Bluetooth is not usable.
        """
        error = self._unusable()
        if error is not None:
            raise error

    @contextmanager
    def _interrupted(self) -> Generator[None, None, None]:
        """
        Refuse at once if Bluetooth is unusable, and stop waiting if it
        becomes so.

        Works the way ``asyncio.timeout`` does: the waiting task is
        canceled, and the cancellation is taken back and replaced on the way
        out, unless something else canceled it too.

        Yields:
            Nothing. The block runs until Bluetooth stops being usable.

        Raises:
            BluetoothUnavailableError: Bluetooth was or became unusable.
        """
        self._require_usable()
        task = asyncio.current_task()
        assert task is not None
        interruption = _Interruption(task)
        self._interruptions.add(interruption)
        try:
            yield
        except BaseException as error:
            if interruption.error is None:
                raise

            settled = interruption.settle()
            # A failure that lands as Bluetooth goes is put down to Bluetooth:
            # a device that stops answering because the radio went off is not
            # the device's fault. Anything else is a bug, and goes on up.
            if settled and isinstance(error, (asyncio.CancelledError, RfcommError)):
                raise interruption.error from error
            raise
        else:
            if interruption.error is not None:
                # What was opened as Bluetooth went is no use. It is ended on
                # the way out like everything else Bluetooth going ended.
                interruption.settle()
                raise interruption.error
        finally:
            self._interruptions.discard(interruption)

    @asynccontextmanager
    async def _open(
        self, opening: Callable[[], AbstractAsyncContextManager[BackendChannel]]
    ) -> AsyncGenerator[RfcommChannel, None]:
        """
        Open a channel, and keep it where Bluetooth going away can reach it.

        Args:
            opening: Makes the backend's channel, not yet open. Called only
                once Bluetooth is known to be usable.

        Yields:
            The open channel.
        """
        # Checked again here, since the block may be entered long after the
        # call that made it.
        self._scope.check()
        async with AsyncExitStack() as stack:
            with self._interrupted():
                backend = await stack.enter_async_context(opening())
            async with self._holding(backend) as channel:
                yield channel

    @asynccontextmanager
    async def _holding(
        self, backend: BackendChannel
    ) -> AsyncGenerator[RfcommChannel, None]:
        """
        Hand out an open channel for the length of a block.

        Its scope is this object's, not that of whatever produced it, so an
        accepted channel outlives the service it arrived through.

        Args:
            backend: The open channel.

        Yields:
            The channel callers hold.
        """
        channel = RfcommChannel(backend, self._scope.child("channel"))
        self._channels.add(channel)
        try:
            yield channel
        finally:
            self._channels.discard(channel)
            channel._close()

    @asynccontextmanager
    async def _serve(
        self, service: UUID, name: str, channel: int | None
    ) -> AsyncGenerator[RfcommService, None]:
        self._scope.check()
        async with AsyncExitStack() as stack:
            with self._interrupted():
                backend = await stack.enter_async_context(
                    self._backend.serve(service, name=name, channel=channel)
                )
            published = RfcommService(
                self, backend, service, name, self._scope.child("service")
            )
            self._services.add(published)
            try:
                yield published
            finally:
                self._services.discard(published)
                published._close()

    def _end_everything(self, reason: UnavailableReason, detail: str | None) -> None:
        """
        End whatever Bluetooth going away has ended.

        Called for every change away from usable, as it is reported -- not
        from a watcher, which would see only the latest value and so miss a
        change and back, after which macOS channels are dead without being
        told.

        Args:
            reason: Why Bluetooth is no longer usable.
            detail: What the platform said about it.
        """
        ending: list[Callable[[BluetoothUnavailableError], None]] = [
            *(channel._end for channel in self._channels),
            *(published._end for published in self._services),
            *(interruption.fire for interruption in self._interruptions),
        ]

        for end in ending:
            # One that fails must not leave the rest running on a Bluetooth
            # that has gone.
            try:
                end(BluetoothUnavailableError(reason, detail))
            except Exception:
                logger.exception("could not end something Bluetooth going ended")

    def _close(self) -> None:
        self._scope.close()
        # Wakes any watcher still waiting, so it finds the scope closed.
        self._state.wake()


@asynccontextmanager
async def _open_bluetooth(provider: BackendProvider) -> AsyncGenerator[Bluetooth, None]:
    """
    Open Bluetooth through a given backend. See [`open_bluetooth`][open_bluetooth].

    Args:
        provider: The platform's backend, or a stand-in for one.

    Yields:
        The Bluetooth object.
    """
    global _open

    if _open:
        raise BluetoothAlreadyOpenError(
            "Bluetooth is already open in this process. Pass that object to "
            "whatever needs it instead of opening another"
        )

    _open = True
    try:
        state = AvailabilityState()
        async with provider.open(state) as backend:
            await state.known()
            bluetooth = Bluetooth(backend, state, Scope("Bluetooth object"))
            state.on_unusable = bluetooth._end_everything
            try:
                yield bluetooth
            finally:
                state.on_unusable = None
                bluetooth._close()
    finally:
        _open = False


def open_bluetooth() -> AbstractAsyncContextManager[Bluetooth]:
    """
    Open Bluetooth.

    This succeeds even when Bluetooth is turned off or missing. Use
    [`Bluetooth.watch_availability`][Bluetooth.watch_availability] to find out whether it can be used.

    Open it once in a program, and pass the object to the functions that need it.

    Returns:
        An async context manager that gives the [`Bluetooth`][Bluetooth] object.
        Leaving the block closes everything that was opened through it.

    Raises:
        BluetoothAlreadyOpenError: Bluetooth is already open in this program.
        RfcommError: The library itself cannot start. On macOS, that is its
            helper process.
    """
    return _open_bluetooth(get_backend())
