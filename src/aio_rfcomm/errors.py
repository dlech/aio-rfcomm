# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Errors raised by aio-rfcomm.

Every failure that Bluetooth or the peer causes surfaces as an
:class:`RfcommError`, which is what a program catches. Backends wrap the
platform exception rather than letting it through, so error handling is the
same on every platform; the original is kept as ``__cause__`` for diagnosis.

Mistakes in the calling code are not :class:`RfcommError`: they are
:class:`RuntimeError` subclasses, grouped at the end, so that catching
:class:`RfcommError` never hides a bug.

Grouped below by what raises them first. The grouping is a reading aid rather
than a promise.
"""

import enum
from typing import Self

from typing_extensions import override

from aio_rfcomm.availability import Availability, UnavailableReason

__all__ = [
    "BluetoothAlreadyOpenError",
    "BluetoothUnavailableError",
    "ChannelBusyError",
    "ChannelClosedError",
    "ChannelInUseError",
    "CloseReason",
    "ConnectionFailedError",
    "DelimiterNotFoundError",
    "DeviceNotFoundError",
    "RfcommError",
    "ScopeClosedError",
    "ServiceNotFoundError",
    "UnsupportedOperationError",
]


class RfcommError(Exception):
    """
    Base class for every error raised by this library.
    """


# --------------------------------------------------------------------------
# Bluetooth itself
# --------------------------------------------------------------------------

# What to say about each reason when the platform has nothing more specific.
_DESCRIPTIONS: dict[UnavailableReason, str] = {
    Availability.SWITCHED_OFF: "Bluetooth is switched off",
    Availability.NO_ADAPTER: "this computer has no Bluetooth adapter",
    Availability.NOT_ALLOWED: "this program may not use Bluetooth",
    Availability.SERVICE_UNAVAILABLE: "the Bluetooth service is not running",
    Availability.UNSUPPORTED: "this system has no Bluetooth",
}


class BluetoothUnavailableError(RfcommError):
    """
    Bluetooth cannot be used.

    Raised by anything attempted while Bluetooth is unusable. :attr:`reason`
    says why.
    """

    def __init__(self, reason: UnavailableReason, detail: str | None = None) -> None:
        """
        Args:
            reason: Why Bluetooth cannot be used.
            detail: What the platform said, when it says more than the reason
                does -- what to do about a refused permission, say.
        """
        super().__init__(detail or _DESCRIPTIONS[reason])
        self.reason: UnavailableReason = reason
        """
        Why Bluetooth cannot be used.
        """

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[UnavailableReason, str]]:
        # Rebuilt from the reason, not from the message alone, so that copying
        # or pickling one gives back the same error.
        return type(self), (self.reason, str(self))


# --------------------------------------------------------------------------
# Opening a channel
# --------------------------------------------------------------------------


class DeviceNotFoundError(RfcommError):
    """
    The device is not known to the OS and could not be reached.
    """


class ServiceNotFoundError(RfcommError):
    """
    The device does not offer the requested service, or, opening by channel
    number on macOS or Linux, refused the channel -- usually because nothing
    there is listening on it.

    The service record may simply not be cached. Refreshing it is not
    something this library can do on every platform; see the design notes.
    """


class ConnectionFailedError(RfcommError):
    """
    The channel could not be opened.

    Raised after any retries have been exhausted.
    """


# --------------------------------------------------------------------------
# Using a channel
# --------------------------------------------------------------------------


class CloseReason(enum.Enum):
    """
    Why a channel is no longer usable.

    There is no member for "this side closed it". Closing happens by leaving
    the ``async with`` block, and by then there is nobody left inside it to be
    told.
    """

    PEER_CLOSED = "the peer closed the channel"
    """
    The far end hung up in an orderly way. The stream simply ended.
    """

    LINK_LOST = "the link was lost"
    """
    The baseband connection went away -- the device moved out of range, lost
    power, or dropped the link.
    """


class ChannelClosedError(RfcommError):
    """
    The channel is over.

    Every ending is raised, the peer hanging up included: by
    :meth:`~aio_rfcomm.RfcommChannel.send`, and by
    :meth:`~aio_rfcomm.RfcommChannel.receive` once whatever arrived before the
    end has been taken, with the same :attr:`reason` every time.
    """

    def __init__(self, reason: CloseReason) -> None:
        """
        Args:
            reason: Why the channel is gone.
        """
        super().__init__(reason.value)
        self.reason = reason
        """
        Why the channel is gone.
        """

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[CloseReason]]:
        # Rebuilt from the reason, not from the message it was turned into.
        return type(self), (self.reason,)


class DelimiterNotFoundError(RfcommError):
    """
    :meth:`~aio_rfcomm.RfcommChannel.receive_until` looked through as many
    bytes as it was allowed without finding its delimiter.

    The peer sent something the program did not expect -- a line longer than
    any it should send, say. Nothing was taken, so what arrived can still be
    read with :meth:`~aio_rfcomm.RfcommChannel.receive`.
    """


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


class ChannelInUseError(RfcommError):
    """
    The RFCOMM channel a service asked to listen on is already taken.

    Either by another program on this machine or by another service in this
    one. Pick a different channel, or let the library choose by leaving the
    channel unset.
    """


class UnsupportedOperationError(RfcommError):
    """
    This platform cannot do that, even though it has a backend.

    Not every platform can do everything. Connecting to a bare RFCOMM channel
    number on Linux, for instance, needs a Bluetooth socket, and the Python
    builds that ship without Bluetooth support cannot open one -- while BlueZ
    offers no way to do it over D-Bus. The message says what would make it
    work.
    """


# --------------------------------------------------------------------------
# Mistakes in the calling code
# --------------------------------------------------------------------------


class BluetoothAlreadyOpenError(RuntimeError):
    """
    Bluetooth was opened while it was already open in this process.

    There is one Bluetooth object per process. Code that needs Bluetooth takes
    that object as an argument rather than opening its own.
    """


class ChannelBusyError(RuntimeError):
    """
    Two tasks used one direction of a channel at the same time.

    One task sends and one task receives; the two directions are independent
    and do not block each other. Sharing a single direction interleaves the
    two callers' data unpredictably, so it is reported as the caller's bug
    rather than tolerated as a race.
    """


class ScopeClosedError(RuntimeError):
    """
    A handle was used after the block that produced it ended.

    The Bluetooth object, channels and services are only meaningful inside
    their ``async with``. Keeping one past the end of the block is a bug in the
    caller, so it is reported at once rather than left to fail somewhere less
    obvious.
    """
