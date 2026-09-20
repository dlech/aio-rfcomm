# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Reading service records.

The record below was captured from a real device -- the BlueZ test server on
the Linux box, read by Windows -- rather than written by hand, so these check
the parser against bytes some other implementation produced. Hand-written
vectors only ever prove that the parser agrees with itself.

Endianness is the thing most worth pinning down. Everything multi-byte in a
record is big-endian, and reading it the other way round is invisible in most
records, because the fields that matter in practice are one byte wide.
"""

import uuid

import pytest

from aio_rfcomm.backend.sdp import (
    PROTOCOL_DESCRIPTOR_LIST,
    SdpError,
    parse_element,
    read_channel,
    read_name,
)

SERVICE = uuid.UUID("539f44f8-e629-4723-bd40-9bd0d2807056")
RFCOMM = uuid.UUID("00000003-0000-1000-8000-00805f9b34fb")
L2CAP = uuid.UUID("00000100-0000-1000-8000-00805f9b34fb")

# One RFCOMM service, as it came off the wire.
RECORD = {
    0x0000: bytes.fromhex("0a00010017"),
    0x0001: bytes.fromhex("35111c539f44f8e6294723bd409bd0d2807056"),
    0x0004: bytes.fromhex("350c350319010035051900030816"),
    0x0005: bytes.fromhex("3503191002"),
    0x0100: bytes.fromhex("250c746573742073657276696365"),
}


# --------------------------------------------------------------------------
# A real record
# --------------------------------------------------------------------------


def test_the_channel_number_comes_out_of_the_record() -> None:
    assert read_channel(RECORD) == 22


def test_the_service_name_comes_out_of_the_record() -> None:
    assert read_name(RECORD) == "test service"


def test_the_service_class_is_the_uuid_that_was_registered() -> None:
    assert parse_element(RECORD[0x0001]) == [SERVICE]


def test_the_protocol_stack_reads_innermost_first() -> None:
    assert parse_element(RECORD[PROTOCOL_DESCRIPTOR_LIST]) == [[L2CAP], [RFCOMM, 22]]


def test_a_record_handle_is_read_big_endian() -> None:
    """
    The one field in this record wide enough to tell the two readings apart.
    """
    assert parse_element(RECORD[0x0000]) == 0x00010017


# --------------------------------------------------------------------------
# Element types
# --------------------------------------------------------------------------


def test_nil_has_no_value() -> None:
    assert parse_element(b"\x00") is None


def test_an_unsigned_integer_is_big_endian() -> None:
    assert parse_element(bytes.fromhex("0a12345678")) == 0x12345678


def test_a_signed_integer_is_big_endian() -> None:
    assert parse_element(bytes.fromhex("10ff")) == -1


def test_a_boolean_reads_as_one() -> None:
    assert parse_element(b"\x28\x01") is True
    assert parse_element(b"\x28\x00") is False


def test_a_short_uuid_expands_onto_the_base() -> None:
    assert parse_element(bytes.fromhex("191002")) == uuid.UUID(
        "00001002-0000-1000-8000-00805f9b34fb"
    )


def test_a_medium_uuid_expands_onto_the_base() -> None:
    assert parse_element(bytes.fromhex("1a0000110d")) == uuid.UUID(
        "0000110d-0000-1000-8000-00805f9b34fb"
    )


def test_a_full_uuid_is_taken_as_it_is() -> None:
    assert parse_element(RECORD[0x0001])[0] == SERVICE


def test_text_stays_bytes() -> None:
    """
    A record says elsewhere what language and encoding its strings are in, so
    the codec does not guess.
    """
    assert (
        parse_element(bytes.fromhex("250c746573742073657276696365")) == b"test service"
    )


def test_sequences_nest() -> None:
    assert parse_element(bytes.fromhex("3505350319010035000000"[:14])) == [[L2CAP]]


def test_a_longer_element_carries_its_own_size() -> None:
    """
    Sizes above 255 use a two-byte field, which is also big-endian.
    """
    payload = b"a" * 300
    element = bytes.fromhex("26") + (300).to_bytes(2, "big") + payload
    assert parse_element(element) == payload


# --------------------------------------------------------------------------
# Records that make no sense
# --------------------------------------------------------------------------


def test_an_element_running_off_the_end_is_refused() -> None:
    with pytest.raises(SdpError, match="only"):
        parse_element(bytes.fromhex("350c3503"))


def test_an_empty_record_is_refused() -> None:
    with pytest.raises(SdpError, match="ran off the end"):
        parse_element(b"")


def test_an_unknown_element_type_is_refused() -> None:
    with pytest.raises(SdpError, match="unknown data element type"):
        parse_element(bytes.fromhex("4800"))


def test_a_uuid_of_the_wrong_width_is_refused() -> None:
    with pytest.raises(SdpError, match="may not be"):
        parse_element(bytes.fromhex("1b0000000000000000"))


def test_a_record_without_a_protocol_list_says_so() -> None:
    with pytest.raises(SdpError, match="no protocol descriptor list"):
        read_channel({0x0100: RECORD[0x0100]})


def test_a_service_that_is_not_rfcomm_says_so() -> None:
    only_l2cap = {PROTOCOL_DESCRIPTOR_LIST: bytes.fromhex("3505350319010035")[:7]}
    with pytest.raises(SdpError, match="not reached over RFCOMM"):
        read_channel(only_l2cap)


def test_an_rfcomm_layer_with_no_channel_says_so() -> None:
    with pytest.raises(SdpError, match="no channel number"):
        read_channel({PROTOCOL_DESCRIPTOR_LIST: bytes.fromhex("35053503190003")})


def test_a_record_with_no_name_is_not_an_error() -> None:
    assert (
        read_name({PROTOCOL_DESCRIPTOR_LIST: RECORD[PROTOCOL_DESCRIPTOR_LIST]}) is None
    )
