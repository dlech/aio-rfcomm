# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Turning what WinRT and Winsock report into our errors.
"""

# Measurements cited in this module were made by 2026-09-20 on a Windows
# machine whose version was not recorded, unless a claim says it was made on
# 2026-10-15. Those were made on Windows 10.0.26200.9457 (25H2), with
# bthport.sys, bthenum.sys and bthusb.sys 10.0.26100.9444, and rfcomm.sys and
# bthserv.dll 10.0.26100.9278. With any new one, record the same: Windows
# updates the Bluetooth stack through servicing, apart from its own version.

from __future__ import annotations

from winrt.windows.devices.bluetooth import (
    BluetoothError,
)

from aio_rfcomm.availability import Availability
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
)

__all__ = ["translate_bluetooth_error", "translate_refusal"]

# What a connect to a channel the device refuses fails with.
_WSAEHOSTDOWN = 10064


def translate_bluetooth_error(error: BluetoothError, address: str) -> RfcommError:
    """
    Turn a WinRT Bluetooth error into one of ours.

    Args:
        error: What WinRT reported.
        address: The device being reached.

    Returns:
        The error to raise instead.
    """
    if error == BluetoothError.DEVICE_NOT_CONNECTED:
        return DeviceNotFoundError(f"{address} did not answer")
    if error == BluetoothError.RADIO_NOT_AVAILABLE:
        return BluetoothUnavailableError(
            Availability.SWITCHED_OFF, "the Bluetooth radio is not available"
        )
    return ConnectionFailedError(f"could not read {address}'s services: {error.name}")


def translate_refusal(error: OSError, address: str, channel: int) -> RfcommError | None:
    """
    Tell a refused channel from the flakiness that is worth trying again.

    Args:
        error: What the connect raised.
        address: The device's Bluetooth address.
        channel: The RFCOMM channel number.

    Returns:
        The error to raise for a refusal, or ``None`` for anything else.
    """
    # Measured on 2026-10-15 against a macOS 26.6.2 peer: a channel nothing
    # listens on fails with WSAEHOSTDOWN as soon as the peer refuses it, while
    # a device that is switched off, and an address never seen, time out with
    # WSAETIMEDOUT. A link refused for authentication is not measured. Asking
    # again is refused again, and Windows holds the next attempt on that
    # device for about a minute, until its link to it has gone.
    if getattr(error, "winerror", None) == _WSAEHOSTDOWN:
        return ServiceNotFoundError(
            f"{address} refused channel {channel}. Usually nothing there is "
            "listening on it"
        )

    return None
