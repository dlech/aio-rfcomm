# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Errors raised by aio-rfcomm.

Every failure surfaces as an :class:`RfcommError`. Backends wrap the platform
exception rather than letting it through, so error handling is the same on
every platform; the original is kept as ``__cause__`` for diagnosis.

Grouped below by what raises them: opening an adapter, using one, opening a
channel, using one, and finally misuse of the API itself.
"""

import enum

__all__ = [
    "AdapterLostError",
    "AdapterLostReason",
    "AdapterNotFoundError",
    "AdapterOffError",
    "ChannelBrokenError",
    "ChannelBusyError",
    "ChannelClosedError",
    "CloseReason",
    "ConnectionFailedError",
    "DeviceNotFoundError",
    "PermissionDeniedError",
    "RfcommError",
    "ScopeClosedError",
    "ServiceNotFoundError",
    "UnsupportedPlatformError",
]


class RfcommError(Exception):
    """
    Base class for every error raised by this library.
    """


# --------------------------------------------------------------------------
# Opening an adapter
# --------------------------------------------------------------------------


class UnsupportedPlatformError(RfcommError):
    """
    This platform has no RFCOMM support.
    """


class PermissionDeniedError(RfcommError):
    """
    The OS refused access to Bluetooth.

    On macOS this is also what a missing ``NSBluetoothAlwaysUsageDescription``
    looks like once the helper process has been reaped, since the abort itself
    is not catchable in the calling process.
    """


class AdapterNotFoundError(RfcommError):
    """
    No Bluetooth adapter matched, or the machine has none.
    """


class AdapterOffError(RfcommError):
    """
    The adapter is present but switched off.

    Distinct from :class:`AdapterNotFoundError`, which means there is no such
    adapter at all: this one the user can fix by turning Bluetooth on.

    Every platform can see this -- ``Adapter1.Powered`` on Linux,
    ``IOBluetoothHostController.powerState`` on macOS, and the radio state on
    Windows.
    """


# --------------------------------------------------------------------------
# Using an adapter
# --------------------------------------------------------------------------


class AdapterLostReason(enum.Enum):
    """
    Why an adapter is no longer usable.
    """

    SWITCHED_OFF = "the adapter was switched off"
    """
    The local radio was turned off.
    """

    REMOVED = "the adapter was removed"
    """
    The adapter was unplugged or otherwise disappeared from the system.
    """


class AdapterLostError(RfcommError):
    """
    The adapter went away while it was in use.

    Raised by :meth:`~aio_rfcomm.RfcommAdapter.fail_when_gone`.
    :attr:`reason` says why.
    """

    def __init__(self, reason: AdapterLostReason) -> None:
        """
        Args:
            reason: Why the adapter is gone.
        """
        super().__init__(reason.value)
        self.reason = reason
        """
        Why the adapter is gone.
        """


# --------------------------------------------------------------------------
# Opening a channel
# --------------------------------------------------------------------------


class DeviceNotFoundError(RfcommError):
    """
    The device is not known to the OS and could not be reached.
    """


class ServiceNotFoundError(RfcommError):
    """
    The device does not offer the requested service.

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

    ADAPTER_OFF = "the adapter was switched off"
    """
    The local radio was turned off while the channel was open.
    """


class ChannelClosedError(RfcommError):
    """
    The channel is no longer usable.

    :meth:`~aio_rfcomm.RfcommChannel.receive` raises this only when the
    channel went away for a reason other than
    :attr:`CloseReason.PEER_CLOSED`; an orderly hang-up by the peer is not an
    error there, it ends the stream and returns ``b""``.

    :meth:`~aio_rfcomm.RfcommChannel.fail_when_gone` raises it for every
    reason, since a caller who asked to stop when the channel closes meant all
    of them. :attr:`reason` says which.
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


class ChannelBrokenError(RfcommError):
    """
    The channel is no longer usable because a send was cancelled.

    A cancelled :meth:`~aio_rfcomm.RfcommChannel.send` leaves an unknown
    number of bytes delivered, so the channel cannot be trusted afterwards.
    Close it.
    """


class ChannelBusyError(RfcommError):
    """
    Two tasks used one direction of a channel at the same time.

    One task sends and one task receives; the two directions are independent
    and do not block each other. Sharing a single direction interleaves the
    two callers' data unpredictably, so it is reported as the caller's bug
    rather than tolerated as a race.
    """


# --------------------------------------------------------------------------
# Misuse
# --------------------------------------------------------------------------


class ScopeClosedError(RfcommError):
    """
    A handle was used after the block that produced it ended.

    Adapters, devices and channels are only meaningful inside their
    ``async with``. Keeping one past the end of the block is a bug in the
    caller, so it is reported at once rather than left to fail somewhere less
    obvious.
    """
