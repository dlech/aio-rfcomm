# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Reading service records, as the Service Discovery Protocol defines them.

A service record is a set of attributes, each holding a *data element*: a
one-byte header giving a type and a size, then that many bytes of value.
Sequences nest, so a record is a small tree.

Only Windows needs this today. Windows hands back raw attribute bytes and
leaves working out the RFCOMM channel number to the caller, where BlueZ does
the lookup itself inside ``ConnectProfile`` and IOBluetooth parses records for
us. It lives here rather than in the Windows backend because it is a pure
codec with nothing platform-specific in it, and because BlueZ's experimental
``GetServiceRecords`` would hand Linux the same raw bytes.

Reading only. Writing records is the server role's problem, and the server
role is not implemented; a half-used encoder would be a liability rather than
a head start.
"""

from __future__ import annotations

import enum
import struct
import uuid
from collections.abc import Mapping
from typing import Any

__all__ = [
    "PROTOCOL_DESCRIPTOR_LIST",
    "SERVICE_NAME",
    "SdpError",
    "parse_element",
    "read_channel",
    "read_name",
]

# Everything multi-byte in a record is big-endian -- sizes and integers alike.
# Worth stating because the prototype this was drawn from reads them
# little-endian throughout and appears to work: every field that matters in
# practice is one byte wide, so the mistake stays invisible until a record
# carries a longer one.
_SIZE_32 = struct.Struct(">I")
_SIZE_16 = struct.Struct(">H")

# The base from which 16- and 32-bit UUIDs are expanded.
_BASE = uuid.UUID("00000000-0000-1000-8000-00805f9b34fb")

PROTOCOL_DESCRIPTOR_LIST = 0x0004
"""
The attribute holding the protocol stack a service is reached over.
"""

SERVICE_NAME = 0x0100
"""
The attribute holding a service's human-readable name.
"""

_RFCOMM = uuid.UUID("00000003-0000-1000-8000-00805f9b34fb")


class _Kind(enum.IntEnum):
    """
    The data element types SDP defines.
    """

    NIL = 0
    UNSIGNED = 1
    SIGNED = 2
    UUID = 3
    TEXT = 4
    BOOLEAN = 5
    SEQUENCE = 6
    ALTERNATIVE = 7
    URL = 8


class SdpError(Exception):
    """
    A service record could not be read.

    Not one of the library's own errors: a backend turns it into one, since
    what a caller can do about a malformed record is the same as for any other
    failure to reach a service.
    """


def parse_element(data: bytes) -> Any:
    """
    Read one data element.

    Args:
        data: The element, which may be followed by more bytes.

    Returns:
        The value: ``None``, an :class:`int`, a :class:`bool`, a
        :class:`~uuid.UUID`, :class:`bytes` for a text string, a :class:`str`
        for a URL, or a list for a sequence.

    Raises:
        SdpError: The bytes are not a data element.
    """
    value, _read = _read_element(data, 0)
    return value


def read_channel(attributes: Mapping[int, bytes]) -> int:
    """
    Find which RFCOMM channel a service is reached on.

    The protocol descriptor list is a sequence of protocol layers, innermost
    first, each a sequence of a protocol UUID and that protocol's parameters.
    RFCOMM's single parameter is the channel number.

    Args:
        attributes: The service record, as attribute identifier to raw bytes.

    Returns:
        The RFCOMM channel number.

    Raises:
        SdpError: The record has no protocol descriptor list, or nothing
            in it is RFCOMM.
    """
    raw = attributes.get(PROTOCOL_DESCRIPTOR_LIST)
    if raw is None:
        raise SdpError("the service record has no protocol descriptor list")

    layers = parse_element(raw)
    if not isinstance(layers, list):
        raise SdpError("the protocol descriptor list is not a sequence")

    for layer in layers:
        if not isinstance(layer, list) or not layer:
            continue
        if layer[0] != _RFCOMM:
            continue
        if len(layer) < 2 or not isinstance(layer[1], int):
            raise SdpError("the RFCOMM layer carries no channel number")
        return layer[1]

    raise SdpError("the service is not reached over RFCOMM")


def read_name(attributes: Mapping[int, bytes]) -> str | None:
    """
    Read a service's name, if it has one.

    Args:
        attributes: The service record, as attribute identifier to raw bytes.

    Returns:
        The name, or ``None`` if the record does not carry one or it is not
        decodable text.
    """
    raw = attributes.get(SERVICE_NAME)
    if raw is None:
        return None
    try:
        text = parse_element(raw)
    except SdpError:
        return None
    if not isinstance(text, bytes):
        return None
    return text.decode(errors="replace")


def _read_element(data: bytes, offset: int) -> tuple[Any, int]:
    """
    Read one data element and say how far it reached.

    Args:
        data: The buffer.
        offset: Where the element starts.

    Returns:
        The value, and the offset just past the element.

    Raises:
        SdpError: The element is malformed or runs off the end.
    """
    if offset >= len(data):
        raise SdpError("a data element ran off the end of the record")

    header = data[offset]
    kind = header >> 3
    index = header & 0x07
    start = offset + 1

    if index < 5:
        # A size named by the index itself: 1, 2, 4, 8 or 16 bytes -- except
        # for nil, which is the one element with no value at all.
        size = 0 if kind == _Kind.NIL else 1 << index
    elif index == 5:
        size, start = data[start], start + 1
    elif index == 6:
        size, start = _read(_SIZE_16, data, start), start + _SIZE_16.size
    else:
        size, start = _read(_SIZE_32, data, start), start + _SIZE_32.size

    end = start + size
    if end > len(data):
        raise SdpError(
            f"a data element claims {size} bytes but only {len(data) - start} are left"
        )
    return _decode(kind, data[start:end]), end


def _read(layout: struct.Struct, data: bytes, offset: int) -> int:
    """
    Read one fixed-width field.

    Args:
        layout: How the field is laid out.
        data: The buffer.
        offset: Where the field starts.

    Returns:
        The value.

    Raises:
        SdpError: The field runs off the end.
    """
    try:
        return layout.unpack_from(data, offset)[0]
    except struct.error as error:
        raise SdpError(f"a data element's size ran off the end: {error}") from error


def _decode(kind: int, value: bytes) -> Any:
    """
    Turn one element's bytes into a Python value.

    Args:
        kind: The element's type.
        value: Its bytes.

    Returns:
        The value.

    Raises:
        SdpError: The type is unknown, or the value does not fit it.
    """
    match kind:
        case _Kind.NIL:
            return None
        case _Kind.UNSIGNED:
            return int.from_bytes(value, "big")
        case _Kind.SIGNED:
            return int.from_bytes(value, "big", signed=True)
        case _Kind.UUID:
            return _decode_uuid(value)
        case _Kind.TEXT:
            # Left as bytes: a record says which language and encoding its
            # strings are in through a separate attribute, and guessing is
            # how mojibake happens.
            return value
        case _Kind.BOOLEAN:
            return bool(value and value[0])
        case _Kind.SEQUENCE | _Kind.ALTERNATIVE:
            return _decode_sequence(value)
        case _Kind.URL:
            return value.decode(errors="replace")
        case _:
            raise SdpError(f"unknown data element type {kind}")


def _decode_sequence(value: bytes) -> list[Any]:
    """
    Read every element of a sequence.

    Args:
        value: The sequence's contents.

    Returns:
        The elements.
    """
    elements: list[Any] = []
    offset = 0
    while offset < len(value):
        element, offset = _read_element(value, offset)
        elements.append(element)
    return elements


def _decode_uuid(value: bytes) -> uuid.UUID:
    """
    Expand a UUID of any of the three widths SDP allows.

    Args:
        value: The UUID's bytes, 2, 4 or 16 of them.

    Returns:
        The full 128-bit UUID.

    Raises:
        SdpError: The width is not one SDP allows.
    """
    if len(value) == 16:
        return uuid.UUID(bytes=bytes(value))
    if len(value) in (2, 4):
        return uuid.UUID(int=_BASE.int | (int.from_bytes(value, "big") << 96))
    raise SdpError(f"a UUID may not be {len(value)} bytes")
