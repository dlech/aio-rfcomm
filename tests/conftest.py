# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Fixtures wiring the fake backend into the public API.

What a test may stand in for is only what CI cannot have: Bluetooth hardware,
and the operating system frameworks that need it. Our own functions, methods
and classes are never replaced -- a test that patches one is testing the
patch. Where a platform layer has to be exercised, the stand-in goes at the
boundary with the OS, so everything from there up is the code under test.

Process-wide state is cleared rather than substituted. The macOS layer cannot
be reloaded to get it back, because the Objective-C runtime registers classes
by name and refuses a second one.
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
