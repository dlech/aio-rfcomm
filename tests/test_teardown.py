# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Stopping a block when the resource underneath it goes away.

Pending sends and receives fail on their own. This is for work that would
otherwise sit idle and never notice, and the two things it must not do are
hang when nothing goes wrong, or bury the cause in an exception group.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.errors import (
    AdapterLostError,
    AdapterLostReason,
    ChannelClosedError,
    CloseReason,
)
from tests.fake import FakeAdapter, FakeChannel


@asynccontextmanager
async def open_channel() -> AsyncGenerator[tuple[RfcommChannel, FakeChannel], None]:
    backend = FakeAdapter()
    handle = RfcommAdapter(backend)
    try:
        device = handle.use_device((await handle.list_known_devices())[0])
        async with device.open_channel(1) as channel:
            yield channel, backend.channels[-1]
    finally:
        handle._close()


async def test_healthy_channel_does_not_hang_the_block() -> None:
    """
    A watcher that never fires must not keep the block from finishing.

    This is the trap that makes a bare watcher task unusable: a task group
    waits for every child, so one that never completes stalls it forever.
    """
    async with open_channel() as (channel, _fake), channel.fail_when_gone():
        await asyncio.sleep(0)


@pytest.mark.parametrize("reason", list(CloseReason))
async def test_every_reason_stops_the_block(reason: CloseReason) -> None:
    async with open_channel() as (channel, fake):
        with pytest.raises(ChannelClosedError) as caught:
            async with channel.fail_when_gone():
                fake.go_away(reason)
                await asyncio.sleep(10)

    assert caught.value.reason is reason


async def test_the_cause_is_not_buried_in_an_exception_group() -> None:
    """
    One failure must arrive as itself, so callers need no ``except*``.
    """
    async with open_channel() as (channel, fake):
        with pytest.raises(ChannelClosedError):
            async with channel.fail_when_gone():
                fake.go_away(CloseReason.LINK_LOST)
                await asyncio.sleep(10)


async def test_the_body_s_own_error_passes_through() -> None:
    async with open_channel() as (channel, _fake):
        with pytest.raises(ValueError, match="from the body"):
            async with channel.fail_when_gone():
                raise ValueError("from the body")


async def test_a_lost_adapter_stops_the_block() -> None:
    backend = FakeAdapter()
    handle = RfcommAdapter(backend)
    try:
        with pytest.raises(AdapterLostError) as caught:
            async with handle.fail_when_gone():
                backend.go_away(AdapterLostReason.REMOVED)
                await asyncio.sleep(10)

        assert caught.value.reason is AdapterLostReason.REMOVED
    finally:
        handle._close()
