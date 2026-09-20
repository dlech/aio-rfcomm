# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Fixtures wiring the fake backend into the public API.
"""

from collections.abc import AsyncGenerator

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.device import RfcommDevice
from tests.fake import FakeAdapter, FakeChannel


@pytest.fixture
async def backend() -> FakeAdapter:
    """
    A fake backend adapter, for driving events by hand.
    """
    return FakeAdapter()


@pytest.fixture
async def adapter(backend: FakeAdapter) -> RfcommAdapter:
    """
    An open adapter over the fake backend.
    """
    return RfcommAdapter(backend)


@pytest.fixture
async def device(adapter: RfcommAdapter) -> RfcommDevice:
    """
    A device found through the fake adapter, ready to use.
    """
    known = await adapter.list_known_devices()
    return adapter.use_device(known[0])


@pytest.fixture
async def opened(
    device: RfcommDevice, backend: FakeAdapter
) -> AsyncGenerator[tuple[RfcommChannel, FakeChannel], None]:
    """
    An open channel, paired with the fake backing it.
    """
    async with device.open_channel(1) as channel:
        yield channel, backend.channels[-1]
