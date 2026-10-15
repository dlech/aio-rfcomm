# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Plain descriptions of devices.

These carry only what does not change while you hold them. Anything that can
change underneath the caller -- whether a device is connected, whether it is
still paired -- is deliberately absent, because an attribute captured at one
moment invites a polling loop and is stale the instant it is read.
"""

import re
from dataclasses import dataclass

__all__ = ["RfcommDeviceInfo"]

# Six two-digit groups, all separated the same way: the back reference makes a
# mix of colons and dashes fail to match.
_ADDRESS = re.compile(r"[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}")


@dataclass(eq=False, frozen=True, slots=True)
class RfcommDeviceInfo:
    """
    A device, as far as the OS or the caller knows it.

    Usually from a listing, but an address is enough to make one, and that is
    how a program that starts from a hard-coded or typed-in address names its
    device::

        server = RfcommDeviceInfo("00:1a:7d:da:71:0c")

    The address is checked and normalised as the description is made, so a
    mistyped one fails here rather than as a device that never answers.

    Raises:
        ValueError: ``address`` is not a Bluetooth address.
    """

    address: str
    """
    The device's Bluetooth address, colon-separated and upper case.

    Accepted as six two-digit hex groups in either case, separated all by
    colons or all by dashes.
    """

    name: str | None = None
    """
    The device's name, if the OS knows one.

    Not reliable for identification: the OS may be reporting a stale cached
    name. Match on :attr:`address`.
    """

    id: str | None = None
    """
    Opaque, platform-specific handle for the device.

    Set where the platform has something worth carrying -- BlueZ's object
    path, say -- so the backend can reach the device again without rebuilding
    a handle from the address. ``None`` where it has not, either because the
    device was named by bare address or because the address is all the
    platform needs, as on macOS. Never inspect it; its meaning differs on
    every platform, and on some there is nothing in it.
    """

    def __post_init__(self) -> None:
        if not _ADDRESS.fullmatch(self.address):
            raise ValueError(f"{self.address!r} is not a Bluetooth address")
        # Frozen, so set past the dataclass's own guard.
        object.__setattr__(self, "address", self.address.replace("-", ":").upper())
