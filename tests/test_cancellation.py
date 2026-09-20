# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Cancellation is ordinary control flow, so every operation has to survive it.

The dangerous case is silent damage: bytes that were taken off the wire and
then dropped because the call awaiting them was cancelled, or a half-finished
send that leaves the caller believing nothing happened.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.channel import RfcommChannel
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


async def test_cancelled_receive_does_not_lose_data() -> None:
    """
    Bytes must not disappear because the call waiting for them was cancelled.

    A backend whose receive consumes data before it can be handed back would
    silently drop it, which is the hardest kind of bug to find later.
    """
    async with open_channel() as (channel, fake):
        waiting = asyncio.ensure_future(channel.receive())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        fake.deliver(b"still here")
        assert await channel.receive() == b"still here"


async def test_data_already_queued_survives_a_cancelled_receive() -> None:
    """
    The same, with the data already waiting when the cancel lands.
    """
    async with open_channel() as (channel, fake):
        fake.deliver(b"arrived first")
        waiting = asyncio.ensure_future(channel.receive())
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        assert await channel.receive() == b"arrived first"


async def test_cancelling_the_scope_still_closes_the_channel() -> None:
    """
    Tearing down under cancellation must not leak the channel.
    """
    backend = FakeAdapter()
    handle = RfcommAdapter(backend)
    try:

        async def work() -> None:
            device = handle.use_device((await handle.list_known_devices())[0])
            async with device.open_channel(1):
                await asyncio.Event().wait()

        task = asyncio.ensure_future(work())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert backend.channels[-1].closed
    finally:
        handle._close()
