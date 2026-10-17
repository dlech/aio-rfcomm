# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Turning what WinRT and Winsock report into our errors.
"""

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

__all__ = ["translate_bluetooth_error", "translate_failure", "translate_refusal"]

# What a connect fails with when the device refuses the channel, and when
# nothing answers, measured on Windows 10.0.26200.9457 and 10.0.26200.9711.
_WSAEHOSTDOWN = 10064
_WSAETIMEDOUT = 10060


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
    # Measured on Windows 10.0.26200.9457 against macOS 26.6.2 and on
    # 10.0.26200.9711 against BlueZ 5.85: a channel nothing listens on fails
    # with WSAEHOSTDOWN as soon as the peer refuses it. A link refused for
    # authentication is not measured. Asking again is refused again, and after
    # a refusal that had to set up the link, Windows holds the next attempt on
    # that device for about a minute.
    if getattr(error, "winerror", None) == _WSAEHOSTDOWN:
        return ServiceNotFoundError(
            f"{address} refused channel {channel}. Usually nothing there is "
            "listening on it"
        )

    return None


def translate_failure(error: OSError, address: str, channel: int) -> RfcommError:
    """
    Say what it means that the last attempt to connect failed too.

    Args:
        error: What the last attempt raised.
        address: The device's Bluetooth address.
        channel: The RFCOMM channel number.

    Returns:
        The error to raise.
    """
    # A device that is on times out now and then too: measured on
    # Windows 10.0.26200.9711, once against BlueZ 5.85 on a link that had to be
    # set up. So only a timeout on every attempt means nothing answered.
    if getattr(error, "winerror", None) == _WSAETIMEDOUT:
        return DeviceNotFoundError(
            f"{address} did not answer. It may be switched off or out of range"
        )

    return ConnectionFailedError(
        f"could not reach {address} on channel {channel}: {error}"
    )
