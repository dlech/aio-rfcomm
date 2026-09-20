# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Plain descriptions of adapters and devices.

These carry only what does not change while you hold them. Anything that can
change underneath the caller -- whether a device is connected, whether it is
still paired -- is deliberately absent, because an attribute captured at one
moment invites a polling loop and is stale the instant it is read.
"""

from dataclasses import dataclass

__all__ = ["RfcommAdapterInfo", "RfcommDeviceInfo"]


@dataclass(eq=False, frozen=True, slots=True)
class RfcommAdapterInfo:
    """
    A Bluetooth adapter on this machine.
    """

    id: str
    """
    Opaque, platform-specific identifier used to open the adapter.
    """

    address: str | None
    """
    The adapter's own Bluetooth address, if the OS reports one.
    """

    name: str | None
    """
    A human-readable name, if the OS reports one.
    """


@dataclass(eq=False, frozen=True, slots=True)
class RfcommDeviceInfo:
    """
    A device already known to the OS.
    """

    address: str
    """
    The device's Bluetooth address, colon-separated and upper case.
    """

    name: str | None
    """
    The device's name, if the OS knows one.

    Not reliable for identification: the OS may be reporting a stale cached
    name. Match on :attr:`address`.
    """
