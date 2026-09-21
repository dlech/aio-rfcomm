# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The parts of the Windows backend that need neither WinRT nor a radio.

Addresses cross the boundary between WinRT and Winsock twice -- WinRT counts
in 48-bit integers and sockets are addressed by text -- so getting that
conversion wrong would reach a different device, or none. The rest is turning
WinRT's errors into ours.
"""

import pytest

pytest.importorskip(
    "winrt.windows.devices.bluetooth", reason="the Windows backend needs WinRT"
)

import uuid

from winrt.windows.devices.bluetooth import BluetoothError

from aio_rfcomm.backend.windows import (
    _each,
    _format,
    _parse,
    _translate,
)
from aio_rfcomm.errors import (
    AdapterOffError,
    ConnectionFailedError,
    DeviceNotFoundError,
)

SERVICE = uuid.UUID("539f44f8-e629-4723-bd40-9bd0d2807056")


# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------


def test_an_address_is_reported_colon_separated_and_upper_case() -> None:
    assert _format(0xC4CB76AA10C6) == "C4:CB:76:AA:10:C6"


def test_a_leading_zero_is_not_lost() -> None:
    """
    An address is six bytes whatever its value, and a shortened one would
    name a different device.
    """
    assert _format(0x001A7DDA710C) == "00:1A:7D:DA:71:0C"


def test_an_address_survives_the_round_trip() -> None:
    assert _parse(_format(0x001A7DDA710C)) == 0x001A7DDA710C


def test_an_address_may_be_written_either_way_round() -> None:
    assert _parse("00-1a-7d-da-71-0c") == _parse("00:1A:7D:DA:71:0C")


def test_something_that_is_not_an_address_says_so() -> None:
    with pytest.raises(DeviceNotFoundError, match="not a Bluetooth address"):
        _parse("the kitchen radio")


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
    error = _translate(BluetoothError.DEVICE_NOT_CONNECTED, "00:11")
    assert isinstance(error, DeviceNotFoundError)


def test_a_radio_that_is_gone_is_reported_as_the_adapter() -> None:
    error = _translate(BluetoothError.RADIO_NOT_AVAILABLE, "00:11")
    assert isinstance(error, AdapterOffError)


def test_anything_else_is_a_connection_failure_naming_what_happened() -> None:
    error = _translate(BluetoothError.OTHER_ERROR, "00:11")
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

    from aio_rfcomm.backend.windows._service import _SOCKADDR_BTH

    assert ctypes.sizeof(_SOCKADDR_BTH) == 30
    assert _SOCKADDR_BTH.port.offset == 26
    assert _SOCKADDR_BTH.serviceClassId.offset == 10


def test_a_uuid_survives_the_trip_into_windows_layout() -> None:
    import ctypes

    from aio_rfcomm.backend.windows._service import _GUID

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
    from aio_rfcomm.backend.windows import _bind_channel

    sock = _FakeSocket(set())
    assert _bind_channel(sock, None) == LAST_CHANNEL  # type: ignore[arg-type]


def test_channels_in_use_are_stepped_over() -> None:
    from aio_rfcomm.backend.provider import LAST_CHANNEL
    from aio_rfcomm.backend.windows import _bind_channel

    sock = _FakeSocket({LAST_CHANNEL, LAST_CHANNEL - 1})
    assert _bind_channel(sock, None) == LAST_CHANNEL - 2  # type: ignore[arg-type]


def test_an_asked_for_channel_that_is_taken_says_so() -> None:
    from aio_rfcomm.backend.windows import _bind_channel
    from aio_rfcomm.errors import ChannelInUseError

    sock = _FakeSocket({7})
    with pytest.raises(ChannelInUseError, match="channel 7"):
        _bind_channel(sock, 7)  # type: ignore[arg-type]


def test_no_free_channel_at_all_is_an_error() -> None:
    from aio_rfcomm.backend.provider import FIRST_CHANNEL, LAST_CHANNEL
    from aio_rfcomm.backend.windows import _bind_channel
    from aio_rfcomm.errors import ChannelInUseError

    sock = _FakeSocket(set(range(FIRST_CHANNEL, LAST_CHANNEL + 1)))
    with pytest.raises(ChannelInUseError, match="every RFCOMM channel"):
        _bind_channel(sock, None)  # type: ignore[arg-type]
