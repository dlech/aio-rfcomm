# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The plain types callers build or catch: device descriptions and errors.
"""

import copy
from typing import assert_never

import pytest

from aio_rfcomm.availability import Availability, UnavailableReason
from aio_rfcomm.bluetooth import Bluetooth
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelBrokenError,
    ChannelBusyError,
    ChannelClosedError,
    CloseReason,
    RfcommError,
    ScopeClosedError,
)
from tests.fake import DEVICE

# --------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "00:1A:7D:DA:71:0C",
        "00:1a:7d:da:71:0c",
        "00-1A-7D-DA-71-0C",
        "00-1a-7d-Da-71-0c",
    ],
)
def test_an_address_is_kept_colon_separated_and_upper_case(written: str) -> None:
    assert RfcommDeviceInfo(written).address == "00:1A:7D:DA:71:0C"


@pytest.mark.parametrize(
    "written",
    [
        "",
        "00:1A:7D:DA:71",
        "00:1A:7D:DA:71:0C:00",
        "00:1A-7D:DA:71:0C",
        "001A7DDA710C",
        "00:1A:7D:DA:71:0G",
        "0:1A:7D:DA:71:0C",
        " 00:1A:7D:DA:71:0C",
        "00:1A:7D:DA:71:0C\n",
    ],
)
def test_anything_else_is_not_an_address(written: str) -> None:
    with pytest.raises(ValueError, match="not a Bluetooth address"):
        RfcommDeviceInfo(written)


def test_a_name_is_not_needed() -> None:
    info = RfcommDeviceInfo("00:1A:7D:DA:71:0C")
    assert info.name is None
    assert info.id is None


# --------------------------------------------------------------------------
# Channel numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("channel", [1, 30])
def test_the_first_and_last_channel_numbers_are_allowed(
    bluetooth: Bluetooth, channel: int
) -> None:
    bluetooth.open_channel(DEVICE, channel)


@pytest.mark.parametrize("channel", [0, 31, -1, 255])
def test_a_channel_number_out_of_range_is_refused_before_anything_happens(
    bluetooth: Bluetooth, channel: int
) -> None:
    # Refused when the call is made, not when the block is entered.
    with pytest.raises(ValueError, match="not an RFCOMM channel"):
        bluetooth.open_channel(DEVICE, channel)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def _explain(reason: UnavailableReason) -> str:
    # Checked by pyright rather than at run time: a reason added and not
    # handled here is a type error, which is the property callers rely on.
    match reason:
        case Availability.SWITCHED_OFF:
            return "off"
        case Availability.NO_ADAPTER:
            return "none"
        case Availability.NOT_ALLOWED:
            return "refused"
        case Availability.SERVICE_UNAVAILABLE:
            return "restarting"
        case Availability.UNSUPPORTED:
            return "never"
        case _:
            assert_never(reason)


@pytest.mark.parametrize(
    "reason",
    [a for a in Availability if a is not Availability.USABLE],
)
def test_every_reason_has_something_to_say(reason: UnavailableReason) -> None:
    error = BluetoothUnavailableError(reason)
    assert error.reason is reason
    assert str(error)
    assert _explain(error.reason)


def test_an_error_copies_as_itself() -> None:
    error = BluetoothUnavailableError(Availability.NOT_ALLOWED, "allow it")
    copied = copy.copy(error)
    assert copied.reason is Availability.NOT_ALLOWED
    assert str(copied) == "allow it"

    closed = copy.copy(ChannelClosedError(CloseReason.LINK_LOST))
    assert closed.reason is CloseReason.LINK_LOST


def test_the_platform_s_own_words_are_kept() -> None:
    error = BluetoothUnavailableError(Availability.NOT_ALLOWED, "allow it in settings")
    assert str(error) == "allow it in settings"


@pytest.mark.parametrize(
    "kind", [ChannelBrokenError, ChannelBusyError, ScopeClosedError]
)
def test_a_mistake_in_the_calling_code_is_not_caught_as_bluetooth(
    kind: type[Exception],
) -> None:
    # Catching RfcommError is how a program handles what Bluetooth or the
    # peer did; a bug in the program must not disappear into that.
    assert issubclass(kind, RuntimeError)
    assert not issubclass(kind, RfcommError)
