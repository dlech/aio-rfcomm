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
    A Bluetooth device.

    :meth:`~aio_rfcomm.Bluetooth.list_known_devices` gives these, and you can
    also make one from an address::

        device = RfcommDeviceInfo("00:1A:7D:DA:71:0C")

    Raises:
        ValueError: ``address`` is not a Bluetooth address.
    """

    address: str
    """
    The device's Bluetooth address, in upper case with colons.

    Give it as six pairs of hex digits, in either case, separated by colons or
    by dashes.
    """

    name: str | None = None
    """
    The device's name, if the system knows one.

    It can be out of date, so match devices on :attr:`address` instead.
    """

    id: str | None = None
    """
    The library's own handle for the device. Leave it alone.
    """
    # BlueZ's object path, where there is one, so the backend need not rebuild
    # it from the address. None otherwise.

    def __post_init__(self) -> None:
        if not _ADDRESS.fullmatch(self.address):
            raise ValueError(f"{self.address!r} is not a Bluetooth address")
        # Frozen, so set past the dataclass's own guard.
        object.__setattr__(self, "address", self.address.replace("-", ":").upper())
