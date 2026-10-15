# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS backend end to end, with only the framework stood in for.

The library and its helper are otherwise tested on either side of the socket
between them, which cannot show that the two halves agree. Here both run in
this process: the public API over the real backend and helper client on one
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

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth import IOBluetoothBackend, IOBluetoothBluetooth
from aio_rfcomm.backend.iobluetooth import __main__ as helper_main
from aio_rfcomm.backend.iobluetooth._codes import CBManagerState
from aio_rfcomm.backend.iobluetooth._helper import Helper, HelperFailedError
from aio_rfcomm.backend.iobluetooth._protocol import Link
from aio_rfcomm.bluetooth import Bluetooth, _open_bluetooth
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    CloseReason,
    DeviceNotFoundError,
)
from tests.fake_iobluetooth import (
    FakeDeviceClass,
    FakeManager,
    change_state,
    clean_state,  # noqa: F401 -- an autouse fixture, used by being imported
    fake_framework,
    opened,
    until_observed,
)

ADDRESS = "00:1A:7D:DA:71:0C"
DEVICE = RfcommDeviceInfo(ADDRESS)


class _Helpers:
    """
    Starts helpers in this process, the way the backend starts real ones.
    """

    def __init__(self) -> None:
        self.started: list[tuple[Helper, socket.socket]] = []

    @asynccontextmanager
    async def start(self) -> AsyncGenerator[Helper, None]:
        near, far = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        helper = Helper(Link(near))
        self.started.append((helper, far))
        serving = asyncio.ensure_future(helper_main._serve(far))
        reading = asyncio.ensure_future(helper.deliver())
        try:
            yield helper
        finally:
            # What run_helper ends with: whatever still waits is released, and
            # the helper reads end of file and stops.
            helper._stop(HelperFailedError("the helper was stopped"))
            helper._link.close()
            async with asyncio.timeout(1):
                await asyncio.gather(serving, return_exceptions=True)
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)

    def kill(self) -> None:
        """
        End the running helper without warning, as a crash would.
        """
        _helper, far = self.started[-1]
        far.shutdown(socket.SHUT_RDWR)


@asynccontextmanager
async def _stack(helpers: _Helpers | None = None) -> AsyncGenerator[Bluetooth, None]:
    """
    Bluetooth whose helpers run in this process, over the fake framework.

    Yields:
        The Bluetooth object, as a caller of the library holds it.
    """
    helpers = helpers or _Helpers()
    async with _open_bluetooth(IOBluetoothBackend(helpers.start)) as bluetooth:
        yield bluetooth


async def _next_availability(bluetooth: Bluetooth, wanted: Availability) -> None:
    async with asyncio.timeout(1):
        async for availability in bluetooth.watch_availability():
            if availability is wanted:
                return


async def test_bytes_cross_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
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
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        opened[-1].hang_up()
        async with asyncio.timeout(1):
            with pytest.raises(ChannelClosedError) as caught:
                await channel.receive()
        assert caught.value.reason is CloseReason.PEER_CLOSED


async def test_a_lost_link_is_not_mistaken_for_a_hang_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
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
    async with _stack() as bluetooth:
        for _round in range(3):
            async with bluetooth.open_channel(DEVICE, 5) as channel:
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
    async with _stack() as bluetooth:
        with pytest.raises(DeviceNotFoundError, match="did not answer"):
            async with bluetooth.open_channel(DEVICE, 5):
                pass


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


async def test_the_first_reading_is_the_manager_s_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework(monkeypatch, state=CBManagerState.POWERED_OFF)
    async with _stack() as bluetooth:
        assert await anext(bluetooth.watch_availability()) is Availability.SWITCHED_OFF
        with pytest.raises(BluetoothUnavailableError, match="turn it on"):
            async with bluetooth.open_channel(DEVICE, 5):
                pass


async def test_a_radio_switched_off_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = fake_framework(monkeypatch)
    async with _stack() as bluetooth:
        await until_observed(manager)
        change_state(manager, CBManagerState.POWERED_OFF)
        await _next_availability(bluetooth, Availability.SWITCHED_OFF)

        change_state(manager, CBManagerState.POWERED_ON)
        await _next_availability(bluetooth, Availability.USABLE)


