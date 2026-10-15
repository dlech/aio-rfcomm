# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Numbers the frameworks hand back, under the names Apple publishes for them.

Each enum mirrors one in an SDK header, with Apple's prefix dropped and its
values kept, because the framework reports them as bare numbers. The exception
is ``OIStatus``, which no header publishes. Nothing here imports rubicon,
so both the library and its helper can use them.
"""

import ctypes
import enum

__all__ = [
    "CBError",
    "CBManagerAuthorization",
    "CBManagerState",
    "IOReturn",
    "OIStatus",
]


class CBManagerAuthorization(enum.IntEnum):
    """
    Whether this program may use Bluetooth, as CoreBluetooth reports it.

    macOS tells a program it may not use that the radio is switched off, so
    this is the only thing that separates the two. The members are
    CoreBluetooth's own ``CBManagerAuthorization`` cases without their shared
    prefix, and carry its values because the helper sends them as numbers.
    """

    NOT_DETERMINED = 0
    """
    ``CBManagerAuthorizationNotDetermined`` -- nobody has been asked yet.
    """

    RESTRICTED = 1
    """
    ``CBManagerAuthorizationRestricted`` -- forbidden by a profile or by
    parental controls, not by a choice the user can reverse.
    """

    DENIED = 2
    """
    ``CBManagerAuthorizationDenied`` -- the user said no.
    """

    ALLOWED_ALWAYS = 3
    """
    ``CBManagerAuthorizationAllowedAlways`` -- the user said yes.
    """


class CBManagerState(enum.IntEnum):
    """
    What a ``CBManager`` says about itself.

    The members are CoreBluetooth's own ``CBManagerState`` cases without their
    shared prefix, and carry its values because the framework reports them as
    numbers. The two temporary ones are kept apart in ``_native._UNSETTLED``,
    since they are not answers.
    """

    UNKNOWN = 0
    """
    ``CBManagerStateUnknown`` -- nothing yet, and an update is promised.
    """

    RESETTING = 1
    """
    ``CBManagerStateResetting`` -- the connection to the system service was
    lost and is being remade. Also temporary, also followed by an update.
    """

    UNSUPPORTED = 2
    """
    ``CBManagerStateUnsupported`` -- this machine cannot do it at all. Apple
    words this for the Bluetooth Low Energy roles, but the property is
    ``CBManager``'s rather than its central subclass's, so it is read here as
    the general statement it appears to be.
    """

    UNAUTHORIZED = 3
    """
    ``CBManagerStateUnauthorized`` -- this program may not use Bluetooth.
    """

    POWERED_OFF = 4
    """
    ``CBManagerStatePoweredOff`` -- the radio is switched off.
    """

    POWERED_ON = 5
    """
    ``CBManagerStatePoweredOn`` -- usable.
    """


class CBError(enum.IntEnum):
    """
    CoreBluetooth's ``CBError`` codes, the ones ``CBError.h`` offers on macOS.

    IOBluetooth sometimes passes one of these where it documents an IOKit
    status: CoreBluetooth turns the daemon's result into an ``NSError`` and
    hands on only its code. A result it has no name for becomes
    ``UNKNOWN``, which is 0 and so reads as success.
    """

    UNKNOWN = 0
    INVALID_PARAMETERS = 1
    INVALID_HANDLE = 2
    NOT_CONNECTED = 3
    OUT_OF_SPACE = 4
    OPERATION_CANCELLED = 5
    CONNECTION_TIMEOUT = 6
    PERIPHERAL_DISCONNECTED = 7
    UUID_NOT_ALLOWED = 8
    ALREADY_ADVERTISING = 9
    CONNECTION_FAILED = 10
    CONNECTION_LIMIT_REACHED = 11
    UNKNOWN_DEVICE = 12
    OPERATION_NOT_SUPPORTED = 13
    PEER_REMOVED_PAIRING_INFORMATION = 14
    ENCRYPTION_TIMED_OUT = 15
    TOO_MANY_LE_PAIRED_DEVICES = 16


def _iokit_common(code: int) -> int:
    """
    Build an IOKit status the way ``iokit_common_err`` in ``IOReturn.h`` does.

    The system (0x38, I/O Kit) and the subsystem (0, common) sit above the
    code, giving 0xE00000xx. Statuses arrive as signed 32-bit integers, which
    is why they show up in tracebacks as large negative numbers.

    Args:
        code: The code within the common subsystem.

    Returns:
        The status as the framework reports it.
    """
    return ctypes.c_int32(0xE0000000 | code).value


class IOReturn(enum.IntEnum):
    """
    The ``kIOReturn`` statuses from ``IOReturn.h`` that the framework is known
    to hand back.
    """

    NOT_OPEN = _iokit_common(0x2CD)
    """
    ``kIOReturnNotOpen`` -- a write to a channel that has closed.
    """

    TIMEOUT = _iokit_common(0x2D6)
    """
    ``kIOReturnTimeout`` -- what a device that did not answer amounts to.
    """

    UNDERRUN = _iokit_common(0x2E7)
    """
    ``kIOReturnUnderrun`` -- a write to a channel whose daemon restarted.
    """


class OIStatus(enum.IntEnum):
    """
    The results of bluetoothd's own Bluetooth stack that the library acts on.

    bluetoothd reports these with each channel open, and CoreBluetooth hands
    IOBluetooth 0 for both, so the open is never reported at all. The names are
    the stack's own, read from the status-name table in bluetoothd on macOS
    26.6.2 (25G83).
    """

    RFCOMM_REMOTE_REJECT = 913
    """
    ``OI_RFCOMM_REMOTE_REJECT`` -- the device refused the channel. Measured
    against BlueZ 5.85 for a channel nothing listens on.
    """

    RFCOMM_REMOTE_DEVICE_DISCONNECTED = 927
    """
    ``OI_RFCOMM_REMOTE_DEVICE_DISCONNECTED`` -- the device hung up as the
    channel opened. Measured against a BlueZ 5.85 server declining it.
    """
