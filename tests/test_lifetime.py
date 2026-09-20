# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Handles must not outlive the scope that produced them.

Structured concurrency is only worth something if a handle stops working once
its owning scope is gone. A stale handle that still half-works is worse than
one that raises, because the failure then surfaces somewhere else entirely.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.errors import ScopeClosedError
from tests.fake import FakeAdapter


@asynccontextmanager
async def adapter_scope() -> AsyncGenerator[tuple[RfcommAdapter, FakeAdapter], None]:
    """
    An adapter and the fake behind it, closed the way the real opener closes
    it.
    """
    backend = FakeAdapter()
    handle = RfcommAdapter(backend)
    try:
        yield handle, backend
    finally:
        handle._close()
        backend.go_away()


async def test_device_refuses_to_open_after_its_adapter_closes() -> None:
    async with adapter_scope() as (adapter, _backend):
        device = adapter.use_device((await adapter.list_known_devices())[0])

    with pytest.raises(ScopeClosedError):
        async with device.open_channel(1):
            pass


async def test_use_device_refuses_after_its_adapter_closes() -> None:
    async with adapter_scope() as (adapter, _backend):
        device = adapter.use_device("00:11:22:33:44:55")

    with pytest.raises(ScopeClosedError):
        async with device.open_service(None):  # type: ignore[arg-type]
            pass


async def test_adapter_refuses_to_list_after_it_closes() -> None:
    async with adapter_scope() as (adapter, _backend):
        pass

    with pytest.raises(ScopeClosedError):
        await adapter.list_known_devices()


async def test_channel_refuses_to_send_after_its_scope_exits() -> None:
    async with adapter_scope() as (adapter, _backend):
        device = adapter.use_device((await adapter.list_known_devices())[0])

        async with device.open_channel(1) as channel:
            await channel.send(b"fine")

        with pytest.raises(ScopeClosedError):
            await channel.send(b"too late")


async def test_channel_refuses_to_receive_after_its_scope_exits() -> None:
    async with adapter_scope() as (adapter, _backend):
        device = adapter.use_device((await adapter.list_known_devices())[0])

        async with device.open_channel(1) as channel:
            pass

        with pytest.raises(ScopeClosedError):
            await channel.receive()


async def test_channel_is_closed_when_its_scope_exits() -> None:
    async with adapter_scope() as (adapter, _backend):
        device = adapter.use_device((await adapter.list_known_devices())[0])

        async with device.open_channel(1) as channel:
            assert isinstance(channel, RfcommChannel)

        assert _backend.channels[-1].closed
