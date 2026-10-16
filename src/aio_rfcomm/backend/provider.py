# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The interface every platform backend implements.

The classes come in the order a caller meets them: the backend itself, what
it opens to stand for Bluetooth on this machine, and then either a channel
opened through that or a service published on it, with the requests it
receives. Each is an ``ABC``, so a backend that forgets a method
fails at construction with the method named, rather than at the first call.

Everything that opens something returns an async context manager. Nothing here
hands back a resource the caller has to remember to close.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from typing_extensions import override

from aio_rfcomm.availability import Availability, UnavailableReason
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import CloseReason, UnsupportedOperationError

__all__ = [
    "FIRST_CHANNEL",
    "LAST_CHANNEL",
    "AvailabilityState",
    "BackendBluetooth",
    "BackendChannel",
    "BackendProvider",
    "BackendRequest",
    "BackendService",
    "NoBluetooth",
    "check_channel",
]

# RFCOMM addresses a channel with five bits and reserves 0 and 31, so a server
# has thirty to choose from. A fact about the protocol rather than about any
# one platform, so every backend and the layer above share these.
FIRST_CHANNEL = 1
LAST_CHANNEL = 30


def check_channel(channel: int) -> None:
    """
    Refuse a number that cannot be an RFCOMM channel, before anything is
    contacted.

    Args:
        channel: The channel number a caller gave.

    Raises:
        ValueError: It is outside 1 to 30.
    """
    if not FIRST_CHANNEL <= channel <= LAST_CHANNEL:
        raise ValueError(
            f"channel {channel} is not an RFCOMM channel. They run from "
            f"{FIRST_CHANNEL} to {LAST_CHANNEL}"
        )


class AvailabilityState:
    """
    Whether Bluetooth is usable, as a backend last reported it.

    The backend sets it. Everything else reads it, or waits for it to change.
    Only ever touched from the event loop's thread.
    """

    def __init__(self) -> None:
        self._value: Availability | None = None
        self._detail: str | None = None
        self._changed = asyncio.Event()
        self._known = asyncio.Event()

        self.on_unusable: Callable[[UnavailableReason, str | None], None] | None = None
        """
        Called with every change away from usable, as it is set. A change and
        back can happen between two looks at ``value``, and what it ended
        has to be ended all the same.
        """

    @property
    def value(self) -> Availability:
        """
        The availability now. Only read once ``known`` has returned.
        """
        assert self._value is not None, "availability read before it was reported"
        return self._value

    @property
    def detail(self) -> str | None:
        """
        What the platform said about it, when that says more than the value.
        """
        return self._detail

    def set(self, value: Availability, detail: str | None = None) -> None:
        """
        Report the availability.

        Args:
            value: Whether Bluetooth is usable, and if not why not.
            detail: What the platform said about it, for the error message.
        """
        if value is self._value and detail == self._detail:
            return

        was = self._value
        self._value = value
        self._detail = detail
        self._known.set()
        self.wake()

        match value:
            case Availability.USABLE:
                pass
            case reason:
                if was is not value and self.on_unusable is not None:
                    self.on_unusable(reason, detail)

    def changed(self) -> asyncio.Event:
        """
        Something to wait on for the next change.

        Taken before looking at ``value``, so that a change in between is
        not missed.

        Returns:
            An event set at the next change.
        """
        return self._changed

    def wake(self) -> None:
        """
        Wake everything waiting for a change, whether or not there was one.
        """
        self._changed.set()
        self._changed = asyncio.Event()

    @property
    def is_known(self) -> bool:
        """
        Whether the first report has been made.
        """
        return self._known.is_set()

    async def known(self) -> None:
        """
        Wait for the first report.
        """
        await self._known.wait()


class BackendProvider(ABC):
    """
    A platform's implementation of this library.
    """

    @abstractmethod
    def open(
        self, state: AvailabilityState
    ) -> AbstractAsyncContextManager[BackendBluetooth]:
        """
        Open Bluetooth for use.

        Owns whatever the platform needs for as long as the context manager is
        held -- on macOS, a helper process -- and may replace it to keep
        Bluetooth working, after the system's Bluetooth service restarts, say.

        Reports availability into ``state`` from the first reading for as long
        as it is held: from tasks of its own, or in the course of another call
        when that is where a change is noticed -- a channel finding its link
        gone, say. The first reading may come after the context manager has
        been entered. Nothing is asked of the backend before it has.

        Args:
            state: Where to report availability.

        Returns:
            An async context manager yielding the backend's Bluetooth object.
        """
        raise NotImplementedError


