# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Turning what BlueZ and the kernel report into our errors.
"""

import errno

from aio_rfcomm.errors import (
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
)

__all__ = ["translate_connect", "translate_registration", "translate_socket_connect"]


def translate_registration(error: Exception, uuid: str) -> RfcommError:
    """
    Turn a failure to register a profile into one of ours.

    Args:
        error: What BlueZ raised.
        uuid: The service UUID being registered.

    Returns:
        The error to raise instead.
    """
    # RegisterProfile answers NotPermitted for this alone, in the source of
    # BlueZ 5.85 (src/profile.c), with "UUID already registered".
    if getattr(error, "type", None) == "org.bluez.Error.NotPermitted":
        return ConnectionFailedError(
            f"another program on this machine has already claimed {uuid}. "
            "BlueZ allows only one profile registration per service UUID."
        )
    return ConnectionFailedError(f"could not register a profile for {uuid}: {error}")


def translate_connect(error: Exception, address: str, uuid: str) -> RfcommError:
    """
    Turn a BlueZ connection error into one of ours.

    Args:
        error: What BlueZ raised.
        address: The device being connected to.
        uuid: The service being connected to.

    Returns:
        The error to raise instead.
    """
    name = getattr(error, "type", None)
    text = str(error)

    # BlueZ names some failures, and gives others as a reason code in the text
    # of the generic org.bluez.Error.Failed. Measured on BlueZ 5.85: a missing
    # profile is named, with the text "No more profiles to connect to", and a
    # device that is switched off is br-connection-page-timeout.
    if (
        name == "org.bluez.Error.BREDR.ProfileUnavailable"
        or text == "br-connection-profile-unavailable"
    ):
        return ServiceNotFoundError(f"{address} does not offer {uuid}")
    # BlueZ 5.85 gives every other connection failure as one of these codes,
    # an errno turned into a fixed string (src/error.c), never as the errno's
    # own text. A device that does not answer is EHOSTDOWN.
    if text == "br-connection-page-timeout":
        return DeviceNotFoundError(f"{address} did not answer")
    return ConnectionFailedError(f"could not open a channel to {address}: {text}")


def translate_socket_connect(error: OSError, address: str, channel: int) -> RfcommError:
    """
    Turn a failed RFCOMM socket connect into one of ours.

    Args:
        error: What ``connect`` raised.
        address: The device being connected to.
        channel: The channel number.

    Returns:
        The error to raise instead.
    """
    match error.errno:
        case errno.ECONNREFUSED:
            # Any refusal reads the same. Nothing listening is the one measured
            # (BlueZ 5.85, a macOS 26.6.2 peer).
            return ServiceNotFoundError(
                f"{address} refused channel {channel}. Usually nothing there is "
                "listening on it"
            )
        case errno.EHOSTDOWN:
            # Measured (BlueZ 5.85): a device that is not there, after the page
            # timeout.
            return DeviceNotFoundError(
                f"{address} did not answer. It may be switched off or out of range"
            )
        case _:
            return ConnectionFailedError(
                f"could not reach {address} on channel {channel}: {error}"
            )
