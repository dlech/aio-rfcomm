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

# Measurements cited in this module were made on macOS 26.6.2 (25G83), with a
# BCM_4387 controller on firmware 23.5.636.5345, unless a claim says otherwise.
# The peers were a Linux machine, on BlueZ 5.72 until 2026-10-05 and BlueZ 5.85
# after, and a Pybricks EV3 on firmware 4.1.0b5. Claims that depend on the
# peer name its version.

import asyncio
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

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
    ChannelInUseError,
    CloseReason,
    DeviceNotFoundError,
)
from aio_rfcomm.service import RfcommConnectionRequest, RfcommService
from tests.fake_iobluetooth import (
    FakeDeviceClass,
    FakeManager,
    change_state,
    clean_state,  # noqa: F401 -- an autouse fixture, used by being imported
    fake_framework,
    fake_framework_parts,
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


async def test_what_arrives_as_the_peer_hangs_up_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The last bytes and the hang-up can arrive together while the helper is
    still waiting to pass on what came before. The bytes go first.
    """
    fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        fake = opened[-1]
        # More than every buffer between here and the helper holds.
        fake.arrive(bytes(4 * 1024 * 1024))
        await asyncio.sleep(0.05)
        fake.arrive(b"tail")
        fake.hang_up()

        received = bytearray()
        async with asyncio.timeout(5):
            with pytest.raises(ChannelClosedError):
                while True:
                    received += await channel.receive()

        assert len(received) == 4 * 1024 * 1024 + 4
        assert received.endswith(b"tail")


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
    error code. That has to arrive here as the device not answering.
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
        with pytest.raises(BluetoothUnavailableError, match="Turn it on"):
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
    # Measured when bluetoothd restarted on its way to powering off. Here it
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


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

CHAT = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")


async def _next_request(service: RfcommService) -> RfcommConnectionRequest:
    async with asyncio.timeout(1):
        return await anext(aiter(service.connection_requests()))


async def test_a_peer_that_connects_is_talked_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, bluetooth.serve(CHAT, name="chat") as service:
        fake = framework.channels.connect_in(ADDRESS, service.channel)
        request = await _next_request(service)
        assert request.info.address == ADDRESS

        async with request.accept() as channel:
            fake.arrive(b"hello\n")
            async with asyncio.timeout(1):
                assert await channel.receive() == b"hello\n"
            await channel.send(b"echo\n")
            await asyncio.sleep(0.01)
            assert bytes(fake.written) == b"echo\n"

            fake.hang_up()
            with pytest.raises(ChannelClosedError) as caught:
                async with asyncio.timeout(1):
                    await channel.receive()
            assert caught.value.reason is CloseReason.PEER_CLOSED


async def test_a_declined_peer_is_hung_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, bluetooth.serve(CHAT, name="chat") as service:
        fake = framework.channels.connect_in(ADDRESS, service.channel)
        request = await _next_request(service)
        await request.decline()
        async with asyncio.timeout(1):
            while fake.open:
                await asyncio.sleep(0.01)


async def test_each_service_has_a_helper_of_its_own_and_registers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    bluetoothd frees a registration only when the process that made it ends,
    and a second from one process leaks the first's slot -- measured.
    """
    framework = fake_framework_parts(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth:
        for _ in range(2):
            async with bluetooth.serve(CHAT, name="chat"):
                pass
        assert len(helpers.started) == 3
        assert len(framework.channels.registrations) == 2
        # Withdrawn with nothing open, each service's helper has gone.
        async with asyncio.timeout(1):
            while not all(h.stopped for h, _far in helpers.started[1:]):
                await asyncio.sleep(0.01)
    assert all(p.withdrawn for p in framework.records.published)


async def test_an_accepted_channel_outlives_the_service_and_its_helper_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth, AsyncExitStack() as stack:
        async with bluetooth.serve(CHAT, name="chat") as service:
            fake = framework.channels.connect_in(ADDRESS, service.channel)
            request = await _next_request(service)
            channel = await stack.enter_async_context(request.accept())

        serving, _far = helpers.started[-1]
        assert framework.records.published[-1].withdrawn
        await asyncio.sleep(0.05)
        assert not serving.stopped

        fake.arrive(b"still here")
        async with asyncio.timeout(1):
            assert await channel.receive() == b"still here"

        await stack.aclose()
        async with asyncio.timeout(1):
            while not serving.stopped:
                await asyncio.sleep(0.01)
        assert not fake.open


async def test_a_channel_already_served_here_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth:
        with pytest.raises(ChannelInUseError):
            async with bluetooth.serve(CHAT, name="chat", channel=8):
                pass


async def test_a_peer_still_waiting_at_withdrawal_is_hung_up_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The helper lives on for the channel already accepted, so a peer queued
    behind it would otherwise stay connected, unread, for as long as it lasts.
    """
    framework = fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, AsyncExitStack() as stack:
        async with bluetooth.serve(CHAT, name="chat") as service:
            framework.channels.connect_in(ADDRESS, service.channel)
            request = await _next_request(service)
            await stack.enter_async_context(request.accept())
            waiting = framework.channels.connect_in(ADDRESS, service.channel)
            await asyncio.sleep(0.05)

        async with asyncio.timeout(1):
            while waiting.open:
                await asyncio.sleep(0.01)


async def test_a_service_given_up_on_while_its_helper_starts_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Starting can wait on a permission dialog for as long as the user leaves it.
    """
    fake_framework(monkeypatch)
    helpers = _Helpers()
    starting = asyncio.Event()
    abandoned = asyncio.Event()

    @asynccontextmanager
    async def slow_after_first() -> AsyncGenerator[Helper, None]:
        if helpers.started:
            starting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                abandoned.set()
                raise
        async with helpers.start() as helper:
            yield helper

    async with _open_bluetooth(IOBluetoothBackend(slow_after_first)) as bluetooth:
        serving = asyncio.ensure_future(bluetooth.serve(CHAT, name="chat").__aenter__())
        async with asyncio.timeout(1):
            await starting.wait()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        async with asyncio.timeout(1):
            await abandoned.wait()


async def test_two_services_at_once_are_on_channels_of_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    async with (
        _stack() as bluetooth,
        bluetooth.serve(CHAT, name="chat") as one,
        bluetooth.serve(CHAT, name="chat") as other,
    ):
        assert one.channel != other.channel


async def test_two_services_started_together_choose_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Each chooses in a process of its own, where the other's record may not
    be published yet.
    """
    fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, AsyncExitStack() as stack:
        one, other = await asyncio.gather(
            stack.enter_async_context(bluetooth.serve(CHAT, name="chat")),
            stack.enter_async_context(bluetooth.serve(CHAT, name="chat")),
        )
        assert one.channel != other.channel
