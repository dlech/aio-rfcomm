# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The Windows backend, with WinRT and Winsock stood in for.

Addresses cross the boundary between WinRT and Winsock twice -- WinRT counts
in 48-bit integers and sockets are addressed by text -- so getting that
conversion wrong would reach a different device, or none. Then come WinRT's
errors turned into ours, and last the backend driven whole through the stand-ins
in tests/fake_windows.py.
"""

import pytest

pytest.importorskip(
    "winrt.windows.devices.bluetooth", reason="the Windows backend needs WinRT"
)

import asyncio
import socket
import threading
import uuid
from collections.abc import AsyncGenerator, Generator

from winrt.windows.devices.bluetooth import BluetoothError
from winrt.windows.devices.enumeration import DeviceWatcherStatus
from winrt.windows.devices.radios import RadioState

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.windows import WindowsBackend, _each
from aio_rfcomm.backend.windows.addresses import format_address, parse_address
from aio_rfcomm.backend.windows.availability import _availability_of
from aio_rfcomm.backend.windows.errors import (
    translate_bluetooth_error,
    translate_failure,
    translate_refusal,
)
from aio_rfcomm.bluetooth import Bluetooth, _open_bluetooth
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    ChannelInUseError,
    ConnectionFailedError,
    DeviceNotFoundError,
    ServiceNotFoundError,
)
from tests.fake_windows import (
    WSAEHOSTDOWN,
    WSAETIMEDOUT,
    FakePeer,
    FakeWindows,
    protocol_list,
    winsock_error,
)

SERVICE = uuid.UUID("539f44f8-e629-4723-bd40-9bd0d2807056")


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------


def test_an_address_is_reported_colon_separated_and_upper_case() -> None:
    assert format_address(0xC4CB76AA10C6) == "C4:CB:76:AA:10:C6"


def test_a_leading_zero_is_not_lost() -> None:
    """
    An address is six bytes whatever its value, and a shortened one would
    name a different device.
    """
    assert format_address(0x001A7DDA710C) == "00:1A:7D:DA:71:0C"


def test_an_address_survives_the_round_trip() -> None:
    assert parse_address(format_address(0x001A7DDA710C)) == 0x001A7DDA710C


def test_an_address_may_be_written_either_way_round() -> None:
    assert parse_address("00-1a-7d-da-71-0c") == parse_address("00:1A:7D:DA:71:0C")


def test_something_that_is_not_an_address_says_so() -> None:
    with pytest.raises(ValueError, match="not a Bluetooth address"):
        parse_address("the kitchen radio")


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def test_no_filter_matches_everything() -> None:
    assert _each(None) == []


def test_one_service_is_taken_on_its_own() -> None:
    assert _each(SERVICE) == [SERVICE]


def test_several_services_are_all_accepted() -> None:
    spp = uuid.UUID("00001101-0000-1000-8000-00805f9b34fb")
    assert _each([SERVICE, spp]) == [SERVICE, spp]


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------


def test_a_device_that_is_not_there_is_not_a_connection_failure() -> None:
    error = translate_bluetooth_error(BluetoothError.DEVICE_NOT_CONNECTED, "00:11")
    assert isinstance(error, DeviceNotFoundError)
    assert "switched off or out of range" in str(error)


def test_a_radio_that_is_gone_is_reported_as_the_adapter() -> None:
    error = translate_bluetooth_error(BluetoothError.RADIO_NOT_AVAILABLE, "00:11")
    assert isinstance(error, BluetoothUnavailableError)
    assert error.reason is Availability.SWITCHED_OFF


def test_anything_else_is_a_connection_failure_naming_what_happened() -> None:
    error = translate_bluetooth_error(BluetoothError.OTHER_ERROR, "00:11")
    assert isinstance(error, ConnectionFailedError)
    assert "OTHER_ERROR" in str(error)


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


def test_the_socket_address_is_packed_the_way_windows_reads_it() -> None:
    """
    Windows declares SOCKADDR_BTH without padding, so it is 30 bytes with the
    RFCOMM channel at offset 26. Left to ctypes' natural alignment it becomes
    40 bytes with the channel at 32, Windows reads the tail of the service
    UUID instead, and the published record advertises a channel nobody is
    listening on -- a service on channel 25 was advertised as 161. Nothing
    fails loudly when this is wrong, so it is pinned here.
    """
    import ctypes

    from aio_rfcomm.backend.windows.publish import _SOCKADDR_BTH

    assert ctypes.sizeof(_SOCKADDR_BTH) == 30
    assert _SOCKADDR_BTH.port.offset == 26
    assert _SOCKADDR_BTH.serviceClassId.offset == 10


def test_a_uuid_survives_the_trip_into_windows_layout() -> None:
    import ctypes

    from aio_rfcomm.backend.windows.publish import _GUID

    value = uuid.UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")
    guid = _GUID.of(value)
    assert guid.Data1 == 0xC0FFEE00
    assert guid.Data2 == 0x1DEA
    assert guid.Data3 == 0x4B1D
    assert (
        bytes(ctypes.cast(guid.Data4, ctypes.POINTER(ctypes.c_ubyte * 8))[0])
        == (value.bytes[8:])
    )


class _FakeSocket:
    """
    A socket that refuses the channels a test says are taken.
    """

    def __init__(self, taken: set[int]) -> None:
        self.taken = taken
        self.bound: int | None = None

    def bind(self, address: tuple[str, int]) -> None:
        import errno

        if address[1] in self.taken:
            raise OSError(errno.EADDRINUSE, "in use")
        self.bound = address[1]


def test_a_free_channel_is_found_from_the_top_down() -> None:
    from aio_rfcomm.backend.provider import LAST_CHANNEL
    from aio_rfcomm.backend.windows.connection import bind_channel

    sock = _FakeSocket(set())
    assert bind_channel(sock, None) == LAST_CHANNEL  # type: ignore[arg-type]


def test_channels_in_use_are_stepped_over() -> None:
    from aio_rfcomm.backend.provider import LAST_CHANNEL
    from aio_rfcomm.backend.windows.connection import bind_channel

    sock = _FakeSocket({LAST_CHANNEL, LAST_CHANNEL - 1})
    assert bind_channel(sock, None) == LAST_CHANNEL - 2  # type: ignore[arg-type]


def test_an_asked_for_channel_that_is_taken_says_so() -> None:
    from aio_rfcomm.backend.windows.connection import bind_channel
    from aio_rfcomm.errors import ChannelInUseError

    sock = _FakeSocket({7})
    with pytest.raises(ChannelInUseError, match="channel 7"):
        bind_channel(sock, 7)  # type: ignore[arg-type]


def test_no_free_channel_at_all_is_an_error() -> None:
    from aio_rfcomm.backend.provider import FIRST_CHANNEL, LAST_CHANNEL
    from aio_rfcomm.backend.windows.connection import bind_channel
    from aio_rfcomm.errors import ChannelInUseError

    sock = _FakeSocket(set(range(FIRST_CHANNEL, LAST_CHANNEL + 1)))
    with pytest.raises(ChannelInUseError, match="every RFCOMM channel"):
        bind_channel(sock, None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Refusals
#
# Measured on Windows 10.0.26200.9457 against macOS 26.6.2 and on
# 10.0.26200.9711 against BlueZ 5.85: a channel nothing listens on fails with
# WSAEHOSTDOWN, and an address nothing answers on with WSAETIMEDOUT.
# --------------------------------------------------------------------------


def test_a_refused_channel_means_nothing_is_listening() -> None:
    error = translate_refusal(winsock_error(WSAEHOSTDOWN), "00:11", 30)
    assert isinstance(error, ServiceNotFoundError)
    assert "refused channel 30" in str(error)


def test_a_device_that_times_out_is_worth_asking_again() -> None:
    assert translate_refusal(winsock_error(WSAETIMEDOUT), "00:11", 30) is None


def test_timing_out_every_time_means_the_device_did_not_answer() -> None:
    error = translate_failure(winsock_error(WSAETIMEDOUT), "00:11", 30)
    assert isinstance(error, DeviceNotFoundError)
    assert "did not answer" in str(error)


def test_failing_every_time_otherwise_is_a_connection_failure() -> None:
    error = translate_failure(winsock_error(10051), "00:11", 30)
    assert isinstance(error, ConnectionFailedError)
    assert "channel 30" in str(error)


# --------------------------------------------------------------------------
# Radio state
# --------------------------------------------------------------------------


def test_a_radio_that_is_on_is_usable() -> None:
    assert _availability_of(RadioState.ON, "LOKI") == (Availability.USABLE, None)


@pytest.mark.parametrize(
    "radio", [RadioState.OFF, RadioState.DISABLED, RadioState.UNKNOWN]
)
def test_any_other_radio_is_switched_off_and_says_which(radio: RadioState) -> None:
    availability, detail = _availability_of(radio, "LOKI")
    assert availability is Availability.SWITCHED_OFF
    assert detail is not None
    assert "LOKI" in detail


# --------------------------------------------------------------------------
# The backend, through the stand-ins
# --------------------------------------------------------------------------

FREYR = "00:1A:7D:DA:71:0C"


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> Generator[FakeWindows, None, None]:
    """
    A machine with one adapter, switched on, and nothing else yet.
    """
    fake = FakeWindows()
    fake.install(monkeypatch)
    fake.plug_in()
    yield fake
    fake.close()


@pytest.fixture
def freyr(machine: FakeWindows) -> FakePeer:
    """
    A paired device in range, with nothing published yet.
    """
    return machine.add_peer(FREYR, "freyr")


@pytest.fixture
async def windows(machine: FakeWindows) -> AsyncGenerator[Bluetooth, None]:
    """
    Bluetooth opened through the Windows backend.
    """
    async with _open_bluetooth(WindowsBackend()) as opened:
        yield opened


async def _heard(far: socket.socket, count: int) -> bytes:
    """
    Read what reached the peer's end.
    """
    far.setblocking(False)
    loop = asyncio.get_running_loop()
    got = b""

    while len(got) < count:
        got += await loop.sock_recv(far, count - len(got))

    return got


# -- availability ----------------------------------------------------------


async def test_bluetooth_with_its_radio_on_reads_as_usable(windows: Bluetooth) -> None:
    assert await anext(windows.watch_availability()) is Availability.USABLE


async def test_switching_the_radio_off_and_on_is_followed(
    windows: Bluetooth, machine: FakeWindows
) -> None:
    watcher = windows.watch_availability()
    assert await anext(watcher) is Availability.USABLE

    machine.switch(RadioState.OFF)
    assert await anext(watcher) is Availability.SWITCHED_OFF

    machine.switch(RadioState.ON)
    assert await anext(watcher) is Availability.USABLE


async def test_an_adapter_unplugged_and_plugged_back_is_followed(
    windows: Bluetooth, machine: FakeWindows
) -> None:
    watcher = windows.watch_availability()
    assert await anext(watcher) is Availability.USABLE
    adapter = machine.adapters[0]

    machine.unplug(adapter)
    assert await anext(watcher) is Availability.NO_ADAPTER
    # Its radio is no longer listened to, since a new one comes with it.
    assert len(adapter.radio.state_changed) == 0

    replugged = machine.plug_in()
    assert await anext(watcher) is Availability.USABLE
    assert len(replugged.radio.state_changed) == 1


async def test_a_radio_that_only_does_low_energy_is_no_adapter(
    machine: FakeWindows,
) -> None:
    machine.adapters[0].is_classic_supported = False

    async with _open_bluetooth(WindowsBackend()) as opened:
        assert await anext(opened.watch_availability()) is Availability.NO_ADAPTER


async def test_a_first_read_that_fails_is_reported_and_read_again(
    machine: FakeWindows,
) -> None:
    machine.failure = OSError("WinRT says no")

    async with _open_bluetooth(WindowsBackend()) as opened:
        watcher = opened.watch_availability()
        assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE

        machine.failure = None
        watcher_events = machine.watchers[0]
        watcher_events.updated.fire(watcher_events, None)
        assert await anext(watcher) is Availability.USABLE


async def test_closing_bluetooth_lets_go_of_every_event(machine: FakeWindows) -> None:
    async with _open_bluetooth(WindowsBackend()):
        pass

    (watcher,) = machine.watchers
    assert watcher.subscribed == 0
    assert watcher.status == DeviceWatcherStatus.STOPPED
    assert len(machine.adapters[0].radio.state_changed) == 0


async def test_switching_the_radio_off_ends_an_open_channel(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    freyr.listening.add(5)

    async with windows.open_channel(RfcommDeviceInfo(FREYR), 5) as channel:
        machine.switch(RadioState.OFF)

        with pytest.raises(BluetoothUnavailableError) as raised:
            await channel.receive()

    assert raised.value.reason is Availability.SWITCHED_OFF


# -- listing ---------------------------------------------------------------


async def test_paired_devices_are_listed(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    machine.add_peer("00:16:53:4A:75:15", "ev3", paired=False)

    (listed,) = await windows.list_known_devices()

    assert listed.address == FREYR
    assert listed.name == "freyr"
    assert listed.id == freyr.id


async def test_a_listing_without_an_address_is_left_out(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.address_listed = False
    assert await windows.list_known_devices() == []


async def test_filtering_reads_what_windows_stored_without_asking_the_device(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.publish(SERVICE, 21)
    freyr.stored = dict(freyr.records)
    freyr.withdraw(SERVICE)

    # Measured on Windows 10.0.26200.9711: the stored copy keeps a record the
    # device has withdrawn, until something asks the device.
    (listed,) = await windows.list_known_devices(service=SERVICE)

    assert listed.address == FREYR
    assert not freyr.connected
    assert await windows.list_known_devices(service=uuid.uuid4()) == []


async def test_filtering_finds_the_address_on_the_device_if_not_listed(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.address_listed = False
    freyr.publish(SERVICE, 21)
    freyr.stored = dict(freyr.records)

    (listed,) = await windows.list_known_devices(service=SERVICE)
    assert listed.address == FREYR


# -- opening ---------------------------------------------------------------


async def test_a_channel_carries_bytes_both_ways(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    freyr.listening.add(5)

    async with windows.open_channel(RfcommDeviceInfo(FREYR), 5) as channel:
        far = machine.far_end()
        assert machine.connected[-1].reached == (FREYR, 5)

        await channel.send(b"hello")
        assert await _heard(far, 5) == b"hello"

        far.sendall(b"back")
        assert await channel.receive_exactly(4) == b"back"


async def test_a_service_is_looked_up_on_the_device_itself(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    freyr.publish(SERVICE, 21)
    freyr.stored = dict(freyr.records)
    # Measured on Windows 10.0.26200.9711 against BlueZ 5.85: a record moved
    # from 21 to 22 was found on 22, while the stored copy still said 21.
    freyr.withdraw(SERVICE)
    freyr.publish(SERVICE, 22)

    async with windows.open_service(RfcommDeviceInfo(FREYR), SERVICE):
        assert machine.connected[-1].reached == (FREYR, 22)


async def test_a_service_the_device_does_not_offer_is_not_found(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.publish(SERVICE, 21)
    freyr.stored = dict(freyr.records)
    freyr.withdraw(SERVICE)

    with pytest.raises(ServiceNotFoundError, match="does not offer"):
        async with windows.open_service(RfcommDeviceInfo(FREYR), SERVICE):
            pass

    assert freyr.attempts == 0


async def test_a_record_without_a_channel_is_not_found(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.records[SERVICE] = {}

    with pytest.raises(ServiceNotFoundError, match="does not say how to reach it"):
        async with windows.open_service(RfcommDeviceInfo(FREYR), SERVICE):
            pass


async def test_a_device_that_does_not_answer_the_query_is_not_found(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.on = False

    with pytest.raises(DeviceNotFoundError, match="switched off or out of range"):
        async with windows.open_service(RfcommDeviceInfo(FREYR), SERVICE):
            pass


async def test_an_address_windows_has_never_seen_is_not_found(
    windows: Bluetooth,
) -> None:
    with pytest.raises(DeviceNotFoundError):
        async with windows.open_service(RfcommDeviceInfo("02:00:00:00:00:01"), SERVICE):
            pass


async def test_a_failed_query_says_how(windows: Bluetooth, freyr: FakePeer) -> None:
    freyr.query_error = BluetoothError.OTHER_ERROR

    with pytest.raises(ConnectionFailedError, match="OTHER_ERROR"):
        async with windows.open_service(RfcommDeviceInfo(FREYR), SERVICE):
            pass


async def test_a_connect_that_times_out_once_is_tried_again(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.listening.add(5)
    freyr.timeouts = 1

    async with windows.open_channel(RfcommDeviceInfo(FREYR), 5):
        assert freyr.attempts == 2


# -- refusals --------------------------------------------------------------


async def test_a_refused_channel_is_not_found_after_one_attempt(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    with pytest.raises(ServiceNotFoundError, match="refused channel 20"):
        async with windows.open_channel(RfcommDeviceInfo(FREYR), 20):
            pass

    assert freyr.attempts == 1
    assert all(sock.fileno() == -1 for sock in machine.sockets)


async def test_a_record_nothing_listens_behind_is_refused_after_one_attempt(
    windows: Bluetooth, freyr: FakePeer
) -> None:
    freyr.records[SERVICE] = {0x0004: protocol_list(9)}

    with pytest.raises(ServiceNotFoundError, match="refused channel 9"):
        async with windows.open_service(RfcommDeviceInfo(FREYR), SERVICE):
            pass

    assert freyr.attempts == 1


async def test_a_device_that_never_answers_is_not_found(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    freyr.on = False

    with pytest.raises(DeviceNotFoundError, match="did not answer"):
        async with windows.open_channel(RfcommDeviceInfo(FREYR), 5):
            pass

    assert freyr.attempts == 4
    assert all(sock.fileno() == -1 for sock in machine.sockets)


async def test_canceling_a_connect_closes_its_socket(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    freyr.listening.add(5)
    freyr.hold = threading.Event()

    async def connect() -> None:
        async with windows.open_channel(RfcommDeviceInfo(FREYR), 5):
            pass

    try:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(connect())

            while freyr.attempts == 0:
                await asyncio.sleep(0.01)

            task.cancel()
    finally:
        freyr.hold.set()

    (sock,) = machine.sockets
    assert sock.fileno() == -1
    assert machine.connected == []


# -- endings ---------------------------------------------------------------


async def test_a_peer_hanging_up_ends_the_channel_after_its_last_words(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    freyr.listening.add(5)

    async with windows.open_channel(RfcommDeviceInfo(FREYR), 5) as channel:
        far = machine.far_end()
        far.sendall(b"bye")
        far.close()

        assert await channel.receive_exactly(3) == b"bye"

        with pytest.raises(ChannelClosedError):
            await channel.receive()

        with pytest.raises(ChannelClosedError):
            await channel.send(b"anyone?")


async def test_leaving_the_block_hangs_up(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    freyr.listening.add(5)

    async with windows.open_channel(RfcommDeviceInfo(FREYR), 5) as channel:
        far = machine.far_end()
        await channel.send(b"last")

    assert await _heard(far, 4) == b"last"
    assert await asyncio.get_running_loop().sock_recv(far, 1) == b""
    assert machine.connected[-1].fileno() == -1


# -- serving ---------------------------------------------------------------


async def test_a_service_is_published_and_accepts_a_peer(
    windows: Bluetooth, machine: FakeWindows, freyr: FakePeer
) -> None:
    async with windows.serve(SERVICE, name="chat") as service:
        # Read back from the record as Windows would read it.
        assert machine.published[SERVICE] == ("chat", service.channel)
        far = machine.connect_in(freyr, service.channel)

        async for request in service.connection_requests():
            assert request.info.address == FREYR

            async with request.accept() as channel:
                await channel.send(b"hi")
                assert await _heard(far, 2) == b"hi"

            break

    assert SERVICE not in machine.published
    assert machine.bound == {}


async def test_a_channel_already_served_is_in_use(
    windows: Bluetooth, machine: FakeWindows
) -> None:
    async with windows.serve(SERVICE, name="chat", channel=7):
        with pytest.raises(ChannelInUseError, match="channel 7"):
            async with windows.serve(uuid.uuid4(), name="other", channel=7):
                pass

    assert machine.bound == {}
