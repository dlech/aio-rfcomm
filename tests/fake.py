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

from aio_rfcomm.backend.provider import BackendAdapter, BackendChannel, BackendProvider
from aio_rfcomm.discovery import RfcommAdapterInfo, RfcommDeviceInfo
from aio_rfcomm.errors import AdapterLostReason, CloseReason

DEVICE = RfcommDeviceInfo("00:11:22:33:44:55", "fake device")
ADAPTER = RfcommAdapterInfo("fake0", "AA:BB:CC:DD:EE:FF", "fake adapter")


class FakeChannel(BackendChannel):
    """
    A channel whose every event the test drives by hand.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent = bytearray()
        self.closed = False
        self.send_blocks = False
        self._incoming: asyncio.Queue[bytes] = asyncio.Queue()

    @override
    async def send(self, data: bytes) -> None:
        if self.send_blocks:
            await asyncio.Event().wait()
        self.sent += data

    @override
    async def receive(self, max_bytes: int | None = None) -> bytes:
        return await self._incoming.get()

    # -- test controls -------------------------------------------------

    def deliver(self, data: bytes) -> None:
        """
        Hand the channel some incoming bytes.
        """
        self._incoming.put_nowait(data)

    def go_away(self, reason: CloseReason = CloseReason.PEER_CLOSED) -> None:
        """
        End the channel, as the backend would on noticing it had gone.
        """
        self._mark_gone(reason)


class FakeAdapter(BackendAdapter):
    """
    An adapter that hands out fake channels.
    """

    def __init__(self) -> None:
        super().__init__()
        self.open_calls = 0
        self.channels: list[FakeChannel] = []

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        return [DEVICE]

    @override
    def open_service(
        self, device: RfcommDeviceInfo | str, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._open()

    @override
    def open_channel(
        self, device: RfcommDeviceInfo | str, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncGenerator[BackendChannel, None]:
        self.open_calls += 1
        channel = FakeChannel()
        self.channels.append(channel)
        try:
            yield channel
        finally:
            channel.closed = True

    # -- test controls -------------------------------------------------

    def go_away(
        self, reason: AdapterLostReason = AdapterLostReason.SWITCHED_OFF
    ) -> None:
        """
        End the adapter, as the backend would on noticing it had gone.
        """
        self._mark_gone(reason)


class FakeProvider(BackendProvider):
    """
    A provider that hands out fake adapters.
    """

    def __init__(self) -> None:
        self.adapters: list[FakeAdapter] = []

    @override
    async def list_adapters(self) -> list[RfcommAdapterInfo]:
        return [ADAPTER]

    @override
    def open_adapter(
        self, adapter: RfcommAdapterInfo | None = None
    ) -> AbstractAsyncContextManager[BackendAdapter]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncGenerator[BackendAdapter, None]:
        backend = FakeAdapter()
        self.adapters.append(backend)
        yield backend
