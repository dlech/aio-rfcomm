# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Stopping a block when the adapter underneath it goes away.

This is for work that would otherwise sit idle and never notice, and the two
things it must not do are hang when nothing goes wrong, or bury the cause in
an exception group.
"""

import asyncio

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.errors import (
    AdapterLostError,
    AdapterLostReason,
)
from tests.fake import FakeAdapter


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
