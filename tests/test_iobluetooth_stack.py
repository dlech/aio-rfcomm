# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS backend end to end, with only the framework stood in for.

The library and its helper are otherwise tested on either side of the socket
between them, which cannot show that the two halves agree. Here both run in
this process: the public API over the real adapter and helper client on one
end of a socket pair, the helper's own request loop and the native layer on
the other, and the fake framework beneath that. Only spawning the helper is
left out, since the child would not have the fake.
"""

import asyncio
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.backend.iobluetooth import IOBluetoothAdapter
from aio_rfcomm.backend.iobluetooth import __main__ as helper_main
from aio_rfcomm.backend.iobluetooth._codes import CBManagerState
from aio_rfcomm.backend.iobluetooth._helper import Helper
from aio_rfcomm.backend.iobluetooth._protocol import Link
from aio_rfcomm.discovery import RfcommAdapterInfo
from aio_rfcomm.errors import (
    AdapterLostError,
    AdapterLostReason,
    ChannelClosedError,
    CloseReason,
    DeviceNotFoundError,
)
from tests.fake_iobluetooth import (
    FakeDeviceClass,
    change_state,
    clean_state,  # noqa: F401 -- an autouse fixture, used by being imported
    fake_framework,
    opened,
    until_observed,
)

ADDRESS = "00:1A:7D:DA:71:0C"


@asynccontextmanager
async def _stack() -> AsyncGenerator[RfcommAdapter, None]:
    """
    An adapter whose helper runs in this process, over the fake framework.

    Yields:
        The adapter, as a caller of the library holds it.
    """
    near, far = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    helper = Helper(Link(near))
    serving = asyncio.ensure_future(helper_main._serve(far))
    reading = asyncio.ensure_future(helper.deliver())
    try:
        yield RfcommAdapter(
            IOBluetoothAdapter(helper, RfcommAdapterInfo("default", None, None))
        )
    finally:
        # What the adapter's scope ends with: the library closes its end, and
        # the helper reads end of file and stops.
        helper._link.close()
        async with asyncio.timeout(1):
            await serving
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)


async def test_bytes_cross_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_framework(monkeypatch)
    async with (
        _stack() as adapter,
        adapter.use_device(ADDRESS).open_channel(5) as channel,
    ):
        fake = opened[-1]

        await channel.send(b"hello\n")
        # The bytes cross the helper before they reach the framework.
        await asyncio.sleep(0.01)
        assert bytes(fake.written) == b"hello\n"

        fake.arrive(b"ch5:hello\n")
        async with asyncio.timeout(1):
            assert await channel.receive() == b"ch5:hello\n"


async def test_a_peer_that_hangs_up_ends_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured against a peer that drops an idle connection: the channel is
    reported closed while the link stays up, which is an orderly end.
    """
    fake_framework(monkeypatch)
    async with (
        _stack() as adapter,
        adapter.use_device(ADDRESS).open_channel(5) as channel,
    ):
        opened[-1].hang_up()
        async with asyncio.timeout(1):
            assert await channel.receive() == b""
            assert await channel.wait_until_gone() is CloseReason.PEER_CLOSED


async def test_a_lost_link_is_not_mistaken_for_a_hang_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework(monkeypatch)
    async with (
        _stack() as adapter,
        adapter.use_device(ADDRESS).open_channel(5) as channel,
    ):
        fake = opened[-1]
        fake.device.connected = False
        fake.hang_up()
        async with asyncio.timeout(1):
            with pytest.raises(ChannelClosedError) as caught:
                await channel.receive()
        assert caught.value.reason is CloseReason.LINK_LOST


async def test_a_closed_channel_leaves_the_device_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Found only by running the library the way a caller does: closing returned
    while the helper still held the device, and the next open was refused.
    """
    fake_framework(monkeypatch)
    async with _stack() as adapter:
        device = adapter.use_device(ADDRESS)
        for _round in range(3):
            async with device.open_channel(5) as channel:
                await channel.send(b"x")
        channels = FakeDeviceClass.made[ADDRESS].channels
        assert len(channels) == 3
        assert not any(c.open for c in channels)


async def test_a_device_that_does_not_answer_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The page is accepted and its timeout reported later, as a CoreBluetooth
    error code; that has to arrive here as the device not answering.
    """
    fake_framework(monkeypatch, answers=False, connected=False)
    async with _stack() as adapter:
        with pytest.raises(DeviceNotFoundError, match="did not answer"):
            async with adapter.use_device(ADDRESS).open_channel(5):
                pass


async def test_a_radio_switched_off_ends_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    What a server waiting on nothing but the adapter needs: told, rather than
    left waiting on a radio that is no longer there.
    """
    manager = fake_framework(monkeypatch)
    async with _stack() as adapter:
        with pytest.raises(AdapterLostError) as caught:
            async with asyncio.timeout(1), adapter.fail_when_gone():
                await until_observed(manager)
                change_state(manager, CBManagerState.POWERED_OFF)
                await asyncio.Event().wait()
        assert caught.value.reason is AdapterLostReason.SWITCHED_OFF


async def test_a_radio_switched_off_with_a_channel_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on a clean toggle: the manager says powered off, and a
    millisecond later the channel hears its link drop.
    """
    manager = fake_framework(monkeypatch)
    async with (
        _stack() as adapter,
        adapter.use_device(ADDRESS).open_channel(5) as channel,
    ):
        fake = opened[-1]
        await until_observed(manager)
        change_state(manager, CBManagerState.POWERED_OFF)
        fake.device.connected = False
        fake.hang_up()
        async with asyncio.timeout(1):
            with pytest.raises(ChannelClosedError) as closed:
                await channel.receive()
            assert closed.value.reason is CloseReason.LINK_LOST
            assert await adapter.wait_until_gone() is AdapterLostReason.SWITCHED_OFF
