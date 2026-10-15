# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Handles must not outlive the scope that produced them.

Structured concurrency is only worth something if a handle stops working once
its owning scope is gone. A stale handle that still half-works is worse than
one that raises, because the failure then surfaces somewhere else entirely.
"""

from uuid import UUID

import pytest

from aio_rfcomm.bluetooth import Bluetooth, _open_bluetooth
from aio_rfcomm.errors import ScopeClosedError
from tests.fake import DEVICE, FakeProvider


async def test_bluetooth_refuses_everything_after_its_block() -> None:
    async with _open_bluetooth(FakeProvider()) as bluetooth:
        pass

    with pytest.raises(ScopeClosedError):
        await bluetooth.list_known_devices()
    with pytest.raises(ScopeClosedError):
        bluetooth.open_channel(DEVICE, 1)
    with pytest.raises(ScopeClosedError):
        bluetooth.serve(UUID(int=1), name="late")
    with pytest.raises(ScopeClosedError):
        bluetooth.watch_availability()


async def test_a_watcher_refuses_after_bluetooth_s_block() -> None:
    async with _open_bluetooth(FakeProvider()) as bluetooth:
        watcher = bluetooth.watch_availability()

    with pytest.raises(ScopeClosedError):
        await anext(watcher)


async def test_channel_refuses_to_send_after_its_scope_exits(
    bluetooth: Bluetooth,
) -> None:
    async with bluetooth.open_channel(DEVICE, 1) as channel:
        await channel.send(b"fine")

    with pytest.raises(ScopeClosedError):
        await channel.send(b"too late")


async def test_channel_refuses_to_receive_after_its_scope_exits(
    bluetooth: Bluetooth,
) -> None:
    async with bluetooth.open_channel(DEVICE, 1) as channel:
        pass

    with pytest.raises(ScopeClosedError):
        await channel.receive()


async def test_channel_is_closed_when_its_scope_exits(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.open_channel(DEVICE, 1):
        assert not provider.backend.channels[-1].closed

    assert provider.backend.channels[-1].closed


async def test_a_service_refuses_requests_after_its_block(
    bluetooth: Bluetooth,
) -> None:
    async with bluetooth.serve(UUID(int=1), name="x") as service:
        requests = service.connection_requests()

    with pytest.raises(ScopeClosedError):
        await anext(requests)
    with pytest.raises(ScopeClosedError):
        service.connection_requests()
