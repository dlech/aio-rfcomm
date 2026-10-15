# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Cancellation is ordinary control flow, so every operation has to survive it.

The dangerous case is silent damage: a channel left open because the block
that owned it was cancelled. Receiving under cancellation is tested against
the real stream channel, in test_stream.py.
"""

import asyncio

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from tests.fake import FakeAdapter


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
