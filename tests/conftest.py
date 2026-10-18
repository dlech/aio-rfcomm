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
from typing import TYPE_CHECKING

import pytest

from aio_rfcomm.bluetooth import Bluetooth, _open_bluetooth
from aio_rfcomm.channel import RfcommChannel
from tests.fake import DEVICE, FakeChannel, FakeProvider

if TYPE_CHECKING:
    from tests.fake_iobluetooth import FakeManager


@pytest.fixture
def provider() -> FakeProvider:
    """
    A fake backend, for driving events by hand.
    """
    return FakeProvider()


@pytest.fixture
async def bluetooth(provider: FakeProvider) -> AsyncGenerator[Bluetooth, None]:
    """
    Bluetooth opened over the fake backend.
    """
    async with _open_bluetooth(provider) as opened:
        yield opened


@pytest.fixture
async def opened(
    bluetooth: Bluetooth, provider: FakeProvider
) -> AsyncGenerator[tuple[RfcommChannel, FakeChannel], None]:
    """
    An open channel, paired with the fake backing it.
    """
    async with bluetooth.open_channel(DEVICE, 1) as channel:
        yield channel, provider.backend.channels[-1]


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> "FakeManager":
    """
    A fake CoreBluetooth manager, for the macOS layer's tests.
    """
    # Imported here, because the fakes need rubicon, which only macOS has.
    from tests.fake_iobluetooth import fake_manager

    return fake_manager(monkeypatch)
