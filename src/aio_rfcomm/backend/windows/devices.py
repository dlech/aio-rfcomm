# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Finding devices, and the channel a service is on, through WinRT.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, cast
from uuid import UUID

assert not TYPE_CHECKING or sys.platform == "win32"

from winrt.system import unbox_string
from winrt.windows.devices.bluetooth import (
    BluetoothCacheMode,
    BluetoothConnectionStatus,
    BluetoothDevice,
    BluetoothError,
)
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
from winrt.windows.devices.enumeration import (
    DeviceInformation,
)

from aio_rfcomm.backend.sdp import SdpError, read_channel
from aio_rfcomm.backend.windows.addresses import format_address, parse_address
from aio_rfcomm.backend.windows.errors import translate_bluetooth_error
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    DeviceNotFoundError,
    ServiceNotFoundError,
)

__all__ = ["ADDRESS_PROPERTY", "look_up", "offers_any", "read_address"]

logger = logging.getLogger(__name__)


# A device interface carries its Bluetooth address, which saves opening
# every paired device just to ask. Written without separators and in lower
# case -- "001a7dda710c" -- so it needs putting into our own form.
ADDRESS_PROPERTY = "System.DeviceInterface.Bluetooth.DeviceAddress"


def read_address(info: DeviceInformation) -> str | None:
    """
    Take a device's Bluetooth address from its listing, if it is there.

    Args:
        info: The device as the listing described it.

    Returns:
        The address, or ``None`` if the listing did not carry one.
    """
    raw = info.properties.get(ADDRESS_PROPERTY)
    if raw is None:
        return None
    try:
        return format_address(int(unbox_string(raw), 16))
    except (OSError, ValueError):
        return None


async def _device(device: RfcommDeviceInfo, address: str) -> BluetoothDevice:
    """
    Get hold of a device, however the caller named it.

    Args:
        device: A description from a listing, or a bare address.
        address: Its address, used where the description has no device id.

    Returns:
        The device.
    """
    # The handle is ours, kept private from users of the description.
    device_id = device._handle  # pyright: ignore[reportPrivateUsage]

    if device_id is None:
        # The stubs allow None, but measured on Windows 10.0.26200.9711 an
        # address Windows does not know still gives a device, which then does
        # not answer.
        found = await BluetoothDevice.from_bluetooth_address_async(
            parse_address(address)
        )
        return cast(BluetoothDevice, found)

    return await BluetoothDevice.from_id_async(device_id)


async def offers_any(device: BluetoothDevice, wanted: list[RfcommServiceId]) -> bool:
    """
    Say whether a device offers any of these services.

    Answered from what Windows already knows rather than by asking the device.
    An uncached query would connect to every paired device in turn just to
    build a list, which is slow, intrusive, and would wake up things like
    headphones. Refreshing a stale record is a separate problem, and one every
    platform has.

    Args:
        device: The device to check.
        wanted: The services to look for.

    Returns:
        True if it offers at least one of them.
    """
    for service in wanted:
        result = await device.get_rfcomm_services_for_id_with_cache_mode_async(
            service, BluetoothCacheMode.CACHED
        )
        if result.error == BluetoothError.SUCCESS and result.services:
            return True
    return False


async def look_up(device: RfcommDeviceInfo, address: str, service: UUID) -> int:
    """
    Ask a device which RFCOMM channel a service is on.

    The query is deliberately uncached. Windows will happily answer from a
    record it read months ago, and a channel number that has moved since is
    worse than a slow answer -- this is the one place the round trip is
    already being paid for, since the device has to be reached either way.

    Args:
        device: The device to ask.
        address: Its address, for the error messages.
        service: The service to find.

    Returns:
        The RFCOMM channel number of the first record offering the service.
        A device publishing several is rare and there is no way for a caller
        to say which it meant. See the note where that is decided.

    Raises:
        DeviceNotFoundError: The device did not answer.
        ServiceNotFoundError: It does not offer that service.
        ConnectionFailedError: The query failed.
    """
    found = await _device(device, address)

    # Every service, then filtered here, rather than asking for the one we
    # want. Windows has a query that takes a service id, and on a device that
    # is not currently connected it answers nothing at all -- no error, no
    # connection attempt, an empty list that reads exactly like a device
    # which does not offer the service. Measured on a disconnected peer, on
    # an unrecorded Windows version: the filtered query returned nothing and
    # left it disconnected, the unfiltered query returned eleven services
    # including the one wanted and connected to do it, and the filtered query
    # then worked. Asking for everything is the only form that reliably goes
    # and looks.
    result = await found.get_rfcomm_services_with_cache_mode_async(
        BluetoothCacheMode.UNCACHED
    )
    if result.error != BluetoothError.SUCCESS:
        raise translate_bluetooth_error(result.error, address)

    # A device that does not answer is not an error to this query. Measured
    # on Windows 10.0.26200.9457, for a paired device switched off and for an
    # address Windows had never seen: SUCCESS with no services after about
    # 5 s, the device still disconnected. One that answered is connected by
    # then.
    if (
        not result.services
        and found.connection_status == BluetoothConnectionStatus.DISCONNECTED
    ):
        raise DeviceNotFoundError(address)

    offered = [s for s in result.services if s.service_id.uuid == service]
    if not offered:
        raise ServiceNotFoundError(f"{address} does not offer {service}")

    if len(offered) > 1:
        # A device may publish the same service class more than once, on
        # different channels. The first is taken, to match macOS, whose
        # getServiceRecordForUUID returns a single record and so cannot offer
        # a choice at all. BlueZ likewise picks inside ConnectProfile without
        # saying which. Windows is the only platform where the choice is even
        # visible, and making it behave differently here would mean the same
        # device answered differently depending on what it was called from.
        # Recorded rather than hidden, since a caller who needs a particular
        # one can ask for its channel number directly.
        logger.debug(
            "%s publishes %d records for %s. Using the first",
            address,
            len(offered),
            service,
        )

    attributes = {
        key: bytes(value)
        for key, value in (
            await offered[0].get_sdp_raw_attributes_with_cache_mode_async(
                BluetoothCacheMode.UNCACHED
            )
        ).items()
    }
    try:
        return read_channel(attributes)
    except SdpError as error:
        raise ServiceNotFoundError(
            f"{address} offers {service} but its record does not say how to "
            f"reach it: {error}"
        ) from error
