# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The interface every platform backend implements.

Four abstract classes, in the order a caller meets them: the backend itself,
an adapter opened through it, and then either a channel opened through that or
a service published on it. Each is an :class:`~abc.ABC`, so a backend that
forgets a method fails at construction with the method named, rather than at
the first call.

Everything that opens something returns an async context manager. Nothing here
hands back a resource the caller has to remember to close.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Collection
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from aio_rfcomm.discovery import RfcommAdapterInfo, RfcommDeviceInfo
from aio_rfcomm.errors import AdapterLostReason, CloseReason

__all__ = [
    "FIRST_CHANNEL",
    "LAST_CHANNEL",
    "BackendAdapter",
    "BackendChannel",
    "BackendProvider",
    "BackendService",
]

# RFCOMM addresses a channel with five bits and reserves 0 and 31, so a server
# has thirty to choose from. A fact about the protocol rather than about any
# one platform, so every backend and the layer above share these.
FIRST_CHANNEL = 1
LAST_CHANNEL = 30


class BackendProvider(ABC):
    """
    A platform's implementation of this library.
    """

    @abstractmethod
    async def list_adapters(self) -> list[RfcommAdapterInfo]:
        """
        List the Bluetooth adapters on this machine.

        Returns:
            The adapters, in the platform's own order. macOS reports at most
            one.
        """
        raise NotImplementedError

    @abstractmethod
    def open_adapter(
        self, adapter: RfcommAdapterInfo | None = None
    ) -> AbstractAsyncContextManager[BackendAdapter]:
        """
        Open an adapter for use.

        Owns whatever the platform needs to talk to Bluetooth for as long as
        the context manager is held -- on macOS that includes a helper process.

        Args:
            adapter: Which adapter. ``None`` for the default.

        Returns:
            An async context manager yielding the open adapter.

        Raises:
            AdapterNotFoundError: No such adapter, or the machine has none.
            AdapterOffError: The adapter is present but switched off.
            PermissionDeniedError: The OS refused access to Bluetooth.
        """
        raise NotImplementedError


class BackendAdapter(ABC):
    """
    An adapter that has been opened and is ready to use.

    Subclasses must call ``super().__init__()``, and must call
    :meth:`_mark_gone` once when they notice the adapter has gone.
    """

    def __init__(self) -> None:
        self._gone = asyncio.Event()
        self._reason: AdapterLostReason | None = None

    def _mark_gone(self, reason: AdapterLostReason) -> None:
        """
        Record that the adapter has gone.

        Args:
            reason: Why the adapter is gone.
        """
        if self._reason is None:
            self._reason = reason
            self._gone.set()

    async def wait_until_gone(self) -> AdapterLostReason:
        """
        Wait until the adapter is gone.

        Implemented once here rather than by every backend.

        Returns:
            Why the adapter went away.
        """
        await self._gone.wait()
        assert self._reason is not None
        return self._reason

    @abstractmethod
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        """
        List the devices this adapter already knows about.

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
        self, device: RfcommDeviceInfo | str, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        """
        Open a channel to a service, looking up its channel number.

        Args:
            device: A device from :meth:`list_known_devices`, or a bare
                Bluetooth address. An address need not be paired or previously
                known.
            service: The service to look up and connect to.

        Returns:
            An async context manager yielding the open channel, closed on exit.

        Raises:
            DeviceNotFoundError: The device could not be reached.
            ServiceNotFoundError: The device does not offer ``service``.
            ConnectionFailedError: The channel could not be opened.
        """
        raise NotImplementedError

    @abstractmethod
    def open_channel(
        self, device: RfcommDeviceInfo | str, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        """
        Open a channel by number, skipping service discovery.

        Worth using when the number is fixed and known, since service
        discovery is a slow round trip.

        Args:
            device: A device from :meth:`list_known_devices`, or a bare
                Bluetooth address. An address need not be paired or previously
                known.
            channel: The RFCOMM channel number.

        Returns:
            An async context manager yielding the open channel, closed on exit.

        Raises:
            DeviceNotFoundError: The device could not be reached.
            ConnectionFailedError: The channel could not be opened.
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
            channel: The RFCOMM channel to listen on. ``None`` leaves the
                choice to the backend, which may refuse it.

        Returns:
            An async context manager yielding the listening service,
            withdrawn on exit.

        Raises:
            ChannelInUseError: Something already holds that channel.
            UnsupportedOperationError: This backend cannot serve yet, or
                cannot choose a channel and was given none.
        """
        raise NotImplementedError


class BackendChannel(ABC):
    """
    An open RFCOMM channel, as the backend sees it.

    Subclasses must call ``super().__init__()``, and must call
    :meth:`_mark_gone` once when they notice the channel has ended.
    """

    def __init__(self) -> None:
        self._gone = asyncio.Event()
        self._reason: CloseReason | None = None

    def _mark_gone(self, reason: CloseReason) -> None:
        """
        Record that the channel has ended.

        Called by the backend when it detects the channel is gone. Only the
        first call counts, so a backend that notices twice does no harm.

        Args:
            reason: Why the channel is gone.
        """
        if self._reason is None:
            self._reason = reason
            self._gone.set()

    async def wait_until_gone(self) -> CloseReason:
        """
        Wait until the channel is gone.

        Implemented once here rather than by every backend: detecting the end
        is platform work, but waiting on it and handing the reason to however
        many callers are interested is not.

        Returns:
            Why the channel went away.
        """
        await self._gone.wait()
        assert self._reason is not None
        return self._reason

    @abstractmethod
    async def send(self, data: bytes) -> None:
        """
        Send all of ``data``.

        Args:
            data: The bytes to send. May be longer than the channel can carry
                in one go; splitting it is the implementation's problem, not
                the caller's.

        Raises:
            ChannelBrokenError: A previous send was cancelled.
            ChannelClosedError: The channel went away.
        """
        raise NotImplementedError

    @abstractmethod
    async def receive(self, max_bytes: int | None = None) -> bytes:
        """
        Take the next bytes to arrive.

        Args:
            max_bytes: At most this many bytes. ``None`` for any amount.

        Returns:
            Between 1 and ``max_bytes`` bytes, or ``b""`` once the peer has
            hung up (:attr:`~aio_rfcomm.errors.CloseReason.PEER_CLOSED`).

        Raises:
            ChannelClosedError: The channel went away unexpectedly.
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
    async def accept(
        self,
    ) -> tuple[RfcommDeviceInfo, AbstractAsyncContextManager[BackendChannel]]:
        """
        Wait for the next peer to connect.

        The channel comes back unopened, as a context manager, so that
        whoever takes it decides how long it lives -- the same way a channel
        opened from this side is handed over.

        Returns:
            Who connected, and their channel as an async context manager.
        """
        raise NotImplementedError
