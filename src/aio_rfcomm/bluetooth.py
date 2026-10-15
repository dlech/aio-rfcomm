# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The object that stands for Bluetooth on this machine.

It owns the connection to the operating system's Bluetooth service for as long
as its block lasts, reports whether Bluetooth is usable, and is what channels
are opened and services published through. It outlives Bluetooth going off
and on and the service restarting; what it opened does not, and each of those
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

# Whether a Bluetooth object is open in this process; see open_bluetooth.
_open = False


class AvailabilityWatcher:
    """
    Says whether Bluetooth is usable: now, and again each time that changes.

    An iterator object rather than an async generator, so that leaving a loop
    over it early leaves nothing suspended to be cleaned up later.
    """

    def __init__(self, state: AvailabilityState, scope: Scope) -> None:
        """
        Not called directly. Watchers come from
        :meth:`Bluetooth.watch_availability`.

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
    :func:`asyncio.timeout` ends one at a deadline.
    """

    def __init__(self, task: asyncio.Task[object]) -> None:
        self.task = task
        self.cancelling = task.cancelling()
        self.error: BluetoothUnavailableError | None = None
        self.cancelled = False

    def fire(self, error: BluetoothUnavailableError) -> None:
        if self.error is not None:
            return

        self.error = error
        # A task reporting the change itself is not waiting on anything, and
        # on 3.11 a cancel taken back is still delivered at its next await.
        if self.task is not asyncio.current_task():
            self.task.cancel()
            self.cancelled = True

    def settle(self) -> bool:
        """
        Take back the cancel, if one was made.

        Returns:
            Whether nothing else cancelled the task meanwhile.
        """
        if not self.cancelled:
            return True
        return self.task.uncancel() <= self.cancelling


class Bluetooth:
    """
    Bluetooth on this machine.

    Opened with :func:`open_bluetooth`, which waits until the platform has
    said whether Bluetooth is usable, so there is always an answer. Whether it
    is usable is reported by :meth:`watch_availability`; anything attempted
    while it is not raises :class:`~aio_rfcomm.errors.BluetoothUnavailableError`
    at once, and every channel, open and published service ends with that
    error when it stops being usable.
    """

    def __init__(
        self, backend: BackendBluetooth, state: AvailabilityState, scope: Scope
    ) -> None:
        """
        Not called directly. Use :func:`open_bluetooth`.

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
        Watch whether Bluetooth is usable.

        Each call makes an independent watcher: two tasks each watching both
        see every change. Give each task its own; tasks sharing one would
        split the values between them.

        The first value comes at once. After that, each value is the
        availability at the time, whenever it differs from the last one this
        watcher returned; a change and back between two reads returns
        nothing, since whatever that change ended raised its own error.

        Returns:
            An async iterator of :class:`~aio_rfcomm.Availability`. It never
            ends by itself.
        """
        self._scope.check()
        return AvailabilityWatcher(self._state, self._scope)

    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        """
        List the devices the OS already knows about.

        A snapshot of what the OS knows, which in practice means paired
        devices. Pairing is not something this library does yet, so for now
        it happens in the OS's own settings.

        Args:
            service: Return only devices offering this service, or any of
                these services. ``None`` returns every known device.

        Returns:
            What the OS knows about each matching device.

        Raises:
            BluetoothUnavailableError: Bluetooth is not usable.
        """
        self._scope.check()
        with self._interrupted():
            return await self._backend.list_known_devices(service=service)

    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[RfcommChannel]:
        """
        Open a channel to a service, looking up its channel number.

        Args:
            device: The device. A program that starts from an address makes
                one from it: ``RfcommDeviceInfo("00:1A:7D:DA:71:0C")``.
            service: The service to look up and connect to.

        Returns:
            An async context manager yielding the open channel, closed on
            exit. Entering it raises what opening the channel raises:
            :class:`~aio_rfcomm.errors.BluetoothUnavailableError` if Bluetooth
            is or becomes unusable,
            :class:`~aio_rfcomm.errors.DeviceNotFoundError`,
            :class:`~aio_rfcomm.errors.ServiceNotFoundError` or
            :class:`~aio_rfcomm.errors.ConnectionFailedError`.
        """
        self._scope.check()
        return self._open(lambda: self._backend.open_service(device, service))

    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[RfcommChannel]:
        """
        Open a channel by number, skipping service discovery.

        Worth using when the number is fixed and known, since service
        discovery is a slow round trip.

        Args:
            device: The device.
            channel: The RFCOMM channel number, 1 to 30.

        Returns:
            An async context manager yielding the open channel, closed on
            exit. Entering it raises what opening the channel raises:
            :class:`~aio_rfcomm.errors.BluetoothUnavailableError` if Bluetooth
            is or becomes unusable,
            :class:`~aio_rfcomm.errors.DeviceNotFoundError`,
            :class:`~aio_rfcomm.errors.ServiceNotFoundError` if the device
            refused the channel -- on macOS and Linux, and usually because
            nothing there is listening on it -- or
            :class:`~aio_rfcomm.errors.ConnectionFailedError`.

        Raises:
            ValueError: ``channel`` is not an RFCOMM channel number.
        """
        self._scope.check()
        check_channel(channel)
        return self._open(lambda: self._backend.open_channel(device, channel))

    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[RfcommService]:
        """
        Publish a service and listen for peers, like a listening socket.

        ::

            async with bluetooth.serve(CHAT, name="chat") as service:
                async for request in service.connection_requests():
                    ...

        Leaving the block withdraws the service and closes any request nobody
        accepted or declined. Channels already accepted belong to their own
        blocks and carry on.

        Args:
            service: The service UUID to publish. Peers look this up to find
                the channel number, so it has to be one they already know.
            name: The human-readable service name, which goes into the
                published record. Required, because it is what a person sees
                when they look at what this machine offers.
            channel: The RFCOMM channel to listen on, 1 to 30. Leave it unset
                to let the library choose, which it can do everywhere except
                on a Linux Python built without Bluetooth socket support --
                there the choice cannot be made safely and has to be yours.

        Returns:
            An async context manager yielding the published service, withdrawn
            on exit. Entering it raises
            :class:`~aio_rfcomm.errors.BluetoothUnavailableError` if Bluetooth
            is unusable, :class:`~aio_rfcomm.errors.ChannelInUseError` if the
            channel is taken, or
            :class:`~aio_rfcomm.errors.UnsupportedOperationError` where the
            platform cannot serve.

        Raises:
            ValueError: ``channel`` is not an RFCOMM channel number.
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

        Works the way :func:`asyncio.timeout` does: the waiting task is
        cancelled, and the cancellation is taken back and replaced on the way
        out, unless something else cancelled it too.

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
                # What was opened as Bluetooth went is no use; it is ended on
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
    Open Bluetooth through a given backend; see :func:`open_bluetooth`.

    Args:
        provider: The platform's backend, or a stand-in for one.

    Yields:
        The Bluetooth object.
    """
    global _open

    if _open:
        raise BluetoothAlreadyOpenError(
            "Bluetooth is already open in this process; pass that object to "
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
    Open Bluetooth for use.

    Waits until the platform has said whether Bluetooth is usable, and then
    always succeeds, whatever the answer: Bluetooth may be switched off, or
    missing altogether, and :meth:`Bluetooth.watch_availability` is how to
    find out which and to wait for it to change.

    One per process. Code that needs Bluetooth takes the object as an
    argument rather than opening its own.

    Returns:
        An async context manager yielding the Bluetooth object. Leaving it
        closes everything opened through it.

    Raises:
        BluetoothAlreadyOpenError: One is already open in this process.
    """
    return _open_bluetooth(get_backend())
