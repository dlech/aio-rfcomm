# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The parts of IOBluetooth and CoreBluetooth this layer calls, typed, and
the small pieces every other module here needs.
"""

import ctypes
import itertools
import sys
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

assert not TYPE_CHECKING or sys.platform == "darwin"

from rubicon.objc import (
    SEL,
    NSObject,
    ObjCClass,
    ObjCInstance,
    ObjCProtocol,
    py_from_ns,
)
from rubicon.objc.api import NSData, NSString
from rubicon.objc.runtime import libc, load_library, objc_id
from typing_extensions import override

if TYPE_CHECKING:
    from aio_rfcomm.backend.iobluetooth.helper.native.channel import ChannelDelegate
    from aio_rfcomm.backend.iobluetooth.helper.native.connecting import (
        ConnectionComplete,
    )

# Loading the framework is what makes its classes findable by name. It does
# not touch the radio, so it does not trigger the permission check that ends
# the process. The first call that needs Bluetooth does.
load_library("IOBluetooth")

# CoreBluetooth is loaded only for CBManager.authorization, which is the one
# way to tell "the user has not allowed Bluetooth" from "the radio is off".
# Reading it does not prompt and does not touch the radio.
load_library("CoreBluetooth")


# --------------------------------------------------------------------------
# Typed views of the framework
#
# rubicon looks methods up at runtime, so a class it hands back has nothing a
# type checker can see. Declaring the handful we call keeps the call sites
# checked rather than littered with ignores, and doubles as the list of what
# this layer actually depends on. Objective-C spellings are kept exactly, so
# they can be looked up in Apple's documentation.
# --------------------------------------------------------------------------


class _UUID(Protocol):
    """
    An ``IOBluetoothSDPUUID``, which is an ``NSData`` of 2, 4 or 16 bytes.
    """

    def getUUIDWithLength(self, length: int, /) -> "_UUID": ...


class _DataElement(Protocol):
    def getTypeDescriptor(self) -> int: ...
    def getUUIDValue(self) -> _UUID: ...
    def getArrayValue(self) -> "Iterable[_DataElement] | None": ...


class NativeRecord(Protocol):
    # A ``ctypes.byref`` result, whose type ctypes keeps private. Same below.
    def getRFCOMMChannelID(self, out: Any, /) -> int: ...
    def getAttributeDataElement(self, attribute: int, /) -> _DataElement | None: ...


class _Connection(Protocol):
    # CoreBluetooth's CBRFCOMMChannel, private, under an IOBluetooth channel.
    @property
    def channelID(self) -> int: ...


class NativeChannel(Protocol):
    # Private, and nil until bluetoothd has connected the channel.
    @property
    def rfcommchannel(self) -> _Connection | None: ...
    def isOpen(self) -> bool: ...
    def getMTU(self) -> int: ...
    def closeChannel(self) -> int: ...
    def writeAsync(self, data: bytes, /, *, length: int, refcon: int) -> int: ...


class _Notification(Protocol):
    def unregister(self) -> None: ...


class _PublishedRecord(Protocol):
    def removeServiceRecord(self) -> int: ...


class NativeDevice(Protocol):
    @property
    def addressString(self) -> NSString: ...
    @property
    def name(self) -> NSString | None: ...
    # The services macOS stored for the device, as records.
    @property
    def services(self) -> Iterable[NativeRecord] | None: ...
    def isConnected(self) -> bool: ...
    def openConnection(self, target: "ConnectionComplete", /) -> int: ...
    def openRFCOMMChannelAsync(
        self, channel: Any, /, *, withChannelID: int, delegate: "ChannelDelegate"
    ) -> int: ...


class _Controller(Protocol):
    @property
    def powerState(self) -> int: ...
    def addressAsString(self) -> NSString | None: ...
    def nameAsString(self) -> NSString | None: ...


class _DeviceClass(Protocol):
    def deviceWithAddressString(self, address: str, /) -> NativeDevice | None: ...
    def pairedDevices(self) -> Iterable[NativeDevice] | None: ...


class _ControllerClass(Protocol):
    def defaultController(self) -> _Controller | None: ...


class _ChannelClass(Protocol):
    # Mostly reached by wrapping a pointer the framework filled in rather than
    # by calling a method on it, so what there is to describe is the call.
    def __call__(self, pointer: objc_id, /) -> NativeChannel: ...
    def registerForChannelOpenNotifications(
        self,
        observer: NSObject,
        /,
        *,
        selector: SEL,
        withChannelID: int,
        direction: int,
    ) -> _Notification: ...


class _RecordClass(Protocol):
    def publishedServiceRecordWithDictionary(
        self, record: ObjCInstance, /
    ) -> _PublishedRecord | None: ...


class _UUIDClass(Protocol):
    def uuidWithData(self, data: bytes, /) -> _UUID: ...
    def uuid16(self, value: int, /) -> _UUID: ...


def get_class(name: str) -> Any:
    """
    Look up an Objective-C class by name.

    Args:
        name: The class's name.

    Returns:
        The class. Missing classes cannot happen once the framework is
        loaded, and would be a mistake in this module rather than anything a
        caller could act on.
    """
    found = ObjCClass(name)
    assert found is not None, f"IOBluetooth has no class {name}"
    return found


def find_class(name: str) -> Any:
    """
    Look up a private Objective-C class, which a future macOS may lack.

    Args:
        name: The class's name.

    Returns:
        The class. Where it is missing, whatever rubicon does about that is
        left for the caller to catch.
    """
    return ObjCClass(name)


IOBluetoothDevice = cast(_DeviceClass, get_class("IOBluetoothDevice"))
IOBluetoothHostController = cast(
    _ControllerClass, get_class("IOBluetoothHostController")
)
IOBluetoothSDPUUID = cast(_UUIDClass, get_class("IOBluetoothSDPUUID"))
IOBluetoothRFCOMMChannel = cast(_ChannelClass, get_class("IOBluetoothRFCOMMChannel"))
IOBluetoothSDPServiceRecord = cast(
    _RecordClass, get_class("IOBluetoothSDPServiceRecord")
)
IOBluetoothRFCOMMChannelDelegate = ObjCProtocol("IOBluetoothRFCOMMChannelDelegate")

# Looked up where they are used rather than beside the others above: they are
# private, and at module scope a class a future macOS lacks would take the
# backend down. See find_class.
COORDINATOR = "IOBluetoothCoreBluetoothCoordinator"
DEVICE_MANAGER = "BluetoothDeviceManager"


if TYPE_CHECKING:

    def _mach_error_string(code: int) -> bytes: ...

else:
    _mach_error_string = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_int)(
        ("mach_error_string", libc), ((1, "code"),)
    )


class _Completion(Protocol):
    """
    A target handed to an asynchronous IOBluetooth call.
    """

    token: int


# IOBluetooth stores the target of an asynchronous call unretained (Apple
# FB13705522), so one has to be held here until its callback fires: a canceled
# caller's frame goes away while the call is still outstanding, and the
# framework would then message a deallocated object. A callback that never
# arrives leaks one small object, which is the cheaper of the two failures.
awaiting_completion: dict[int, _Completion] = {}
completion_tokens = itertools.count()


def hold(complete: _Completion) -> None:
    """
    Keep a completion object alive until its callback takes it back out.

    Args:
        complete: The target handed to an asynchronous IOBluetooth call.
    """
    awaiting_completion[complete.token] = complete


def release(complete: _Completion) -> None:
    """
    Let go of one whose call never started.

    Does nothing once the callback has already taken it out, so it is safe on
    any path that is not cancellation.

    Args:
        complete: The target that was held.
    """
    awaiting_completion.pop(complete.token, None)


class MachError(Exception):
    """
    A failure reported as a mach status code.

    The numbering is the operating system's own and spans its subsystems --
    the kernel, IOKit and the rest -- so a status is only meaningful once it
    is known which one it came from. That is also why one can arrive
    described in terms of something with no bearing on what was attempted.
    ``mach_error_string`` renders any of them.
    """

    def __init__(self, code: int) -> None:
        """
        Args:
            code: The status that was returned.
        """
        super().__init__(code, _mach_error_string(code).decode())

    @property
    def code(self) -> int:
        """
        The status that was returned.
        """
        return cast(int, self.args[0])

    @override
    def __str__(self) -> str:
        return f"[mach error {self.code}] {self.args[1]}"


def check(code: int) -> None:
    """
    Turn a non-zero status into an exception.

    Args:
        code: The status to check.

    Raises:
        MachError: The status was non-zero.
    """
    if code:
        raise MachError(code)


class _ManagerClass(Protocol):
    """
    The one class property we want from ``CBManager``.
    """

    @property
    def authorization(self) -> int: ...


CBManager = cast(_ManagerClass, get_class("CBManager"))


def python_uuid(uuid: _UUID) -> UUID:
    """
    Turn the framework's UUID into a Python one.

    Args:
        uuid: A UUID of any length.

    Returns:
        The full 128-bit UUID.
    """
    widened = cast(NSData, uuid.getUUIDWithLength(16))
    return UUID(bytes=py_from_ns(widened))


def each(service: UUID | Collection[UUID] | None) -> list[UUID]:
    """
    Normalize the service filter to a list.

    Args:
        service: One service, several, or ``None`` for no filter.

    Returns:
        The services to match, empty for no filter.
    """
    if service is None:
        return []
    if isinstance(service, UUID):
        return [service]
    return list(service)


def sdp_uuid(service: UUID) -> _UUID:
    """
    Turn a Python UUID into the framework's own.

    Args:
        service: The service UUID.

    Returns:
        The matching ``IOBluetoothSDPUUID``.
    """
    return IOBluetoothSDPUUID.uuidWithData(service.bytes)


def normalize(address: str) -> str:
    """
    Put a Bluetooth address into the form this library reports.

    On macOS 26.6.2, IOBluetooth writes device addresses with dashes and in
    lower case, and controller addresses with colons and in upper case. Callers
    compare these against addresses from other platforms, so they are all made
    to look the same.

    Args:
        address: The address as IOBluetooth wrote it.

    Returns:
        The address, colon separated and upper case.
    """
    return address.replace("-", ":").upper()
