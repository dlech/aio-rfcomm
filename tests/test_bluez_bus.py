# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The Linux backend over a real bus, against a stand-in for bluetoothd.

Everything from dbus-fast up is the code under test: following availability,
listing, connecting through a profile, serving, and settling how a channel
ended. See ``tests/fake_bluez.py`` for what stands in for BlueZ.
"""

import socket
import sys
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

assert not TYPE_CHECKING or sys.platform == "linux"

import pytest

pytest.importorskip("dbus_fast", reason="the Linux backend needs dbus-fast")

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.bluez import BlueZBackend
from aio_rfcomm.bluetooth import Bluetooth, _open_bluetooth
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    ConnectionFailedError,
    DeviceNotFoundError,
    ServiceNotFoundError,
)
from tests.fake_bluez import FakeBlueZ, FakeDevice, private_bus

FREYR = "00:1A:7D:DA:71:0C"
SERVICE = "539f44f8-e629-4723-bd40-9bd0d2807056"
SPP = "00001101-0000-1000-8000-00805f9b34fb"


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    """
    A private bus, standing in for the system bus.
    """
    # Not tmp_path: a socket's path has to fit in about a hundred bytes.
    with private_bus(Path("/tmp")) as address:
        monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", address)
        yield address


@pytest.fixture
async def bluez(bus: str) -> AsyncGenerator[FakeBlueZ, None]:
    """
    BlueZ running, with one adapter that is switched on.
    """
    fake = FakeBlueZ(bus)
    fake.add_adapter("hci0", FREYR)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


def _open() -> AbstractAsyncContextManager[Bluetooth]:
    return _open_bluetooth(BlueZBackend())


async def _now(bluetooth: Bluetooth) -> Availability:
    return await anext(bluetooth.watch_availability())


async def _until(bluetooth: Bluetooth, wanted: Availability) -> None:
    async for availability in bluetooth.watch_availability():
        if availability is wanted:
            return


def _info(device: FakeDevice) -> RfcommDeviceInfo:
    return RfcommDeviceInfo(device.address, device.alias, _handle=device.path)


def _listed(
    devices: list[RfcommDeviceInfo],
) -> list[tuple[str, str | None, str | None]]:
    return [(d.address, d.name, d._handle) for d in devices]


def _expected(*devices: FakeDevice) -> list[tuple[str, str | None, str | None]]:
    return [(d.address, d.alias, d.path) for d in devices]


def _receive(peer: socket.socket, count: int) -> bytes:
    peer.settimeout(5)
    return peer.recv(count)


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


async def test_an_adapter_that_is_on_is_usable(bluez: FakeBlueZ) -> None:
    async with _open() as bluetooth:
        assert await _now(bluetooth) is Availability.USABLE


async def test_switching_off_and_on_is_followed(bluez: FakeBlueZ) -> None:
    (adapter,) = bluez.adapters

    async with _open() as bluetooth:
        adapter.switch_off()
        await _until(bluetooth, Availability.SWITCHED_OFF)

        adapter.switch("off-enabling")
        adapter.switch("on")
        await _until(bluetooth, Availability.USABLE)


async def test_changes_are_followed_without_asking_bluez(bluez: FakeBlueZ) -> None:
    """
    Each change comes with a signal that carries the new values.
    """
    (adapter,) = bluez.adapters

    async with _open() as bluetooth:
        asked = len(bluez.calls)

        adapter.switch_off()
        await _until(bluetooth, Availability.SWITCHED_OFF)
        adapter.switch("on")
        await _until(bluetooth, Availability.USABLE)

        bluez.add_device(adapter, "00:11:22:33:44:55", "peer")
        await bluez.flush()
        assert len(await bluetooth.list_known_devices()) == 1

        assert bluez.calls[asked:] == []


async def test_a_change_just_after_everything_is_read_is_kept(bus: str) -> None:
    """
    The signal can be taken in before the task that asked for everything
    sees the reply, which predates it.
    """
    fake = FakeBlueZ(bus)
    adapter = fake.add_adapter("hci0", FREYR)

    def switch_off() -> None:
        adapter.switch_off()
        # Holding the loop lets the reply and the signals reach our socket
        # before it is read, so they are taken in together.
        time.sleep(0.05)

    fake.after_reading = switch_off
    await fake.start()
    try:
        async with _open() as bluetooth:
            await _until(bluetooth, Availability.SWITCHED_OFF)
    finally:
        await fake.stop()


async def test_adapters_coming_and_going_are_followed(bus: str) -> None:
    fake = FakeBlueZ(bus)
    await fake.start()
    try:
        async with _open() as bluetooth:
            assert await _now(bluetooth) is Availability.NO_ADAPTER

            adapter = fake.add_adapter("hci0", FREYR)
            await _until(bluetooth, Availability.USABLE)

            fake.remove_adapter(adapter)
            await _until(bluetooth, Availability.NO_ADAPTER)
    finally:
        await fake.stop()


async def test_bluez_starting_and_stopping_is_followed(bus: str) -> None:
    fake = FakeBlueZ(bus)
    fake.add_adapter("hci0", FREYR)
    try:
        async with _open() as bluetooth:
            assert await _now(bluetooth) is Availability.SERVICE_UNAVAILABLE

            await fake.start()
            await _until(bluetooth, Availability.USABLE)

            await fake.stop()
            await _until(bluetooth, Availability.SERVICE_UNAVAILABLE)

            await fake.start()
            await _until(bluetooth, Availability.USABLE)
    finally:
        await fake.stop()


async def test_no_system_bus_is_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp_path}/nothing")

    async with _open() as bluetooth:
        assert await _now(bluetooth) is Availability.UNSUPPORTED


async def test_the_adapter_in_use_is_kept_when_another_arrives(bus: str) -> None:
    fake = FakeBlueZ(bus)
    second = fake.add_adapter("hci1", "00:11:22:33:44:01")
    kept = fake.add_device(second, "00:11:22:33:44:55", "kept")
    await fake.start()
    try:
        async with _open() as bluetooth:
            # hci0 comes first in path order, which is what would choose it.
            first = fake.add_adapter("hci0", FREYR)
            fake.add_device(first, "00:11:22:33:44:66", "other")
            await fake.flush()

            # Only the adapter in use going off changes anything.
            second.switch_off()
            await _until(bluetooth, Availability.SWITCHED_OFF)
            second.switch("on")
            await _until(bluetooth, Availability.USABLE)

            assert _listed(await bluetooth.list_known_devices()) == _expected(kept)
    finally:
        await fake.stop()


async def test_the_next_adapter_is_used_when_the_one_in_use_goes(bus: str) -> None:
    fake = FakeBlueZ(bus)
    first = fake.add_adapter("hci0", FREYR)
    second = fake.add_adapter("hci1", "00:11:22:33:44:01", power_state="off")
    device = fake.add_device(second, "00:11:22:33:44:55", "on hci1")
    await fake.start()
    try:
        async with _open() as bluetooth:
            fake.remove_adapter(first)
            await _until(bluetooth, Availability.SWITCHED_OFF)

            second.switch("on")
            await _until(bluetooth, Availability.USABLE)
            assert _listed(await bluetooth.list_known_devices()) == _expected(device)
    finally:
        await fake.stop()


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


async def test_listing_gives_the_devices_of_the_adapter_in_use(
    bluez: FakeBlueZ,
) -> None:
    (adapter,) = bluez.adapters
    other = bluez.add_adapter("hci1", "00:11:22:33:44:01")
    # BlueZ reports UUIDs in lower case.
    spp = bluez.add_device(adapter, "00:11:22:33:44:55", "spp", [SPP])
    plain = bluez.add_device(adapter, "00:11:22:33:44:66", "plain")
    bluez.add_device(other, "00:11:22:33:44:77", "elsewhere", [SPP])

    async with _open() as bluetooth:
        assert _listed(await bluetooth.list_known_devices()) == _expected(spp, plain)
        listed = await bluetooth.list_known_devices(service=UUID(SPP.upper()))
        assert _listed(listed) == _expected(spp)
        listed = await bluetooth.list_known_devices(service=[UUID(SERVICE), UUID(SPP)])
        assert _listed(listed) == _expected(spp)
        assert await bluetooth.list_known_devices(service=UUID(SERVICE)) == []


async def test_listing_follows_devices_as_they_change(bluez: FakeBlueZ) -> None:
    (adapter,) = bluez.adapters

    async with _open() as bluetooth:
        assert await bluetooth.list_known_devices() == []

        device = bluez.add_device(adapter, "00:11:22:33:44:55", "before")
        await bluez.flush()
        assert _listed(await bluetooth.list_known_devices()) == _expected(device)

        device.rename("after")
        device.offer([SPP])
        await bluez.flush()
        listed = await bluetooth.list_known_devices(service=UUID(SPP))
        assert _listed(listed) == [(device.address, "after", device.path)]

        bluez.remove_device(device)
        await bluez.flush()
        assert await bluetooth.list_known_devices() == []


async def test_listing_after_bluez_restarts_sees_what_it_has_now(
    bluez: FakeBlueZ,
) -> None:
    (adapter,) = bluez.adapters
    gone = bluez.add_device(adapter, "00:11:22:33:44:55", "gone")

    async with _open() as bluetooth:
        await bluez.stop()
        await _until(bluetooth, Availability.SERVICE_UNAVAILABLE)

        bluez.devices.remove(gone)
        stayed = bluez.add_device(adapter, "00:11:22:33:44:66", "stayed")
        await bluez.start()
        await _until(bluetooth, Availability.USABLE)

        assert _listed(await bluetooth.list_known_devices()) == _expected(stayed)


# --------------------------------------------------------------------------
# Opening a channel to a service
# --------------------------------------------------------------------------


async def test_a_channel_carries_data_both_ways(bluez: FakeBlueZ) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer", [SERVICE])

    async with (
        _open() as bluetooth,
        bluetooth.open_service(_info(device), UUID(SERVICE)) as channel,
    ):
        (link,) = device.links
        await channel.send(b"ping")
        assert _receive(link.peer, 4) == b"ping"

        link.peer.sendall(b"pong")
        assert await channel.receive() == b"pong"


async def test_closing_a_channel_hangs_up_although_bluez_keeps_a_copy(
    bluez: FakeBlueZ,
) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer", [SERVICE])

    async with _open() as bluetooth:
        async with bluetooth.open_service(_info(device), UUID(SERVICE)):
            (link,) = device.links

        assert _receive(link.peer, 1) == b""
        # The profile was only for this channel.
        assert not bluez.registrations


async def test_a_device_named_only_by_address_is_found(bluez: FakeBlueZ) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer", [SERVICE])

    async with (
        _open() as bluetooth,
        bluetooth.open_service(RfcommDeviceInfo(device.address), UUID(SERVICE)),
    ):
        assert device.links


async def test_concurrent_channels_to_one_service_share_a_profile(
    bluez: FakeBlueZ,
) -> None:
    """
    BlueZ refuses a second registration of a UUID, so two at once would fail
    if each registered its own.
    """
    adapter = bluez.adapters[0]
    first = bluez.add_device(adapter, "00:11:22:33:44:55", "first", [SERVICE])
    second = bluez.add_device(adapter, "00:11:22:33:44:66", "second", [SERVICE])

    async with (
        _open() as bluetooth,
        bluetooth.open_service(_info(first), UUID(SERVICE)),
        bluetooth.open_service(_info(second), UUID(SERVICE)),
    ):
        assert first.links
        assert second.links
        assert len(bluez.registrations) == 1


async def test_a_service_the_device_lacks_is_not_found(bluez: FakeBlueZ) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer", [SPP])

    async with _open() as bluetooth:
        with pytest.raises(ServiceNotFoundError):
            async with bluetooth.open_service(_info(device), UUID(SERVICE)):
                pass

    assert not bluez.registrations


async def test_a_device_that_does_not_answer_is_not_found(bluez: FakeBlueZ) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer", [SERVICE])
    device.answers = False

    async with _open() as bluetooth:
        with pytest.raises(DeviceNotFoundError, match="switched off or out of range"):
            async with bluetooth.open_service(_info(device), UUID(SERVICE)):
                pass


async def test_a_device_bluez_does_not_know_is_not_found(bluez: FakeBlueZ) -> None:
    async with _open() as bluetooth:
        with pytest.raises(DeviceNotFoundError):
            async with bluetooth.open_service(
                RfcommDeviceInfo("00:11:22:33:44:55"), UUID(SERVICE)
            ):
                pass

    assert not bluez.registrations


async def test_a_service_another_program_holds_says_so(bluez: FakeBlueZ) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer", [SERVICE])
    bluez.register_elsewhere(SERVICE)

    async with _open() as bluetooth:
        with pytest.raises(ConnectionFailedError, match="another program"):
            async with bluetooth.open_service(_info(device), UUID(SERVICE)):
                pass


# --------------------------------------------------------------------------
# Serving
#
# CI's Python has no Bluetooth sockets, and there is no adapter to bind to
# anyway, so a channel is named and taken unchecked.
# --------------------------------------------------------------------------


@pytest.fixture
def no_bluetooth_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)


@pytest.mark.usefixtures("no_bluetooth_sockets")
async def test_a_served_service_is_published_and_withdrawn(bluez: FakeBlueZ) -> None:
    async with _open() as bluetooth:
        async with bluetooth.serve(UUID(SERVICE), name="echo", channel=5) as service:
            assert service.channel == 5
            registration = bluez.registrations[SERVICE]
            assert registration.options["Role"].value == "server"
            assert registration.options["Channel"].value == 5
            assert registration.options["Name"].value == "echo"

        assert not bluez.registrations


@pytest.mark.usefixtures("no_bluetooth_sockets")
async def test_a_peer_that_connects_is_accepted(bluez: FakeBlueZ) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer")

    async with (
        _open() as bluetooth,
        bluetooth.serve(UUID(SERVICE), name="echo", channel=5) as service,
    ):
        peer = await bluez.connect_from(device, SERVICE)
        request = await anext(service.connection_requests())
        assert request.info.address == device.address
        assert request.info._handle == device.path

        async with request.accept() as channel:
            peer.sendall(b"ping")
            assert await channel.receive() == b"ping"
            await channel.send(b"pong")
            assert _receive(peer, 4) == b"pong"

        assert _receive(peer, 1) == b""


@pytest.mark.usefixtures("no_bluetooth_sockets")
async def test_declining_hangs_up_on_the_peer(bluez: FakeBlueZ) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer")

    async with (
        _open() as bluetooth,
        bluetooth.serve(UUID(SERVICE), name="echo", channel=5) as service,
    ):
        peer = await bluez.connect_from(device, SERVICE)
        request = await anext(service.connection_requests())
        await request.decline()

        assert _receive(peer, 1) == b""


@pytest.mark.usefixtures("no_bluetooth_sockets")
async def test_a_peer_nobody_took_is_hung_up_when_serving_ends(
    bluez: FakeBlueZ,
) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer")

    async with _open() as bluetooth:
        async with bluetooth.serve(UUID(SERVICE), name="echo", channel=5):
            peer = await bluez.connect_from(device, SERVICE)

        assert _receive(peer, 1) == b""


@pytest.mark.usefixtures("no_bluetooth_sockets")
async def test_a_service_another_program_serves_says_so(bluez: FakeBlueZ) -> None:
    bluez.register_elsewhere(SERVICE)

    async with _open() as bluetooth:
        with pytest.raises(ConnectionFailedError, match="another program"):
            async with bluetooth.serve(UUID(SERVICE), name="echo", channel=5):
                pass


# --------------------------------------------------------------------------
# How a channel ended
# --------------------------------------------------------------------------


async def test_a_radio_going_off_ends_the_channel_with_bluetooth(
    bluez: FakeBlueZ,
) -> None:
    """
    Measured on BlueZ 5.85: the channel ends about 50 ms before the adapter is
    reported off, while its power state already reads as going off.
    """
    (adapter,) = bluez.adapters
    device = bluez.add_device(adapter, "00:11:22:33:44:55", "peer", [SERVICE])

    async with (
        _open() as bluetooth,
        bluetooth.open_service(_info(device), UUID(SERVICE)) as channel,
    ):
        (link,) = device.links
        asked = len(bluez.calls)
        # Read, but not yet announced.
        adapter.power_state = "on-disabling"
        link.drop()

        with pytest.raises(BluetoothUnavailableError) as raised:
            await channel.receive()

        assert raised.value.reason is Availability.SWITCHED_OFF
        # Only the adapter in use is read again.
        assert bluez.calls[asked:] == ["org.freedesktop.DBus.Properties.GetAll"]


async def test_a_hang_up_with_the_radio_on_is_the_connection_s_end(
    bluez: FakeBlueZ,
) -> None:
    device = bluez.add_device(bluez.adapters[0], "00:11:22:33:44:55", "peer", [SERVICE])

    async with (
        _open() as bluetooth,
        bluetooth.open_service(_info(device), UUID(SERVICE)) as channel,
    ):
        (link,) = device.links
        link.peer.close()

        with pytest.raises(ChannelClosedError):
            await channel.receive()

        assert await _now(bluetooth) is Availability.USABLE
