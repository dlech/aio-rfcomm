# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Bluetooth addresses, between WinRT's integers and our text.
"""

from __future__ import annotations

__all__ = ["format_address", "parse_address"]


def format_address(address: int) -> str:
    """
    Put a Bluetooth address into the form this library reports.

    Args:
        address: The address as WinRT gives it, a 48-bit integer.

    Returns:
        The address, colon separated and upper case.
    """
    return address.to_bytes(6, "big").hex(":").upper()


def parse_address(address: str) -> int:
    """
    Read an address back into the form WinRT wants.

    Args:
        address: A colon-separated Bluetooth address.

    Returns:
        The address as a 48-bit integer.

    Raises:
        ValueError: It is not a Bluetooth address.
    """
    try:
        return int(address.replace(":", "").replace("-", ""), 16)
    except ValueError as error:
        raise ValueError(f"{address!r} is not a Bluetooth address") from error
