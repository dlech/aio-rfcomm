# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Errors raised by aio-rfcomm.

Errors that Bluetooth or the other device cause derive from
[`RfcommError`][RfcommError], and are the same on every platform. The platform's own
error, if there is one, is kept as ``__cause__``.

Errors caused by mistakes in your own code derive from ``RuntimeError``
instead, so catching [`RfcommError`][RfcommError] never hides a bug.
"""

from typing import Self

from typing_extensions import override

from aio_rfcomm.availability import Availability, UnavailableReason

__all__ = [
    "BluetoothAlreadyOpenError",
    "BluetoothUnavailableError",
    "BusyResourceError",
    "ChannelClosedError",
    "ChannelInUseError",
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
    Bluetooth cannot be used. [`reason`][reason] says why.
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
        Why Bluetooth cannot be used. See [`Availability`][aio_rfcomm.Availability].
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


class ChannelClosedError(RfcommError):
    """
    The connection has ended: the other device hung up, or the connection to
    it was lost.

    Once a connection has ended, and any bytes that arrived before the end
    have been received, every call to [`receive`][aio_rfcomm.RfcommChannel.receive]
    and [`send`][aio_rfcomm.RfcommChannel.send] raises this.
    """

    def __init__(self) -> None:
        super().__init__("the connection has ended")

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[()]]:
        # The message is not an argument, so it is not passed back.
        return type(self), ()


class DelimiterNotFoundError(RfcommError):
    """
    [`receive_until`][aio_rfcomm.RfcommChannel.receive_until] found no delimiter within
    ``max_bytes``.

    No bytes were read, so [`receive`][aio_rfcomm.RfcommChannel.receive] can still
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


class BusyResourceError(RuntimeError):
    """
    Two tasks received on one channel at the same time, or two tasks sent.

    One task can send while another receives.
    """


class ScopeClosedError(RuntimeError):
    """
    Something was used after its ``async with`` block ended.
    """
