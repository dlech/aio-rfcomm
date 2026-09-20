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
