# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Whether Bluetooth can be used, and why not.

One enum covers both answers, so that whatever reports availability reports a
named value every time -- including the one that means usable. Errors carry
only the reasons, through [`UnavailableReason`][UnavailableReason], so that matching on an
error's reason never has a case that cannot happen.
"""

import enum
from typing import Literal, TypeAlias

__all__ = ["Availability", "UnavailableReason"]


class Availability(enum.Enum):
    """
    Whether Bluetooth on this computer can be used, and if not, why not.
    """

    USABLE = enum.auto()
    """
    Bluetooth can be used.
    """

    SWITCHED_OFF = enum.auto()
    """
    Bluetooth is turned off. The user can turn it on.
    """

    NO_ADAPTER = enum.auto()
    """
    The computer has no Bluetooth adapter. The user can plug one in.
    """

    NOT_ALLOWED = enum.auto()
    """
    This program does not have permission to use Bluetooth. The user can
    usually give it permission in the system settings.
    """

    SERVICE_UNAVAILABLE = enum.auto()
    """
    The system's Bluetooth service is not running. Wait a moment. It usually
    restarts by itself.
    """

    UNSUPPORTED = enum.auto()
    """
    This system cannot use Bluetooth at all.
    """

    # Added last, so that the members before it keep their values.
    AWAITING_PERMISSION = enum.auto()
    """
    The system is asking the user whether this program may use Bluetooth.
    The availability changes when they answer.
    """


UnavailableReason: TypeAlias = Literal[
    Availability.SWITCHED_OFF,
    Availability.NO_ADAPTER,
    Availability.NOT_ALLOWED,
    Availability.AWAITING_PERMISSION,
    Availability.SERVICE_UNAVAILABLE,
    Availability.UNSUPPORTED,
]
"""
Every [`Availability`][Availability] except [`USABLE`][Availability.USABLE]: the reasons
Bluetooth cannot be used. A type checker can confirm that a ``match`` on one
covers every case.
"""