async def test_a_radio_switched_off_with_a_channel_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on a clean toggle: the manager says powered off, and a
    millisecond later the channel hears its link drop. The radio is why.
    """
    manager = fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        fake = opened[-1]
        await until_observed(manager)
        change_state(manager, CBManagerState.POWERED_OFF)
        fake.device.connected = False
        fake.hang_up()
        async with asyncio.timeout(1):
            with pytest.raises(BluetoothUnavailableError) as caught:
                await channel.receive()
        assert caught.value.reason is Availability.SWITCHED_OFF


async def test_an_open_the_helper_refuses_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The helper checks the radio itself before opening, and the reason has to
    survive the trip back across the socket.
    """
    fake_framework(monkeypatch, state=CBManagerState.POWERED_OFF)
    helpers = _Helpers()
    async with helpers.start() as helper:
        bluetooth = IOBluetoothBluetooth()
        bluetooth.helper = helper
        with pytest.raises(BluetoothUnavailableError) as caught:
            async with asyncio.timeout(1), bluetooth.open_channel(DEVICE, 5):
                pass
    assert caught.value.reason is Availability.SWITCHED_OFF


# --------------------------------------------------------------------------
# The Bluetooth service restarting
# --------------------------------------------------------------------------


async def _restart(manager: FakeManager) -> None:
    await until_observed(manager)
    # Measured when bluetoothd restarted on its way to powering off; here it
    # comes back on.
    change_state(manager, CBManagerState.RESETTING, CBManagerState.POWERED_ON)


async def test_a_restart_brings_a_new_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    After bluetoothd restarts, every device object in the old helper stays
    bound to the old daemon, so only a new helper recovers.
    """
    manager = fake_framework(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.USABLE

        await _restart(manager)
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE
            assert await anext(watcher) is Availability.USABLE
        assert len(helpers.started) == 2

        async with bluetooth.open_channel(DEVICE, 5) as channel:
            await channel.send(b"through the new helper")


async def test_a_restart_ends_the_channels_it_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured: after a restart a channel is never told, and writes to it fail
    quietly. It has to be ended from here.
    """
    manager = fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        await _restart(manager)
        async with asyncio.timeout(1):
            with pytest.raises(BluetoothUnavailableError) as caught:
                await channel.receive()
        assert caught.value.reason is Availability.SERVICE_UNAVAILABLE


async def test_a_helper_that_stops_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.USABLE

        helpers.kill()
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE
            assert await anext(watcher) is Availability.USABLE
        assert len(helpers.started) == 2


async def test_a_first_helper_that_will_not_start_is_an_error() -> None:
    @asynccontextmanager
    async def broken() -> AsyncGenerator[Helper, None]:
        raise HelperFailedError("the helper stopped during startup")
        yield  # pragma: no cover

    with pytest.raises(HelperFailedError):
        async with _open_bluetooth(IOBluetoothBackend(broken)):
            pass


async def test_a_helper_refused_bluetooth_is_not_allowed() -> None:
    """
    macOS ends a helper it will not let use Bluetooth, and would end a new one
    the same way, so nothing is retried and the reason is reported.
    """

    @asynccontextmanager
    async def refused() -> AsyncGenerator[Helper, None]:
        raise BluetoothUnavailableError(
            Availability.NOT_ALLOWED, "call prompt_under_name() first"
        )
        yield  # pragma: no cover

    async with _open_bluetooth(IOBluetoothBackend(refused)) as bluetooth:
        assert await anext(bluetooth.watch_availability()) is Availability.NOT_ALLOWED
        with pytest.raises(BluetoothUnavailableError, match="prompt_under_name"):
            await bluetooth.list_known_devices()


async def test_a_replacement_that_will_not_start_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Retrying at once would only fail again, as fast as it could.
    """
    fake_framework(monkeypatch)
    helpers = _Helpers()
    attempts = 0

    @asynccontextmanager
    async def first_only() -> AsyncGenerator[Helper, None]:
        nonlocal attempts
        attempts += 1
        if helpers.started:
            raise HelperFailedError("could not start the macOS Bluetooth helper")
        async with helpers.start() as helper:
            yield helper

    async with _open_bluetooth(IOBluetoothBackend(first_only)) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.USABLE

        helpers.kill()
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE
            # Said once the replacement has failed, a moment later.
            while True:
                with pytest.raises(BluetoothUnavailableError) as caught:
                    await bluetooth.list_known_devices()
                if "could not be restarted" in str(caught.value):
                    break
                await asyncio.sleep(0.01)

        await asyncio.sleep(0.05)
    assert attempts == 2
