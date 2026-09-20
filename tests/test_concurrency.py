# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
One task per direction.

Sharing a direction between two tasks interleaves their data unpredictably.
That is a bug in the caller, and it should be reported rather than tolerated
as a race.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.errors import ChannelBusyError
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


async def test_two_receivers_is_an_error() -> None:
    async with open_channel() as (channel, _fake):
        first = asyncio.ensure_future(channel.receive())
        await asyncio.sleep(0)

        with pytest.raises(ChannelBusyError):
            await channel.receive()

        first.cancel()


async def test_two_senders_is_an_error() -> None:
    async with open_channel() as (channel, fake):
        fake.send_blocks = True
        first = asyncio.ensure_future(channel.send(b"one"))
        await asyncio.sleep(0)

        with pytest.raises(ChannelBusyError):
            await channel.send(b"two")

        first.cancel()


async def test_sending_and_receiving_at_once_is_fine() -> None:
    """
    The two directions are independent and must not block each other.
    """
    async with open_channel() as (channel, fake):
        waiting = asyncio.ensure_future(channel.receive())
        await asyncio.sleep(0)

        await channel.send(b"outbound")
        assert bytes(fake.sent) == b"outbound"

        fake.deliver(b"inbound")
        assert await waiting == b"inbound"
