# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The devices macOS has paired with, and the services it stored for them.
"""

import sys
from collections.abc import Collection
from typing import TYPE_CHECKING
from uuid import UUID

assert not TYPE_CHECKING or sys.platform == "darwin"

from aio_rfcomm.backend.iobluetooth.codes import (
    SDPDataElementType,
)
from aio_rfcomm.backend.iobluetooth.helper.native import bindings
from aio_rfcomm.backend.iobluetooth.helper.native.bindings import (
    NativeDevice,
    NativeRecord,
    each,
    normalize,
    python_uuid,
)
from aio_rfcomm.discovery import RfcommDeviceInfo

# The SDP attribute naming a record's service classes.
_SERVICE_CLASS_ID_LIST = 0x0001


def _service_classes(record: NativeRecord) -> set[UUID]:
    """
    Read the service classes a stored record names.

    Args:
        record: The record.

    Returns:
        The UUIDs in its Service Class ID List.
    """
    listed = record.getAttributeDataElement(_SERVICE_CLASS_ID_LIST)
    if listed is None:
        return set()

    # macOS 26.6.2 stores a record once per class, each copy holding its one
    # class as a bare UUID rather than as a list.
    match listed.getTypeDescriptor():
        case SDPDataElementType.UUID:
            elements = [listed]
        case SDPDataElementType.DATA_ELEMENT_SEQUENCE:
            elements = list(listed.getArrayValue() or [])
        case _:
            return set()

    return {
        python_uuid(element.getUUIDValue())
        for element in elements
        if element.getTypeDescriptor() == SDPDataElementType.UUID
    }


def offering(device: NativeDevice, services: Collection[UUID]) -> NativeRecord | None:
    """
    Find a stored record whose service class is one of these.

    Args:
        device: The device.
        services: The service classes to look for.

    Returns:
        The first such record, or ``None``.
    """
    # Not getServiceRecordForUUID, which matches a UUID anywhere in a record:
    # on macOS 26.6.2 a Pybricks EV3, whose record names Serial Port only as a
    # profile it follows, was found offering Serial Port.
    for record in device.services or []:
        if not _service_classes(record).isdisjoint(services):
            return record

    return None


def list_known_devices(
    service: UUID | Collection[UUID] | None = None,
) -> list[RfcommDeviceInfo]:
    """
    List the devices macOS has paired with.

    Filtering happens against the stored SDP records, which is all that is
    available without connecting to each device in turn.

    Args:
        service: Return only devices with a record whose service class is
            this, or any of these. ``None`` returns every paired device.

    Returns:
        The matching devices.
    """
    wanted = each(service)

    found: list[RfcommDeviceInfo] = []
    seen: set[str] = set()
    for device in bindings.IOBluetoothDevice.pairedDevices() or []:
        if wanted and offering(device, wanted) is None:
            continue
        address = normalize(str(device.addressString))
        # macOS 26.6.2 lists some devices twice. Measured rather than guessed
        # at: pairedDevices() returned thirteen entries holding twelve distinct
        # objects, with one device's object appearing at two non-adjacent
        # positions. The two entries are the same object, so there is nothing
        # to tell them apart and nothing to choose between them -- only a
        # caller left wondering which of two identical devices to use.
        if address in seen:
            continue
        seen.add(address)
        name = device.name
        found.append(RfcommDeviceInfo(address, str(name) if name else None))
    return found
