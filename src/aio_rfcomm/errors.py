# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Errors raised by aio-rfcomm.

Errors that Bluetooth or the other device cause derive from
:class:`RfcommError`, and are the same on every platform. The platform's own
error, if there is one, is kept as ``__cause__``.

Errors caused by mistakes in your own code derive from :class:`RuntimeError`
instead, so catching :class:`RfcommError` never hides a bug.
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
    The base class of every error that Bluetooth or the other device causes.
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
    Bluetooth cannot be used. :attr:`reason` says why.
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
        Why Bluetooth cannot be used. See :class:`~aio_rfcomm.Availability`.
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
    The device did not answer. It may be turned off or out of range.
    """


class ServiceNotFoundError(RfcommError):
    """
    The device does not offer that service, or refused that channel number.

    The system's stored copy of the device's services may also be out of date.
    """


class ConnectionFailedError(RfcommError):
    """
    The channel could not be opened, or the service could not be published.
    """


# --------------------------------------------------------------------------
# Using a channel
# --------------------------------------------------------------------------


class CloseReason(enum.Enum):
    """
    How a connection ended.
    """

    PEER_CLOSED = "the other device hung up"
    """
    The other device hung up.
    """

    LINK_LOST = "the connection was lost"
    """
    The connection to the device was lost. It may have moved out of range or
    been turned off.
    """


class ChannelClosedError(RfcommError):
    """
    The connection has ended. :attr:`reason` says how.

    Once a connection has ended, and any bytes that arrived before the end
    have been received, every call to :meth:`~aio_rfcomm.RfcommChannel.receive`
    and :meth:`~aio_rfcomm.RfcommChannel.send` raises this, with the same
    reason.
    """

    def __init__(self, reason: CloseReason) -> None:
        """
        Args:
            reason: Why the channel is gone.
        """
        super().__init__(reason.value)
        self.reason = reason
        """
        How the connection ended.
        """

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[CloseReason]]:
        # Rebuilt from the reason, not from the message it was turned into.
        return type(self), (self.reason,)


class DelimiterNotFoundError(RfcommError):
    """
    :meth:`~aio_rfcomm.RfcommChannel.receive_until` found no delimiter within
    ``max_bytes``.

    No bytes were read, so :meth:`~aio_rfcomm.RfcommChannel.receive` can still
    read them.
    """


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


class ChannelInUseError(RfcommError):
    """
    The RFCOMM channel you asked to serve on is already in use.

    Pick another channel, or leave it out to let the library choose.
    """


class UnsupportedOperationError(RfcommError):
    """
    This platform cannot do that. The message says what to do instead.
    """


# --------------------------------------------------------------------------
# Mistakes in the calling code
# --------------------------------------------------------------------------


class BluetoothAlreadyOpenError(RuntimeError):
    """
    Bluetooth was opened while it was already open.

    Open it once, and pass the object to the functions that need it.
    """


class ChannelBusyError(RuntimeError):
    """
    Two tasks received on one channel at the same time, or two tasks sent.

    One task can send while another receives.
    """


class ScopeClosedError(RuntimeError):
    """
    Something was used after its ``async with`` block ended.
    """
