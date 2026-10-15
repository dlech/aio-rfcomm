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

from aio_rfcomm.bluetooth import Bluetooth
from tests.fake import DEVICE, FakeProvider


async def test_cancelling_the_scope_still_closes_the_channel(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    """
    Tearing down under cancellation must not leak the channel.
    """

    async def work() -> None:
        async with bluetooth.open_channel(DEVICE, 1):
            await asyncio.Event().wait()

    task = asyncio.ensure_future(work())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.backend.channels[-1].closed
