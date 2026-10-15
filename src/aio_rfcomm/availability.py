# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Whether Bluetooth can be used, and why not.

One enum covers both answers, so that whatever reports availability reports a
named value every time -- including the one that means usable. Errors carry
only the reasons, through :data:`UnavailableReason`, so that matching on an
error's reason never has a case that cannot happen.
"""

import enum
from typing import Literal, TypeAlias

__all__ = ["Availability", "UnavailableReason"]


class Availability(enum.Enum):
    """
    Whether Bluetooth on this machine can be used, and if not, why not.

    Every member but :attr:`USABLE` is a reason, and says what the user can do
    about it.
    """

    USABLE = enum.auto()
    """
    Bluetooth can be used.
    """

    SWITCHED_OFF = enum.auto()
    """
    Bluetooth is switched off. The user can turn it on; on Linux it may
    instead be blocked, by ``rfkill`` or airplane mode.
    """

    NO_ADAPTER = enum.auto()
    """
    This machine has no Bluetooth adapter. The user can plug one in.
    """

    NOT_ALLOWED = enum.auto()
    """
    This program may not use Bluetooth. Usually the user can allow it in the
    system's settings, but not when a profile or parental controls forbid it.
    """

    SERVICE_UNAVAILABLE = enum.auto()
    """
    The system's Bluetooth service is not running. Usually it comes back by
    itself: on macOS, measured, in about a second and a half.
    """

    UNSUPPORTED = enum.auto()
    """
    This system has no Bluetooth service at all. Nothing the user can do.
    """


UnavailableReason: TypeAlias = Literal[
    Availability.SWITCHED_OFF,
    Availability.NO_ADAPTER,
    Availability.NOT_ALLOWED,
    Availability.SERVICE_UNAVAILABLE,
    Availability.UNSUPPORTED,
]
"""
Every :class:`Availability` but :attr:`~Availability.USABLE`.

A type rather than a class, so members are always spelled
``Availability.SWITCHED_OFF``. Matching on one with a case for each reason and
``assert_never`` in the last is checked as exhaustive.
"""
