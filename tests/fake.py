# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
A backend that implements the contract without any Bluetooth.

Everything about a channel's lifecycle is pure logic, so the whole of it can
be exercised here -- on any platform, in CI, with no radio and no helper
process. Each real backend has to satisfy the same tests.
"""

import asyncio
from collections.abc import AsyncGenerator, Collection
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID

from typing_extensions import override

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    BackendRequest,
    BackendService,
)
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import ChannelClosedError, ChannelInUseError, CloseReason

DEVICE = RfcommDeviceInfo("00:11:22:33:44:55", "fake device")


class FakeChannel(BackendChannel):
    """
    A channel whose every event the test drives by hand.

    Keeps the backend contract: what was delivered is handed over first, and
    only then is the end raised, the same way every time.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent = bytearray()
        self.closed = False
        self.aborted = False
        self.send_blocks = False
        self._incoming = bytearray()
        self._changed = asyncio.Event()

    @override
    def abort(self) -> None:
        self.aborted = True
        self.go_away(CloseReason.LINK_LOST)

    @override
    async def send(self, data: bytes) -> None:
        if self.reason is not None:
            raise ChannelClosedError(self.reason)
        if self.send_blocks:
            self._changed.clear()
            await self._changed.wait()
            if self.reason is not None:
                raise ChannelClosedError(self.reason)
        self.sent += data

    @override
    async def receive(self, max_bytes: int | None = None) -> bytes:
        while not self._incoming:
            if self.reason is not None:
                raise ChannelClosedError(self.reason)
            self._changed.clear()
            await self._changed.wait()
        count = len(self._incoming) if max_bytes is None else max_bytes
        taken = bytes(self._incoming[:count])
        del self._incoming[:count]
        return taken

    # -- test controls -------------------------------------------------

    def deliver(self, data: bytes) -> None:
        """
        Hand the channel some incoming bytes.
        """
        self._incoming += data
        self._changed.set()

    def go_away(self, reason: CloseReason = CloseReason.PEER_CLOSED) -> None:
        """
        End the channel, as the backend would on noticing it had gone.
        """
        self._mark_gone(reason)
        self._changed.set()


class FakeRequest(BackendRequest):
    """
    A peer that has connected, holding the channel the test made for it.
    """

    def __init__(self, peer: RfcommDeviceInfo, channel: FakeChannel) -> None:
        self._peer = peer
        self.channel = channel
        self.taken = False

    @property
    @override
    def info(self) -> RfcommDeviceInfo:
        return self._peer

    @override
    def open(self) -> AbstractAsyncContextManager[BackendChannel]:
        return self._hold()

    @asynccontextmanager
    async def _hold(self) -> AsyncGenerator[BackendChannel, None]:
        assert not self.channel.closed, "opened a request that was closed"
        self.taken = True
        try:
            yield self.channel
        finally:
            self.channel.closed = True

    @override
    def close(self) -> None:
        if not self.taken:
            self.channel.closed = True


class FakeService(BackendService):
    """
    A published service whose peers the test conjures up by hand.
    """

    def __init__(self, service: UUID, name: str, channel: int) -> None:
        self.service = service
        self.name = name
        self.withdrawn = False
        self._channel = channel
        self._arriving: asyncio.Queue[FakeRequest] = asyncio.Queue()

    @property
    @override
    def channel(self) -> int:
        return self._channel

    @override
    async def accept(self) -> BackendRequest:
        return await self._arriving.get()

    # -- test controls -------------------------------------------------

    def connect(self, peer: RfcommDeviceInfo = DEVICE) -> FakeChannel:
        """
        Have a peer connect to the service.

        Returns:
            The channel its request holds.
        """
        channel = FakeChannel()
        self._arriving.put_nowait(FakeRequest(peer, channel))
        return channel


class FakeBluetooth(BackendBluetooth):
    """
    Bluetooth that hands out fake channels and services.
    """

    def __init__(self) -> None:
        self.open_calls = 0
        self.opens_block = False
        # What a blocked open does when cancelled: re-raise, raise this
        # instead, or finish the open regardless.
        self.open_cancel_raises: BaseException | None = None
        self.open_cancel_finishes = False
        self.channels: list[FakeChannel] = []
        self.services: list[FakeService] = []
        self.listed = 0

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        self.listed += 1
        return [DEVICE]

    @override
    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._open()

    @override
    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._open()

    @override
    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[BackendService]:
        return self._serve(service, name, channel)

    @asynccontextmanager
    async def _serve(
        self, service: UUID, name: str, channel: int | None
    ) -> AsyncGenerator[BackendService, None]:
        taken = {s.channel for s in self.services if not s.withdrawn}
        if channel is None:
            channel = next(c for c in range(30, 0, -1) if c not in taken)
        elif channel in taken:
            raise ChannelInUseError(f"channel {channel} is taken")
        published = FakeService(service, name, channel)
        self.services.append(published)
        try:
            yield published
        finally:
            published.withdrawn = True

    @asynccontextmanager
    async def _open(self) -> AsyncGenerator[BackendChannel, None]:
        self.open_calls += 1
        if self.opens_block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if self.open_cancel_raises is not None:
                    raise self.open_cancel_raises from None
                if not self.open_cancel_finishes:
                    raise
        channel = FakeChannel()
        self.channels.append(channel)
        try:
            yield channel
        finally:
            channel.closed = True


class FakeProvider(BackendProvider):
    """
    A provider whose availability the test sets by hand.
    """

    def __init__(self, first: Availability | None = Availability.USABLE) -> None:
        """
        Args:
            first: What to report on opening, or ``None`` to report nothing
                until the test says.
        """
        self.backend = FakeBluetooth()
        self.opened = 0
        self._first = first
        self._state: AvailabilityState | None = None

    @override
    def open(
        self, state: AvailabilityState
    ) -> AbstractAsyncContextManager[BackendBluetooth]:
        return self._open(state)

    @asynccontextmanager
    async def _open(
        self, state: AvailabilityState
    ) -> AsyncGenerator[BackendBluetooth, None]:
        self.opened += 1
        self._state = state
        if self._first is not None:
            state.set(self._first)
        try:
            yield self.backend
        finally:
            self._state = None

    # -- test controls -------------------------------------------------

    def report(self, availability: Availability, detail: str | None = None) -> None:
        """
        Report availability, as the platform would on a change.
        """
        assert self._state is not None, "nothing has opened Bluetooth"
        self._state.set(availability, detail)