class BackendBluetooth(ABC):
    """
    Bluetooth on this machine, as the backend sees it.

    Nothing here is called while the backend has reported Bluetooth unusable.
    What was opened earlier is still closed then, though -- channels and
    services leave their blocks, requests are closed -- so closing must
    neither raise nor wait on a platform that has gone.
    """

    @abstractmethod
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        """
        List the devices the OS already knows about.

        A snapshot of what the OS knows, which in practice means paired
        devices. Scanning for new ones is a separate feature, not yet
        implemented.

        Args:
            service: Return only devices offering this service, or any of
                these services. ``None`` returns every known device.

        Returns:
            The matching devices.
        """
        raise NotImplementedError

    @abstractmethod
    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        """
        Open a channel to a service, looking up its channel number.

        Args:
            device: The device. Need not be paired or previously known.
            service: The service to look up and connect to.

        Returns:
            An async context manager yielding the open channel, closed on exit.

        Raises:
            BluetoothUnavailableError: Bluetooth turned out not to be usable.
            DeviceNotFoundError: The device could not be reached.
            ServiceNotFoundError: The device does not offer ``service``.
            ConnectionFailedError: The channel could not be opened.
            UnsupportedOperationError: The platform cannot open a channel
                now, though it could at another time. macOS holds only one
                per device at once.
        """
        raise NotImplementedError

    @abstractmethod
    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        """
        Open a channel by number, skipping service discovery.

        Args:
            device: The device. Need not be paired or previously known.
            channel: The RFCOMM channel number, already checked.

        Returns:
            An async context manager yielding the open channel, closed on exit.

        Raises:
            BluetoothUnavailableError: Bluetooth turned out not to be usable.
            DeviceNotFoundError: The device could not be reached.
            ServiceNotFoundError: The device refused the channel, usually
                because nothing there is listening on it.
            ConnectionFailedError: The channel could not be opened.
            UnsupportedOperationError: The platform cannot open a channel
                now, though it could at another time. macOS holds only one
                per device at once.
        """
        raise NotImplementedError

    @abstractmethod
    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[BackendService]:
        """
        Publish a service and listen for peers.

        Args:
            service: The service UUID to publish.
            name: The human-readable service name, which goes into the
                published record.
            channel: The RFCOMM channel to listen on, already checked.
                ``None`` leaves the choice to the backend, which may refuse
                it.

        Returns:
            An async context manager yielding the listening service,
            withdrawn on exit.

        Raises:
            ChannelInUseError: Something already holds that channel.
            UnsupportedOperationError: This backend cannot serve yet, or
                cannot choose a channel and was given none.
        """
        raise NotImplementedError


class NoBluetooth(BackendBluetooth):
    """
    Stands in when there is nothing to reach, because Bluetooth has been
    reported unusable from the start. Never asked anything, since nothing is
    asked of a backend while it says Bluetooth is unusable.
    """

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        raise UnsupportedOperationError("Bluetooth is not usable here")

    @override
    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        raise UnsupportedOperationError("Bluetooth is not usable here")

    @override
    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        raise UnsupportedOperationError("Bluetooth is not usable here")

    @override
    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[BackendService]:
        raise UnsupportedOperationError("Bluetooth is not usable here")


class BackendChannel(ABC):
    """
    An open RFCOMM channel, as the backend sees it.

    Every ending is raised: once the channel is over, ``receive`` -- after
    handing over whatever arrived before the end -- and ``send`` raise
    ``ChannelClosedError``, with the same reason
    every time.

    Subclasses must call ``super().__init__()``, and record the end with
    ``_mark_gone`` once they know why.
    """

    def __init__(self) -> None:
        self._reason: CloseReason | None = None

    def _mark_gone(self, reason: CloseReason) -> None:
        """
        Record why the channel ended. Only the first call counts.

        Args:
            reason: Why the channel is gone.
        """
        if self._reason is None:
            self._reason = reason

    @property
    def reason(self) -> CloseReason | None:
        """
        Why the channel ended, once that is known.
        """
        return self._reason

    @property
    def ended(self) -> bool:
        """
        Whether the channel is over, which may be known before why it is.
        """
        return self._reason is not None

    @abstractmethod
    def abort(self) -> None:
        """
        Stop the channel now, because Bluetooth has gone.

        Pending and later calls raise their ending as usual. What arrived
        before still comes out first. Must not raise: it is called while
        everything else Bluetooth going ended is being ended too.
        """
        raise NotImplementedError

    @abstractmethod
    async def send(self, data: bytes) -> None:
        """
        Send all of ``data``.

        Hands all of it to the platform before waiting for anything, so that
        canceling the wait never splits it: a canceled send still sends
        everything, unless the channel ends first.

        Args:
            data: The bytes to send. May be longer than the channel can carry
                in one go. Splitting it is the implementation's problem, not
                the caller's.

        Raises:
            ChannelClosedError: The channel is over.
        """
        raise NotImplementedError

    @abstractmethod
    async def receive(self, max_bytes: int | None = None) -> bytes:
        """
        Take the next bytes to arrive.

        Args:
            max_bytes: At most this many bytes, at least 1. ``None`` for
                whatever has arrived.

        Returns:
            Between 1 and ``max_bytes`` bytes. Never empty.

        Raises:
            ChannelClosedError: The channel is over, and everything that
                arrived before the end has been taken.
        """
        raise NotImplementedError


class BackendService(ABC):
    """
    A published service, listening for peers.
    """

    @property
    @abstractmethod
    def channel(self) -> int:
        """
        The RFCOMM channel the service is listening on.
        """
        raise NotImplementedError

    @abstractmethod
    async def accept(self) -> BackendRequest:
        """
        Wait for the next peer to connect.

        Returns:
            The peer's request, holding its connection until it is opened or
            closed.
        """
        raise NotImplementedError


class BackendRequest(ABC):
    """
    A peer that has connected, and the connection it is holding.

    The connection belongs to the request until ``open`` hands it to a
    channel. Closing it before then, by ``close`` or ``decline``, is
    the only other way it ends.
    """

    @property
    @abstractmethod
    def info(self) -> RfcommDeviceInfo:
        """
        Who connected.
        """
        raise NotImplementedError

    @abstractmethod
    def open(self) -> AbstractAsyncContextManager[BackendChannel]:
        """
        Take the connection as a channel.

        The connection passes to the channel only when the context manager is
        entered. Until then ``close`` still closes it.

        Returns:
            An async context manager yielding the channel, closed on exit.
        """
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """
        Hang up at once, unless a channel has the connection already.

        Safe to call more than once, and from a finalizer.
        """
        raise NotImplementedError

    async def decline(self) -> None:
        """
        Turn the peer away.

        Hangs up before anything is awaited, so it takes effect even if
        canceled. A backend that has to wait for its platform to finish
        overrides this, and still closes first.
        """
        self.close()
