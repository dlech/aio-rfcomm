# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The IOBluetooth layer, as thin a wrapper as the framework allows.

Everything here must run on the main thread of a process whose event loop is
backed by a CFRunLoop. That is not a style preference: an asynchronous
IOBluetooth call made from anywhere else returns success and then never
completes, and ``openRFCOMMChannelSync`` is refused outright. The helper
process exists to provide such a thread. Nothing in this module arranges one.

This module knows nothing about the helper or its control protocol, so the
same code serves an in-process caller that already owns a CFRunLoop main
thread.
"""

# Do not add ``from __future__ import annotations`` to this module. rubicon
# reads the annotations on the delegate methods below at runtime to build
# each one's Objective-C type encoding, and under PEP 563 an annotation is
# stored as its own source text rather than the object, so a callback would
# be given the wrong signature and read rubbish off the stack.

# Measurements cited in this module were made on macOS 26.6.2 (25G83), with a
# BCM_4387 controller on firmware 23.5.636.5345, unless a claim says otherwise.
# The peers were a Linux machine, on BlueZ 5.72 until 2026-10-05 and BlueZ 5.85
# after, and a Pybricks EV3 on firmware 4.1.0b5. Claims that depend on the
# peer name its version.

import asyncio
import ctypes
import itertools
import logging
import os
import socket
import stat
import time
from collections.abc import AsyncGenerator, Callable, Collection, Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn, Protocol, cast
from uuid import UUID

from rubicon.objc import (
    SEL,
    NSObject,
    ObjCClass,
    ObjCInstance,
    ObjCProtocol,
    ns_from_py,
    objc_method,
)
from rubicon.objc.api import NSString
from rubicon.objc.runtime import autoreleasepool, libc, load_library, objc_id
from typing_extensions import override

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth._codes import (
    CBError,
    CBManagerAuthorization,
    CBManagerState,
    IOReturn,
    OIStatus,
)
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelInUseError,
    CloseReason,
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
    UnsupportedOperationError,
)

__all__ = [
    "Channel",
    "MachError",
    "Service",
    "hear_open_results",
    "keep_unread_bytes",
    "list_known_devices",
    "open_channel",
    "read_authorization",
    "read_power_state",
    "record_open_result",
    "report_states",
    "serve",
]

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
# this module actually depends on. Objective-C spellings are kept exactly, so
# they can be looked up in Apple's documentation.
# --------------------------------------------------------------------------


class _UUID(Protocol):
    """
    An ``IOBluetoothSDPUUID``.

    Nothing is ever called on one: it is built from a Python UUID and handed
    straight back to the framework. Naming the type says that much at the
    signatures it appears in, which ``Any`` does not -- though, having no
    members, it cannot check that what arrives really is one.
    """


class _Record(Protocol):
    # A ``ctypes.byref`` result, whose type ctypes keeps private. Same below.
    def getRFCOMMChannelID(self, out: Any, /) -> int: ...


class _Channel(Protocol):
    def isOpen(self) -> bool: ...
    def getMTU(self) -> int: ...
    def closeChannel(self) -> int: ...
    def writeAsync(self, data: bytes, /, *, length: int, refcon: int) -> int: ...


class _IncomingChannel(_Channel, Protocol):
    # Handed over open, with no delegate yet.
    def setDelegate(self, delegate: "_ChannelDelegate", /) -> int: ...
    def getDevice(self) -> "_Device": ...


class _Notification(Protocol):
    def unregister(self) -> None: ...


class _PublishedRecord(Protocol):
    def removeServiceRecord(self) -> int: ...


class _Device(Protocol):
    @property
    def addressString(self) -> NSString: ...
    @property
    def name(self) -> NSString | None: ...
    def isConnected(self) -> bool: ...
    def openConnection(self, target: "_ConnectionComplete", /) -> int: ...
    def closeConnection(self) -> int: ...
    def performSDPQuery(self, target: "_QueryComplete", /) -> int: ...
    def getServiceRecordForUUID(self, uuid: _UUID, /) -> _Record | None: ...
    def openRFCOMMChannelAsync(
        self, channel: Any, /, *, withChannelID: int, delegate: "_ChannelDelegate"
    ) -> int: ...


class _Controller(Protocol):
    @property
    def powerState(self) -> int: ...
    def addressAsString(self) -> NSString | None: ...
    def nameAsString(self) -> NSString | None: ...


class _Observable(Protocol):
    """
    Key-value observing, which every ``NSObject`` has.

    From the ``NSKeyValueObserverRegistration`` category rather than from the
    ``NSObject`` protocol.
    """

    def addObserver(
        self, observer: NSObject, /, *, forKeyPath: str, options: int, context: Any
    ) -> None: ...
    # The context form, which Apple asks for: the two-argument one has to guess
    # which registration to drop when an observer is registered more than once.
    def removeObserver(
        self, observer: NSObject, /, *, forKeyPath: str, context: Any
    ) -> None: ...


class _Manager(_Observable, Protocol):
    # Reached privately, but a ``CBManager``, and its state is public API.
    @property
    def state(self) -> int: ...


class _Coordinator(Protocol):
    @property
    def classicManager(self) -> _Manager: ...


class _CoordinatorClass(Protocol):
    def sharedInstance(self) -> _Coordinator: ...


class _DeviceClass(Protocol):
    def deviceWithAddressString(self, address: str, /) -> _Device | None: ...
    def pairedDevices(self) -> Iterable[_Device] | None: ...


class _ControllerClass(Protocol):
    def defaultController(self) -> _Controller | None: ...


class _ChannelClass(Protocol):
    # Mostly reached by wrapping a pointer the framework filled in rather than
    # by calling a method on it, so what there is to describe is the call.
    def __call__(self, pointer: objc_id, /) -> _Channel: ...
    def registerForChannelOpenNotifications(
        self,
        observer: NSObject,
        /,
        *,
        selector: SEL,
        withChannelID: int,
        direction: int,
    ) -> _Notification: ...


class _LocalServices(Protocol):
    def allValues(self) -> Iterable[_Record]: ...


class _DeviceManager(Protocol):
    def getLocalServices(self) -> _LocalServices | None: ...


class _DeviceManagerClass(Protocol):
    def sharedDeviceManager(self) -> _DeviceManager: ...


class _RecordClass(Protocol):
    def publishedServiceRecordWithDictionary(
        self, record: ObjCInstance, /
    ) -> _PublishedRecord | None: ...


class _UUIDClass(Protocol):
    def uuidWithData(self, data: bytes, /) -> _UUID: ...
    def uuid16(self, value: int, /) -> _UUID: ...


def _class(name: str) -> Any:
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


IOBluetoothDevice = cast(_DeviceClass, _class("IOBluetoothDevice"))
IOBluetoothHostController = cast(_ControllerClass, _class("IOBluetoothHostController"))
IOBluetoothSDPUUID = cast(_UUIDClass, _class("IOBluetoothSDPUUID"))
IOBluetoothRFCOMMChannel = cast(_ChannelClass, _class("IOBluetoothRFCOMMChannel"))
IOBluetoothSDPServiceRecord = cast(_RecordClass, _class("IOBluetoothSDPServiceRecord"))
IOBluetoothRFCOMMChannelDelegate = ObjCProtocol("IOBluetoothRFCOMMChannelDelegate")

# Looked up where they are used rather than beside the others above: they are
# private, and at module scope a class a future macOS lacks would take the
# backend down.
_COORDINATOR = "IOBluetoothCoreBluetoothCoordinator"
_DEVICE_MANAGER = "BluetoothDeviceManager"

# The runtime calls the hook in hear_open_results needs, declared on a handle
# of its own so as not to disturb rubicon's declarations of the same functions.
_objc = ctypes.CDLL("/usr/lib/libobjc.dylib")
_objc.sel_registerName.restype = ctypes.c_void_p
_objc.sel_registerName.argtypes = [ctypes.c_char_p]
_objc.class_getInstanceMethod.restype = ctypes.c_void_p
_objc.class_getInstanceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_objc.method_getImplementation.restype = ctypes.c_void_p
_objc.method_getImplementation.argtypes = [ctypes.c_void_p]
_objc.method_setImplementation.restype = ctypes.c_void_p
_objc.method_setImplementation.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

if TYPE_CHECKING:

    def _mach_error_string(code: int) -> bytes: ...

else:
    _mach_error_string = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_int)(
        ("mach_error_string", libc), ((1, "code"),)
    )

# kBluetoothHCIPowerStateON. The framework's power states are C enum
# constants rather than exported symbols, so there is nothing to read out of
# the dylib at runtime and the value has to be written down.
_POWER_ON = 1

# A bound on waiting for the framework to finish closing a channel. This is
# the one place a limit belongs: it sits on the teardown path, and teardown
# that can block forever leaves a cancelled caller unable to unwind at all.
# Running out is not an error, so it gives up by carrying on.
_CLOSE_TIMEOUT = 5.0

# How many times to ask for a channel whose predecessor is still going away,
# and how long to leave between asks. Measured: a reconnect straight after a
# close needs exactly one wait, so this is twenty times what the framework
# actually takes. It is not a guess at how long an operation should be
# allowed -- that is the caller's to decide -- but the point at which a local
# quirk stops looking like settling and starts looking like a framework that
# is wedged, which is worth saying out loud rather than waiting on for ever.
_OPEN_ATTEMPTS = 20
_SETTLE_SECONDS = 0.1

# How long to let a channel open go unreported before asking for it again, and
# how many times to ask. macOS fails some opens internally -- a nil channel
# with a zero status -- and then tells nobody, so silence is the only symptom
# and no state distinguishes it from an open still in progress. Measured over
# 64 successful opens: 58 to 498 ms, so this is twice the slowest. Asking
# again costs nothing on the air: an open that did land leaves the channel
# where the framework finds it and hands it straight back.
_OPEN_REPORT_SECONDS = 1.0
_OPEN_REPORT_ATTEMPTS = 4

# How often to look again while the manager's state is unknown. See
# report_states.
_UNKNOWN_POLL_SECONDS = 0.05

# The two the documentation calls temporary, promising another value after.
# Everything else is an answer, which is not the same as a usable one.
_UNSETTLED = (CBManagerState.UNKNOWN, CBManagerState.RESETTING)

# NSKeyValueObservingOptionNew | NSKeyValueObservingOptionInitial. The initial
# notification is what lets the state go unchecked before observing, and with
# it the race between checking and registering.
_OBSERVE_INITIAL = 1 | 4

# How long a device is left alone after a channel on it closes. Closing the
# last channel tears down the RFCOMM session, and the L2CAP link underneath it
# goes about thirty milliseconds after the close is reported finished. An open
# issued inside that window is handed the dying link and fails as a nil channel
# with a zero status, reported to nobody. Measured swallowed at 25 ms and never
# at 50, so twice that. A deadline rather than a delay: a caller that did
# anything at all in between waits for none of it.
_RELINK_SECONDS = 0.1

# A write's refcon is the address of the object holding its future, and that
# object has to stay alive until the completion callback reads it back out --
# including when whoever asked for the write was cancelled long ago and the
# frame that held it is gone. Keyed by the address the callback is handed.
_in_flight: dict[int, "ctypes.py_object[asyncio.Future[int]]"] = {}


class _Completion(Protocol):
    """
    A target handed to an asynchronous IOBluetooth call.
    """

    token: int


# IOBluetooth stores the target of an asynchronous call unretained (Apple
# FB13705522), so one has to be held here until its callback fires: a cancelled
# caller's frame goes away while the call is still outstanding, and the
# framework would then message a deallocated object. A callback that never
# arrives leaks one small object, which is the cheaper of the two failures.
_awaiting_completion: dict[int, _Completion] = {}
_completion_tokens = itertools.count()

# bluetoothd's own result for each channel open, before CoreBluetooth reduces
# it to an error code, by device address and channel. Written on a framework
# thread by the hook hear_open_results installs.
_open_results: dict[tuple[str, int], int] = {}

# Devices a caller is using. Claimed before the link is brought up and held
# until after the channel closes, so for part of its life there is no channel
# yet -- see open_channel, which is the only thing that puts anything here.
_in_use: set[str] = set()

# When each device last had a channel closed -- see _RELINK_SECONDS.
_closed_at: dict[str, float] = {}


class _StateObserver(NSObject):
    """
    Reports when a ``CBManager`` reaches a state being waited for.
    """

    reached: asyncio.Event
    loop: asyncio.AbstractEventLoop
    wanted: Callable[[int], bool]

    @objc_method
    def init(self):
        self.reached = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        return self

    # Annotated as what rubicon assumes anyway, so the selector is unchanged.
    @objc_method
    def observeValueForKeyPath_ofObject_change_context_(
        self,
        _path: ObjCInstance,
        watched: ObjCInstance,
        _change: ObjCInstance,
        _context: ObjCInstance,
    ) -> None:
        # Read from the manager, not from the change dictionary, which can
        # carry a stale value on an initial notification. Set through the loop
        # because these arrive on a framework thread.
        if self.wanted(cast(_Manager, watched).state):
            self.loop.call_soon_threadsafe(self.reached.set)


class _StateReporter(NSObject):
    """
    Passes on every state a ``CBManager`` reaches.
    """

    loop: asyncio.AbstractEventLoop
    report: Callable[[int, int], None]

    @objc_method
    def init(self):
        self.loop = asyncio.get_running_loop()
        return self

    # Annotated as what rubicon assumes anyway, so the selector is unchanged.
    @objc_method
    def observeValueForKeyPath_ofObject_change_context_(
        self,
        _path: ObjCInstance,
        watched: ObjCInstance,
        _change: ObjCInstance,
        _context: ObjCInstance,
    ) -> None:
        # Read here, on the framework thread, so each change is reported as
        # it was rather than as it is by the time the loop gets to it.
        self.loop.call_soon_threadsafe(
            self.report, cast(_Manager, watched).state, read_authorization()
        )


async def _wait_until_ready() -> None:
    """
    Wait for CoreBluetooth to settle, and object if it settled unusably.

    CoreBluetooth discards every command until its manager reaches the
    powered-on state, and IOBluetooth issues ours without waiting: for the
    first tens of milliseconds of a process an open is dropped, logged only as
    "API MISUSE", and never reported. Nothing bounds the wait, because a
    manager that never settles is one no open would have worked on either.

    Returns without waiting if the manager cannot be reached at all, since a
    macOS that does not have it is one this can say nothing about.

    Raises:
        BluetoothUnavailableError: The radio is switched off, this program
            may not use Bluetooth, or the machine has none that works.
        ConnectionFailedError: The manager settled on a state with no meaning
            here, which is a macOS this code has not been taught about.
    """
    manager = _classic_manager()
    if manager is None:
        return

    await _wait_until_settled(manager)

    # Settling is not the same as being usable: a refused or switched-off radio
    # settles just as firmly as a working one, and waiting for powered-on alone
    # would wait for ever.
    state = manager.state
    if state != CBManagerState.POWERED_ON:
        raise _unusable(state)


def _classic_manager() -> _Manager | None:
    """
    Reach CoreBluetooth's classic manager, if this macOS has one.

    Returns:
        The manager, or ``None`` if the private path to it has gone.
    """
    try:
        coordinator = cast(_CoordinatorClass, ObjCClass(_COORDINATOR))
        return coordinator.sharedInstance().classicManager
    except Exception:  # noqa: BLE001
        logger.debug("CoreBluetooth's classic manager could not be reached")
        return None


async def _wait_until_settled(manager: _Manager) -> None:
    """
    Wait for the manager's state to stop being provisional.

    Done on every call rather than once per process, because the state can go
    back to being provisional -- the radio being switched off and on again
    passes through resetting -- and because it costs nothing when it has not:
    an observer registered with the initial option is told the current value
    before ``addObserver`` returns, measured at 50 to 800 microseconds.

    Args:
        manager: The classic manager.
    """
    await _wait_for_state(manager, lambda state: state not in _UNSETTLED)


async def report_states(report: Callable[[int, int], None]) -> NoReturn:
    """
    Report the manager's state now, and again on every change.

    IOBluetooth was not seen to report the radio any other way: a channel
    open at the time hears its link drop, and that was all. Measured across a
    real toggle: the manager goes from powered on to powered off, on a
    framework thread, about a millisecond before the channel is told.

    Every value is reported, the temporary ones included. What they mean is
    for the caller to decide.

    If the manager cannot be reached, the controller's power state is
    reported once instead, since a macOS without it is one this can say
    nothing more about.

    Args:
        report: Called with each ``CBManagerState`` and the
            ``CBManagerAuthorization`` at the time, on this loop's thread.
    """
    manager = _classic_manager()
    if manager is None:
        powered = read_power_state()
        report(
            CBManagerState.POWERED_ON if powered else CBManagerState.POWERED_OFF,
            read_authorization(),
        )
        await asyncio.Event().wait()
        raise AssertionError("an event nobody sets was set")

    last: tuple[int, int] | None = None

    def once(state: int, authorization: int) -> None:
        nonlocal last
        if (state, authorization) != last:
            last = (state, authorization)
            report(state, authorization)

    # KVO offers no way to say which thread to answer on -- see the reporter.
    reporter = _StateReporter.alloc().init()
    reporter.report = once
    manager.addObserver(
        reporter, forKeyPath="state", options=_OBSERVE_INITIAL, context=None
    )
    try:
        # Measured in a fresh process: the state settles while the observer is
        # being added, after the initial notification has read it and before
        # the observer hears of changes, so nothing reports it. Read again now
        # that the observer is in place, queued behind that notification.
        asyncio.get_running_loop().call_soon(once, manager.state, read_authorization())

        # Unknown is documented as temporary with an update to follow, but in
        # a fresh process the update was measured going unreported. So while
        # it is unknown, look again.
        await asyncio.sleep(0)
        while last is not None and last[0] == CBManagerState.UNKNOWN:
            await asyncio.sleep(_UNKNOWN_POLL_SECONDS)
            once(manager.state, read_authorization())

        await asyncio.Event().wait()
        raise AssertionError("an event nobody sets was set")
    finally:
        manager.removeObserver(reporter, forKeyPath="state", context=None)


async def _wait_for_state(manager: _Manager, wanted: Callable[[int], bool]) -> None:
    """
    Wait for the manager's state to be one that ``wanted`` accepts.

    Args:
        manager: The classic manager.
        wanted: Says whether a state is the one being waited for.
    """
    # KVO offers no way to say which thread to answer on -- see the observer.
    observer = _StateObserver.alloc().init()
    observer.wanted = wanted
    manager.addObserver(
        observer, forKeyPath="state", options=_OBSERVE_INITIAL, context=None
    )
    try:
        # The state can settle while the observer is being added, unheard.
        # See report_states.
        if wanted(manager.state):
            observer.reached.set()
        await observer.reached.wait()
    finally:
        manager.removeObserver(observer, forKeyPath="state", context=None)


def _unusable(state: int) -> RfcommError:
    """
    Say why a settled manager is no use.

    Args:
        state: The ``CBManagerState`` it settled on.

    Returns:
        The error to raise.
    """
    match state:
        case CBManagerState.POWERED_OFF | CBManagerState.UNAUTHORIZED:
            # Both, because macOS reports a radio this program may not use as
            # switched off -- so telling someone to turn Bluetooth on would
            # send them to a switch that is already on. The authorization is
            # what separates the two, as it does in the reported availability.
            if read_authorization() == CBManagerAuthorization.ALLOWED_ALWAYS:
                return BluetoothUnavailableError(
                    Availability.SWITCHED_OFF,
                    "this Mac's Bluetooth is switched off. Turn it on",
                )
            return BluetoothUnavailableError(
                Availability.NOT_ALLOWED,
                "Bluetooth is not allowed for this program. Check Privacy & "
                "Security > Bluetooth in System Settings",
            )
        case CBManagerState.UNSUPPORTED:
            return BluetoothUnavailableError(
                Availability.UNSUPPORTED, "this Mac has no usable Bluetooth"
            )
        case _:
            return ConnectionFailedError(
                f"CoreBluetooth settled in state {state}, which this does not know"
            )


def _hold(complete: _Completion) -> None:
    """
    Keep a completion object alive until its callback takes it back out.

    Args:
        complete: The target handed to an asynchronous IOBluetooth call.
    """
    _awaiting_completion[complete.token] = complete


def _release(complete: _Completion) -> None:
    """
    Let go of one whose call never started.

    Does nothing once the callback has already taken it out, so it is safe on
    any path that is not cancellation.

    Args:
        complete: The target that was held.
    """
    _awaiting_completion.pop(complete.token, None)


logger = logging.getLogger(__name__)


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


def _check(code: int) -> None:
    """
    Turn a non-zero status into an exception.

    Args:
        code: The status to check.

    Raises:
        MachError: The status was non-zero.
    """
    if code:
        raise MachError(code)


# --------------------------------------------------------------------------
# Delegates
#
# IOBluetooth reports everything through delegate callbacks, and a delegate is
# held weakly, so each of these has to be kept alive by whoever installed it.
# --------------------------------------------------------------------------


class _ConnectionComplete(NSObject):
    """
    Completion for the ACL connection a channel is carried over.
    """

    future: asyncio.Future[None]
    token: int

    # No return annotation: rubicon reads annotations to build the
    # Objective-C signature, and an initialiser's is already known.
    @objc_method
    def init(self):
        self.future = asyncio.get_running_loop().create_future()
        self.token = next(_completion_tokens)
        return self

    @objc_method
    def connectionComplete_status_(self, _device: ObjCInstance, status: int) -> None:
        _awaiting_completion.pop(self.token, None)
        if self.future.done():
            return
        if status:
            # Not the IOKit status the header promises but CoreBluetooth's
            # error code: the daemon's result made into an NSError, of which
            # only the code is passed on. An unanswered page arrives as
            # CONNECTION_FAILED, which mach_error_string renders as nonsense,
            # so it is reported as the timeout it really is.
            raise_as = (
                IOReturn.TIMEOUT if status == CBError.CONNECTION_FAILED else status
            )
            self.future.set_exception(MachError(raise_as))
        else:
            self.future.set_result(None)


class _QueryComplete(NSObject):
    """
    Completion for an SDP query.
    """

    future: asyncio.Future[None]
    token: int

    @objc_method
    def init(self):
        self.future = asyncio.get_running_loop().create_future()
        self.token = next(_completion_tokens)
        return self

    @objc_method
    def sdpQueryComplete_status_(self, _device: ObjCInstance, status: int) -> None:
        _awaiting_completion.pop(self.token, None)
        if self.future.done():
            return
        if status:
            self.future.set_exception(MachError(status))
        else:
            self.future.set_result(None)


# Bytes read out of a closing channel's stream, by the address of its
# IOBluetoothRFCOMMChannel. Written on a framework thread and taken on the main
# thread, in the delegate's rfcommChannelClosed:.
_unread: dict[int, bytes] = {}


def _address_of(channel: object) -> int:
    """
    Get the address an IOBluetoothRFCOMMChannel is known by in ``_unread``.

    Args:
        channel: The channel, or ``None`` where the framework gave none.

    Returns:
        Its address, or 0 for anything that has none.
    """
    pointer = getattr(channel, "ptr", None)
    return cast(int, getattr(pointer, "value", None) or 0)


class _ChannelDelegate(NSObject, protocols=[IOBluetoothRFCOMMChannelDelegate]):
    """
    Everything one open RFCOMM channel reports back.
    """

    opened: asyncio.Future[None]
    arrived: asyncio.Queue[bytes]
    ended: asyncio.Event

    @objc_method
    def init(self):
        self.opened = asyncio.get_running_loop().create_future()
        self.arrived = asyncio.Queue()
        self.ended = asyncio.Event()
        return self

    @objc_method
    def rfcommChannelOpenComplete_status_(
        self, _channel: ObjCInstance, status: int
    ) -> None:
        if self.opened.done():
            return
        if status:
            self.opened.set_exception(MachError(status))
        else:
            self.opened.set_result(None)

    @objc_method
    def rfcommChannelData_data_length_(
        self,
        _channel: ObjCInstance,
        _data: ctypes.c_void_p,
        _length: ctypes.c_size_t,
    ) -> None:
        # The bytes belong to IOBluetooth and are only valid for the duration
        # of this callback, so they are copied out rather than referenced.
        #
        # The casts below are for the type checker alone. These parameters are
        # annotated with ctypes types because rubicon reads the annotations to
        # build the selector's signature, but what it hands the callback are
        # plain integers.
        self.arrived.put_nowait(ctypes.string_at(_data, cast(int, _length)))

    @objc_method
    def rfcommChannelClosed_(self, channel: ObjCInstance) -> None:
        # What IOBluetooth was about to throw away. See keep_unread_bytes.
        unread = _unread.pop(_address_of(channel), None)

        if unread:
            self.arrived.put_nowait(unread)

        self.ended.set()

    @objc_method
    def rfcommChannelWriteComplete_refcon_status_bytesWritten_(
        self,
        _channel: ObjCInstance,
        _refcon: ctypes.c_void_p,
        status: int,
        _length: ctypes.c_size_t,
    ) -> None:
        refcon = cast(int, _refcon)
        holder = _in_flight.pop(refcon, None)
        if holder is None:
            # A write from a channel that has already been let go of. There
            # is nothing left to tell.
            return
        future = holder.value
        if future.done():
            return
        if status:
            future.set_exception(MachError(status))
        else:
            future.set_result(cast(int, _length))


# --------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------


class _ManagerClass(Protocol):
    """
    The one class property we want from ``CBManager``.
    """

    @property
    def authorization(self) -> int: ...


CBManager = cast(_ManagerClass, _class("CBManager"))


def read_authorization() -> int:
    """
    Read whether this process may use Bluetooth at all.

    macOS reports an unauthorised radio as switched off, so without this a
    program whose permission was never granted -- or was refused -- is told
    to turn Bluetooth on, which it already is. Reading this neither prompts
    nor touches the radio.

    Returns:
        A ``CBManagerAuthorization``. rubicon hands ``NSInteger`` back as a
        Python ``int`` already, so there is nothing to convert.
    """
    return CBManager.authorization


def read_power_state() -> bool:
    """
    Read whether the radio is switched on.

    Returns:
        True if the adapter is powered.
    """
    controller = IOBluetoothHostController.defaultController()
    return controller is not None and controller.powerState == _POWER_ON


def list_known_devices(
    service: UUID | Collection[UUID] | None = None,
) -> list[RfcommDeviceInfo]:
    """
    List the devices macOS has paired with.

    Filtering happens against the cached SDP records, which is all that is
    available without connecting to each device in turn.

    Args:
        service: Return only devices offering this service, or any of these
            services. ``None`` returns every paired device.

    Returns:
        The matching devices.
    """
    wanted = [_sdp_uuid(u) for u in _each(service)]

    found: list[RfcommDeviceInfo] = []
    seen: set[str] = set()
    for device in IOBluetoothDevice.pairedDevices() or []:
        if wanted and not any(device.getServiceRecordForUUID(u) for u in wanted):
            continue
        address = _normalise(str(device.addressString))
        # macOS lists some devices twice. Measured rather than guessed at:
        # pairedDevices() returned thirteen entries holding twelve distinct
        # objects, with one device's object appearing at two non-adjacent
        # positions. The two entries are the same object, so there is nothing
        # to tell them apart and nothing to choose between them -- only a
        # caller left wondering which of two identical devices to use.
        if address in seen:
            continue
        seen.add(address)
        name = device.name
        found.append(RfcommDeviceInfo(address, str(name) if name else None))
    return found


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------


class Channel:
    """
    An RFCOMM channel, from the moment the framework hands one over.

    Reading is a queue rather than a method returning bytes, because
    IOBluetooth pushes: data arrives in a delegate callback whether or not
    anybody has asked for it, and there is no synchronous receive to call.

    This object is the only thing that holds the framework's channel. That is
    a requirement rather than a convention -- see :meth:`close`.
    """

    def __init__(
        self, channel: _Channel, delegate: _ChannelDelegate, device: _Device
    ) -> None:
        """
        Args:
            channel: The ``IOBluetoothRFCOMMChannel``, which may not be open
                yet.
            delegate: Its delegate, held here because the channel does not.
            device: The device it runs over, for telling a hang-up apart from
                a lost link.
        """
        self._channel = channel
        self._delegate = delegate
        self._device = device

    @property
    def arrived(self) -> asyncio.Queue[bytes]:
        """
        Bytes that have come in and not yet been taken.
        """
        return self._delegate.arrived

    def peer(self) -> RfcommDeviceInfo:
        """
        Returns:
            The device at the other end.
        """
        name = self._device.name
        return RfcommDeviceInfo(
            _normalise(str(self._device.addressString)), str(name) if name else None
        )

    async def wait_until_open(self) -> None:
        """
        Wait for the framework to finish opening the channel.

        Raises:
            MachError: The channel could not be opened.
        """
        await self._delegate.opened

    async def send(self, data: bytes) -> None:
        """
        Write all of ``data`` to the channel.

        The framework rejects a write longer than the channel's maximum
        transmission unit, so this splits one. The size is read each time
        rather than kept, since it belongs to the channel and not to us.

        Args:
            data: The bytes to write.

        Raises:
            MachError: The write failed.
        """
        limit = int(self._channel.getMTU())
        for start in range(0, len(data), limit):
            await self._write(data[start : start + limit])

    async def _write(self, piece: bytes) -> None:
        """
        Write one piece, no longer than the channel's maximum unit.

        Args:
            piece: The bytes to write.

        Raises:
            MachError: The write failed.
        """
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        holder = ctypes.py_object(future)
        refcon = ctypes.addressof(holder)
        # Held until the completion callback takes it back out. Letting it go
        # when this task is cancelled would leave the callback reading an
        # address whose memory has been freed, which crashes the process some
        # unrelated moment later.
        _in_flight[refcon] = holder
        try:
            _check(self._channel.writeAsync(piece, length=len(piece), refcon=refcon))
        except BaseException:
            del _in_flight[refcon]
            raise
        await future

    async def wait_until_ended(self) -> CloseReason:
        """
        Wait for the far end to close the channel.

        Returns:
            Why it closed. The baseband link tells the two cases apart: if it
            is still up, the peer closed just this channel. If it is gone, the
            whole link went away.
        """
        await self._delegate.ended.wait()
        if self._device.isConnected():
            return CloseReason.PEER_CLOSED
        return CloseReason.LINK_LOST

    def release(self) -> None:
        """
        Let go of a channel without closing it.

        For one whose open was never reported: it may in fact be open, in
        which case ``closeChannel`` would close the channel the next attempt
        means to pick up, and the caller would be left writing into a channel
        the peer has already seen go away.

        Calling this twice is harmless. The channel is unusable afterwards.
        """
        if not hasattr(self, "_channel"):
            return

        _unread.pop(_address_of(self._channel), None)
        # Dropped inside a pool for the reason close() explains at length.
        with autoreleasepool():
            del self._channel
            del self._delegate

    async def close(self) -> None:
        """
        Close the channel and let the framework have it back.

        Two things have to happen and the order matters. ``closeChannel``
        only starts the teardown, so this waits for the framework to say it
        has finished -- with a bound, since teardown that can block forever
        leaves a cancelled caller unable to unwind, and giving up here means
        carrying on rather than raising.

        Then the channel is let go of, which is not tidiness. rubicon keeps a
        reference for as long as its Python wrapper lives and gives that
        reference back by autoreleasing it, and an autorelease with no pool
        in place is never actually carried out. The channel would stay alive,
        stay open, and be handed back the next time one is asked for on this
        number, complete with the previous caller's stream. Dropping the
        wrapper inside a pool is what makes the release happen, and this
        object being the only thing holding the channel is what makes
        dropping it enough.

        Calling this twice is harmless. The channel is unusable afterwards.
        """
        if not hasattr(self, "_channel"):
            return

        self._channel.closeChannel()
        try:
            if not self._delegate.ended.is_set():
                async with asyncio.timeout(_CLOSE_TIMEOUT):
                    await self._delegate.ended.wait()
        except TimeoutError:
            logger.warning("RFCOMM channel did not finish closing")
        finally:
            _unread.pop(_address_of(self._channel), None)
            with autoreleasepool():
                # Deleted rather than set aside, because this object is
                # finished: anything that reaches for the channel afterwards
                # is a mistake and should say so.
                del self._channel
                del self._delegate


@asynccontextmanager
async def open_channel(
    address: str, *, service: UUID | None = None, channel: int | None = None
) -> AsyncGenerator[Channel, None]:
    """
    Open an RFCOMM channel to a device.

    Exactly one of ``service`` and ``channel`` says which channel to open. A
    service is looked up in the device's SDP records, querying the device if
    they are not already cached.

    Args:
        address: The device's Bluetooth address.
        service: The service to look up.
        channel: The channel number to open directly.

    Yields:
        The open channel, closed on exit.

    Raises:
        BluetoothUnavailableError: The radio is switched off, this program
            may not use Bluetooth, or the machine has none that works.
        DeviceNotFoundError: The device did not answer.
        ServiceNotFoundError: The device does not offer that service.
        ConnectionFailedError: The channel could not be opened.
        UnsupportedOperationError: The device already has a channel open.
    """
    if (service is None) == (channel is None):
        raise TypeError("open_channel takes exactly one of service and channel")

    # Before anything else touches the framework. That is the discipline
    # CoreBluetooth asks of its own callers, and reaching for a device first
    # measurably delays the manager coming up.
    await _wait_until_ready()

    device = IOBluetoothDevice.deviceWithAddressString(address)
    if not device:
        raise DeviceNotFoundError(f"{address} is not a Bluetooth address")
    logger.debug("%s: connected=%s", address, device.isConnected())

    # One caller at a time per device, refused rather than queued, because
    # macOS gets a second one wrong three separate ways. Connecting keeps one
    # completion target per device, so a second ``openConnection:`` while one
    # is outstanding overwrites the first's and that caller waits for ever.
    # Opening a channel likewise installs its completion in a slot the device
    # has only one of. And closing is serviced in the order channels were
    # opened rather than the one asked for, so with two open, closing either
    # tears down the wrong one and writes to the other go nowhere -- measured
    # against a peer listening on three channels: asked to close 6, then 5,
    # then 4, and 4, 5, 6 closed in that order, with the right number on the
    # wire each time.
    #
    # All three are per device. Two devices at once share nothing: measured
    # with channels open to two at the same time, neither the channels, the
    # delegates nor the data crossed, and both closes were reported. So this
    # is claimed per address, and before the first await, so that two callers
    # racing here cannot both pass.
    key = address.upper()
    if key in _in_use:
        raise UnsupportedOperationError(
            f"{address} already has an RFCOMM channel open, and macOS cannot "
            "hold two to one device at once -- it closes them in the order "
            "they were opened rather than the one asked for, which loses both. "
            "Close the first channel before opening another."
        )
    _in_use.add(key)
    try:
        await _wait_for_relink(key)

        # Both paths need the baseband link: an SDP query runs over it, and so
        # does the channel itself.
        if not device.isConnected():
            await _connect(device, address)

        if channel is not None:
            number = channel
        else:
            # Guaranteed by the check above, which the type checker cannot see.
            number = await _look_up(device, address, cast(UUID, service))

        opening = await _open_reported(device, number, address)

        logger.debug("channel %d to %s is open", number, address)
        try:
            yield opening
        finally:
            try:
                await opening.close()
            finally:
                _closed_at[key] = time.monotonic()
    finally:
        _in_use.discard(key)


async def _wait_for_relink(key: str) -> None:
    """
    Wait out the previous channel's teardown, if it is still going on.

    Args:
        key: The device's address, upper case.
    """
    closed = _closed_at.get(key)
    if closed is None:
        return
    left = _RELINK_SECONDS - (time.monotonic() - closed)
    if left > 0:
        logger.debug("letting %s's last channel finish going away", key)
        await asyncio.sleep(left)


_HandleOpened = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)

# The hook runs on a framework thread, where rubicon is not safe to use: it
# loads a class's methods on first use, and two threads doing so at once raise
# -- measured, as a helper that failed to make a channel's delegate while the
# hook read an incoming channel's result. So the hook reads with plain message
# sends, and its keys are made here, on the main thread.
_send_with_object = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)(("objc_msgSend", _objc))
_send_for_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc)
)
_send_for_int = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc)
)
_send_for_text = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc)
)
_send_is_kind = ctypes.CFUNCTYPE(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)(("objc_msgSend", _objc))
_objc.objc_getClass.restype = ctypes.c_void_p
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_IS_KIND_OF_CLASS = _objc.sel_registerName(b"isKindOfClass:")
_NSNUMBER = _objc.objc_getClass(b"NSNumber")
_NSSTRING = _objc.objc_getClass(b"NSString")
_OBJECT_FOR_KEY = _objc.sel_registerName(b"objectForKey:")
_INT_VALUE = _objc.sel_registerName(b"intValue")
_ADDRESS_STRING = _objc.sel_registerName(b"addressString")
_UTF8_STRING = _objc.sel_registerName(b"UTF8String")
_RESULT_KEY = cast(ObjCInstance, ns_from_py("kCBMsgArgResult"))
_CHANNEL_KEY = cast(ObjCInstance, ns_from_py("kCBMsgArgRFCOMMChannelID"))
_SOCKET_KEY = cast(ObjCInstance, ns_from_py("kCBMsgArgSocket"))


class _OpenResult(NamedTuple):
    """
    What bluetoothd said about one channel open.
    """

    address: str | None
    channel: int | None
    result: int | None
    socket: int | None


def _read_open_result(peer: int, info: int) -> _OpenResult:
    """
    Read bluetoothd's report of a channel open, on whatever thread.

    Args:
        peer: The ``CBClassicPeer`` the channel was opened to.
        info: What bluetoothd sent.

    Returns:
        What it said, with ``None`` for anything it left out.
    """

    # Checked before anything is sent to them: an Objective-C exception on
    # this thread is beyond any Python handler and would end the process.
    def number(key: ObjCInstance) -> int | None:
        found = _send_with_object(info, _OBJECT_FOR_KEY, key.ptr)
        if found and _send_is_kind(found, _IS_KIND_OF_CLASS, _NSNUMBER):
            return int(_send_for_int(found, _INT_VALUE))

        return None

    address = None
    text = _send_for_object(peer, _ADDRESS_STRING)

    if text and _send_is_kind(text, _IS_KIND_OF_CLASS, _NSSTRING):
        utf8 = _send_for_text(text, _UTF8_STRING)
        address = utf8.decode() if utf8 else None

    return _OpenResult(
        address, number(_CHANNEL_KEY), number(_RESULT_KEY), number(_SOCKET_KEY)
    )


def record_open_result(address: str, channel: int, result: int) -> None:
    """
    Record bluetoothd's result for one channel open.

    Args:
        address: The device's address, in any of the forms IOBluetooth writes.
        channel: The RFCOMM channel number.
        result: The daemon's ``kCBMsgArgResult``, 0 for success.
    """
    _open_results[(_normalise(address), channel)] = result


def _note_open_result(opened: _OpenResult) -> None:
    """
    Note one channel open's result as bluetoothd reported it.

    Args:
        opened: What bluetoothd said.
    """
    if opened.address is None or opened.channel is None or opened.result is None:
        return
    record_open_result(opened.address, opened.channel, opened.result)


def _describe_result(result: int) -> str:
    """
    Say what CoreBluetooth makes of one of bluetoothd's results.

    Args:
        result: A ``kCBMsgArgResult`` other than 0.

    Returns:
        CoreBluetooth's own description, which for most results is only
        "Unknown error."
    """
    # A private category, so a macOS without it costs only the description.
    try:
        error = _class("NSError").errorWithInfo_(
            ns_from_py({"kCBMsgArgResult": result})
        )
    except Exception:  # noqa: BLE001
        return "no description"

    if error is None:
        return "no error"

    return str(error.localizedDescription)


def _is_null_device(descriptor: int) -> bool:
    try:
        opened = os.fstat(descriptor)
    except OSError:
        return False

    return (
        stat.S_ISCHR(opened.st_mode) and opened.st_rdev == os.stat(os.devnull).st_rdev
    )


def _close_unclaimed_socket(opened: _OpenResult) -> None:
    """
    Close the descriptor a failed channel open leaves behind.

    Args:
        opened: What bluetoothd said, once CoreBluetooth has handled it.
    """
    if not opened.result or opened.socket is None:
        return

    # CoreBluetooth duplicates each descriptor bluetoothd sends, and only the
    # channel object made for a success closes it. A failure carries
    # bluetoothd's descriptor 0, /dev/null: measured, one leaked per refused
    # open. Only /dev/null is closed, so a real socket never is.
    if _is_null_device(opened.socket):
        os.close(opened.socket)


# The hook and what it replaced, kept for the life of the process: the
# framework calls through them on threads of its own.
_hooked: list[object] = []

# The same for the hook keep_unread_bytes installs.
_keeping: list[object] = []


def hear_open_results() -> None:
    """
    Keep bluetoothd's result for each channel open, which nothing else sees.

    ``-[CBClassicPeer handleRFCOMMChannelOpened:]`` turns the daemon's result
    into an ``NSError`` and passes IOBluetooth only its code, which is 0 for
    any result CoreBluetooth has no name for -- a refused channel among them
    (follow-ups §13.6). So the method is wrapped, in this process only, to
    note the result first and then do exactly what it did.

    Does nothing if this macOS has no such method, and nothing the second time.
    """
    if _hooked:
        return

    try:
        selector = _objc.sel_registerName(b"handleRFCOMMChannelOpened:")
        method = _objc.class_getInstanceMethod(_class("CBClassicPeer").ptr, selector)
    except Exception:  # noqa: BLE001
        method = None
    if not method:
        logger.debug("CoreBluetooth has no handleRFCOMMChannelOpened: to hear")
        return

    original = _HandleOpened(_objc.method_getImplementation(method))

    def opened(peer: int, selector: int, info: int) -> None:
        said = None

        try:
            said = _read_open_result(peer, info)
            _note_open_result(said)
        except Exception:
            # Losing the note costs a clearer error, never the open itself.
            logger.debug("could not read a channel open's result", exc_info=True)

        original(peer, selector, info)

        if said is not None:
            try:
                _close_unclaimed_socket(said)
            except Exception:
                logger.debug("could not check a failed open's socket", exc_info=True)

    replacement = _HandleOpened(opened)
    _hooked.extend([original, replacement])
    _objc.method_setImplementation(method, ctypes.cast(replacement, ctypes.c_void_p))


_Removal = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
# How much of a closing channel's stream is read at a time. Only a buffer size.
_UNREAD_PIECE_BYTES = 4096
_RFCOMMCHANNEL = _objc.sel_registerName(b"rfcommchannel")
_INPUT_STREAM = _objc.sel_registerName(b"inputStream")
_PROPERTY_FOR_KEY = _objc.sel_registerName(b"propertyForKey:")
_DELEGATE = _objc.sel_registerName(b"delegate")
_NSDATA = _objc.objc_getClass(b"NSData")
_core_foundation = ctypes.CDLL(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_core_foundation.CFDataGetLength.restype = ctypes.c_long
_core_foundation.CFDataGetLength.argtypes = [ctypes.c_void_p]
_core_foundation.CFDataGetBytePtr.restype = ctypes.c_void_p
_core_foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
_SOCKET_HANDLE = ctypes.c_void_p.in_dll(
    _core_foundation, "kCFStreamPropertySocketNativeHandle"
).value


def _socket_of(stream: int) -> int | None:
    """
    Find the socket under a channel's input stream.

    Runs on a framework thread, so it uses plain message sends only.

    Args:
        stream: The address of the stream.

    Returns:
        The socket's descriptor, or ``None`` if the stream does not say.
    """
    handle = _send_with_object(stream, _PROPERTY_FOR_KEY, _SOCKET_HANDLE)

    if not handle or not _send_is_kind(handle, _IS_KIND_OF_CLASS, _NSDATA):
        return None

    if _core_foundation.CFDataGetLength(handle) != ctypes.sizeof(ctypes.c_int):
        return None

    return ctypes.c_int.from_address(_core_foundation.CFDataGetBytePtr(handle)).value


def _read_unread(channel: int) -> bytes:
    """
    Read what is left in a channel's input stream.

    Runs on a framework thread, so it uses plain message sends only.

    Args:
        channel: The address of the IOBluetoothRFCOMMChannel.

    Returns:
        The bytes, if any.
    """
    rfcomm = _send_for_object(channel, _RFCOMMCHANNEL)
    stream = _send_for_object(rfcomm, _INPUT_STREAM) if rfcomm else None

    if not stream:
        return b""

    descriptor = _socket_of(stream)

    if descriptor is None:
        return b""

    # Straight from the socket, without waiting, and never through the stream.
    # The stream's read can wait on a socket that has already ended: measured
    # on macOS 26.6.2 against a Pybricks EV3 on firmware 4.1.0b6, it waited on
    # one that poll() called hung up and empty, holding up the framework queue
    # this runs on, and every open after it failed until the helper exited.
    #
    # The main run loop may be reading the same socket through the stream. What
    # it has read it has already handed to the delegate, and the close reaches
    # the main thread only after this runs.
    pieces: list[bytes] = []

    with socket.socket(fileno=os.dup(descriptor)) as reader:
        while True:
            try:
                piece = reader.recv(_UNREAD_PIECE_BYTES, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break

            if not piece:
                break

            pieces.append(piece)

    return b"".join(pieces)


def keep_unread_bytes() -> None:
    """
    Keep the bytes a channel received just before the device hung up.

    Wraps ``-[IOBluetoothRFCOMMChannel removeRFCOMMChannelforDevice]``, in
    this process only, to read the channel's socket dry before its input
    stream is closed. The delegate's ``rfcommChannelClosed:`` hands the bytes
    over.

    Does nothing if this macOS has no such method, and nothing the second time.
    """
    # When the device hangs up, the removal closes the input stream on a
    # framework thread without reading what is left, while the stream's own
    # reads happen on the main run loop. So bytes that arrive just before the
    # hang-up are thrown away. Read in the disassembly of IOBluetooth on macOS
    # 26.6.2 (25G83). Measured on the same macOS against a BlueZ 5.85 server
    # that sends and hangs up at once, with the helper's main thread held up
    # for 0.3 s as the channel opened: 12 of 12 sends lost without this hook,
    # none with it. The removal sends rfcommChannelClosed: to the main thread
    # after it has run, so the bytes are in place by then.
    if _keeping:
        return

    try:
        selector = _objc.sel_registerName(b"removeRFCOMMChannelforDevice")
        method = _objc.class_getInstanceMethod(
            _class("IOBluetoothRFCOMMChannel").ptr, selector
        )
    except Exception:  # noqa: BLE001
        method = None
    if not method:
        logger.debug("IOBluetooth has no removeRFCOMMChannelforDevice to wrap")
        return

    original = _Removal(_objc.method_getImplementation(method))

    def removing(channel: int, selector: int) -> None:
        try:
            # A channel nobody has taken yet has nobody to hand bytes to.
            if _send_for_object(channel, _DELEGATE):
                unread = _read_unread(channel)

                if unread:
                    _unread[channel] = unread
        except Exception:
            # Losing these bytes is what happened without the hook.
            logger.debug("could not read a closing channel's stream", exc_info=True)

        original(channel, selector)

    replacement = _Removal(removing)
    _keeping.extend([original, replacement])
    _objc.method_setImplementation(method, ctypes.cast(replacement, ctypes.c_void_p))


async def _open_reported(device: _Device, number: int, address: str) -> Channel:
    """
    Open a channel, asking again if the framework never says whether it did.

    A failed open that reports no error reaches neither delegate method, so the
    caller would otherwise wait for ever -- see :data:`_OPEN_REPORT_SECONDS`.
    Asking again costs nothing on the air: an open that did land leaves the
    channel where the framework finds it and hands it straight back, and one
    that did not never reached the peer.

    Args:
        device: The ``IOBluetoothDevice``.
        number: The RFCOMM channel number.
        address: Its address, for the error messages.

    Returns:
        The channel, open.

    Raises:
        ServiceNotFoundError: The device refused the channel, usually
            because nothing there is listening on it.
        ConnectionFailedError: The channel could not be opened, or the
            framework never reported one open.
    """
    key = (_normalise(address), number)
    for _attempt in range(_OPEN_REPORT_ATTEMPTS):
        _open_results.pop(key, None)
        opening = await _request(device, number, address)
        try:
            async with asyncio.timeout(_OPEN_REPORT_SECONDS):
                await opening.wait_until_open()
        except MachError as error:
            await opening.close()
            raise ConnectionFailedError(
                f"could not open channel {number} on {address}: {error}"
            ) from error
        except TimeoutError:
            opening.release()
            result = _open_results.get(key)
            # Only silence is worth asking again about. Every other result
            # bluetoothd gave over four days of opens on macOS 26.6.2 (911,
            # 913, 927 and 1902) was a definite failure.
            match result:
                case None | 0:
                    logger.debug(
                        "channel %d to %s was never reported open "
                        "(bluetoothd: %s). asking again",
                        number,
                        address,
                        result,
                    )
                    continue
                case OIStatus.RFCOMM_REMOTE_REJECT:
                    raise ServiceNotFoundError(
                        f"{address} refused channel {number}. Usually nothing "
                        "there is listening on it"
                    ) from None
                case OIStatus.RFCOMM_REMOTE_DEVICE_DISCONNECTED:
                    raise ConnectionFailedError(
                        f"{address} hung up as channel {number} opened"
                    ) from None
                case _:
                    raise ConnectionFailedError(
                        f"could not open channel {number} on {address} "
                        f"(bluetoothd said {result}: {_describe_result(result)})"
                    ) from None
        except BaseException:
            await opening.close()
            raise
        return opening

    raise ConnectionFailedError(
        f"the framework never reported channel {number} on {address} open, "
        f"after {_OPEN_REPORT_ATTEMPTS} attempts"
    )


async def _request(device: _Device, number: int, address: str) -> Channel:
    """
    Ask the framework for a channel, waiting out the previous one if need be.

    A channel that comes back already open was not opened by this call: the
    framework found one on that number and bound this wrapper to it instead.
    Nothing is outstanding, so no open-complete callback is coming and
    waiting on one would wait for ever. Such a wrapper is no use either --
    the framework binds it and sets its delegate but never wires its
    streams, so it can be written to and never delivers a byte, and what is
    written goes to whoever still holds the channel. It is the previous
    channel to this device on this number, which the framework keeps handing
    over until it has finished tearing that one down, so the thing to do is
    wait and ask again.

    Waiting and asking again is cruder than waiting for the framework to say
    the old channel is gone, but nothing says so. ``rfcommChannelClosed:``
    answers a different question -- it says the channel closed, which it
    reports around thirteen milliseconds after ``closeChannel`` and well
    before the framework stops handing that channel back.

    ``registerForChannelCloseNotification`` never fires, for a close this
    program asks for or one the peer causes, where the delegate callback
    does: measured with a real ``SEL``. The disassembly says why. A
    ``CBClassicPeer`` has one disconnect callback. The coordinator that would
    pass a close on to notifications sets it, and every
    ``IOBluetoothRFCOMMChannel`` set up on that peer replaces it with its own.

    Args:
        device: The connected ``IOBluetoothDevice``.
        number: The RFCOMM channel number.
        address: The device's address, for the error message.

    Returns:
        The channel, which is not open yet.

    Raises:
        ConnectionFailedError: The channel could not be requested, or the
            previous one never finished closing.
    """
    for _attempt in range(_OPEN_ATTEMPTS):
        delegate = _ChannelDelegate.alloc().init()
        handle = objc_id()
        try:
            _check(
                device.openRFCOMMChannelAsync(
                    ctypes.byref(handle), withChannelID=number, delegate=delegate
                )
            )
        except MachError as error:
            raise ConnectionFailedError(
                f"could not open channel {number} on {address}: {error}"
            ) from error

        opened = IOBluetoothRFCOMMChannel(handle)
        if not opened.isOpen():
            return Channel(opened, delegate, device)

        logger.debug(
            "channel %d on %s is still open from an earlier use. Waiting",
            number,
            address,
        )
        # Let go of inside a pool for the same reason Channel.close does it,
        # and without closing it: this is the previous channel, already on its
        # way out under its own steam.
        with autoreleasepool():
            del opened, delegate
        await asyncio.sleep(_SETTLE_SECONDS)

    raise ConnectionFailedError(
        f"channel {number} on {address} is already open and did not come free"
    )


async def _connect(device: _Device, address: str) -> None:
    """
    Bring up the baseband link to a device.

    Args:
        device: The ``IOBluetoothDevice``.
        address: Its address, for the error message.

    Raises:
        DeviceNotFoundError: The device did not answer.
        ConnectionFailedError: The link could not be brought up.
    """
    complete = _ConnectionComplete.alloc().init()
    _hold(complete)
    try:
        _check(device.openConnection(complete))
        await complete.future
    except MachError as error:
        _release(complete)
        raise _translate(error, address) from error


async def _look_up(device: _Device, address: str, service: UUID) -> int:
    """
    Find which RFCOMM channel a service is on.

    The cached record is tried first. macOS fills its cache from a real SDP
    query rather than from advertising data, so a hit here is trustworthy.
    A miss is worth one query before giving up.

    Args:
        device: The connected ``IOBluetoothDevice``.
        address: Its address, for the error message.
        service: The service to find.

    Returns:
        The RFCOMM channel number.

    Raises:
        ServiceNotFoundError: The device does not offer that service.
    """
    uuid = _sdp_uuid(service)
    record = device.getServiceRecordForUUID(uuid)
    if not record:
        complete = _QueryComplete.alloc().init()
        _hold(complete)
        try:
            _check(device.performSDPQuery(complete))
            await complete.future
        except MachError as error:
            _release(complete)
            raise ServiceNotFoundError(
                f"could not read {address}'s services: {error}"
            ) from error
        record = device.getServiceRecordForUUID(uuid)

    if not record:
        raise ServiceNotFoundError(f"{address} does not offer {service}")

    # A channel number is one byte, and the framework declares the
    # out-parameter as a pointer to unsigned char -- which rubicon reads as
    # ``char *``, so it is the one ctypes type it will accept here. The value
    # comes back as a one-byte string rather than as a number.
    number = ctypes.c_char()
    if record.getRFCOMMChannelID(ctypes.byref(number)):
        raise ServiceNotFoundError(f"{service} on {address} is not an RFCOMM service")
    return number.value[0]


def _translate(error: MachError, address: str) -> RfcommError:
    """
    Turn an IOBluetooth status into one of ours.

    Args:
        error: What IOBluetooth reported.
        address: The device being reached.

    Returns:
        The error to raise instead.
    """
    if error.code == IOReturn.TIMEOUT:
        return DeviceNotFoundError(
            f"{address} did not answer. It may be switched off or out of range"
        )
    return ConnectionFailedError(f"could not reach {address}: {error}")


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

# kIOBluetoothUserNotificationChannelDirectionIncoming. Measured: IOBluetooth
# never passes on a registration for kIOBluetoothUserNotificationChannelDirectionAny.
_INCOMING = 1

# bluetoothd never registers these two with its RFCOMM stack (disassembly), so
# a service on one is not like a service anywhere else. They are left alone.
_NOT_CHOSEN = (10, 15)

# The protocol UUIDs a record names to say it is reached over RFCOMM.
_L2CAP = 0x0100
_RFCOMM = 0x0003


def _record(service: UUID, name: str, number: int) -> ObjCInstance:
    """
    Build the service record to publish.

    Args:
        service: The service class.
        name: The service name.
        number: The RFCOMM channel number.

    Returns:
        The record, in the dictionary form IOBluetooth publishes.
    """
    # Sequences as plain arrays. Written as data element dictionaries, which
    # is what the framework's own documentation shows, the encoder writes out
    # the dictionaries' key names in place of their contents (follow-ups
    # §10.1). Arrays it encodes correctly, measured.
    record: dict[str, Any] = {
        "0001": [_sdp_uuid(service)],
        "0004": [
            [IOBluetoothSDPUUID.uuid16(_L2CAP)],
            [
                IOBluetoothSDPUUID.uuid16(_RFCOMM),
                {
                    "DataElementType": 1,
                    "DataElementSize": 1,
                    "DataElementValue": number,
                },
            ],
        ],
        "0100": name,
    }
    return cast(ObjCInstance, ns_from_py(cast(Any, record)))


def _channels_in_use() -> set[int]:
    """
    Read which RFCOMM channels services published on this Mac name, by any
    process.

    Returns:
        The channel numbers, empty if the private path to them has gone.
    """
    try:
        manager = cast(_DeviceManagerClass, ObjCClass(_DEVICE_MANAGER))
        services = manager.sharedDeviceManager().getLocalServices()
        records = list(services.allValues()) if services is not None else []
    except Exception:
        logger.debug("could not read the local services", exc_info=True)
        return set()

    used = set()

    for record in records:
        number = ctypes.c_char()
        if not record.getRFCOMMChannelID(ctypes.byref(number)):
            used.add(number.value[0])

    return used


def _choose_channel(channel: int | None, avoiding: Collection[int]) -> int:
    """
    Settle which channel to serve on: one with no service record.

    Args:
        channel: The one asked for, or ``None`` to choose.
        avoiding: Channels this program is already serving on, from other
            processes.

    Returns:
        The channel number.

    Raises:
        ChannelInUseError: The one asked for has a record, or every one does.
    """
    # A channel that has had a record since Bluetooth was last switched on is
    # never served on again, because it may not work and nothing would say
    # so. Measured, with the disassembly of bluetoothd: a record can never be
    # withdrawn (each removal is answered OI_STATUS_INVALID_HANDLE, for the
    # handle it gave out), and every connection to a channel, accepted ones
    # included, is kept in a list never cleared. The next registration on the
    # channel, in a later process, takes one off the list and accepts it
    # instead of listening. The accept fails, the peer being long gone, and
    # the service never hears a peer at all.
    used = _channels_in_use() | set(avoiding)
    if channel is not None:
        if channel in used:
            raise ChannelInUseError(
                f"RFCOMM channel {channel} already has a service published on "
                "this Mac, by this program or another. macOS keeps a channel "
                "it has served on, and may not deliver connections on it "
                "again, until Bluetooth has been switched off and on. Do that, "
                "or let the library choose a channel."
            )
        return channel

    # From the top, which this Mac's own services leave alone.
    for number in range(30, 0, -1):
        if number not in used and number not in _NOT_CHOSEN:
            return number
    raise ChannelInUseError(
        "every RFCOMM channel already has a service published on this Mac. "
        "macOS frees them only when Bluetooth is switched off and on."
    )


class _OpenListener(NSObject):
    """
    Hears each channel a peer opens to a published service.
    """

    arrived: asyncio.Queue[ObjCInstance]

    @objc_method
    def init(self):
        self.arrived = asyncio.Queue()
        return self

    # Called on the main queue, so on the loop's own thread.
    @objc_method
    def channelOpened_channel_(
        self, _notification: ObjCInstance, channel: ObjCInstance
    ) -> None:
        self.arrived.put_nowait(channel)


class Service:
    """
    A published service, handing over the channels peers open to it.
    """

    def __init__(self, number: int, listener: _OpenListener) -> None:
        """
        Args:
            number: The RFCOMM channel it is on.
            listener: Hears each channel opened to it.
        """
        self.channel = number
        self._listener = listener

    async def accept(self) -> Channel:
        """
        Wait for a peer to open a channel.

        Returns:
            The channel, already open.

        Raises:
            Exception: The channel could not be taken over. It is closed, and
                the next one is unaffected.
        """
        opened = cast(_IncomingChannel, await self._listener.arrived.get())

        try:
            delegate = _ChannelDelegate.alloc().init()
            opened.setDelegate(delegate)
            if not opened.isOpen():
                # Closed while it waited, before it had a delegate to say so.
                delegate.ended.set()

            return Channel(opened, delegate, opened.getDevice())
        except Exception:
            with autoreleasepool():
                opened.closeChannel()
                del opened
            raise


@asynccontextmanager
async def serve(
    service: UUID,
    *,
    name: str,
    channel: int | None = None,
    avoiding: Collection[int] = (),
) -> AsyncGenerator[Service, None]:
    """
    Publish a service and hear the channels peers open to it. Call once per
    process.

    Args:
        service: The service class.
        name: The service name.
        channel: The RFCOMM channel, or ``None`` to choose one.
        avoiding: Channels this program is already serving on, from other
            processes.

    Yields:
        The service, withdrawn on exit. Channels already handed over carry on.

    Raises:
        BluetoothUnavailableError: Bluetooth cannot be used.
        ChannelInUseError: The channel is taken.
        ConnectionFailedError: The service record could not be published.
    """
    # Once per process: bluetoothd keeps one registration per process, each
    # replacing the last, and frees only the last when the process ends.
    # Every one before it holds a slot in its RFCOMM stack until Bluetooth is
    # next switched off, and once the slots run out nothing on this Mac can
    # serve -- measured, each registration reported as OI_STATUS_NO_RESOURCES
    # and every peer's connection left waiting. Unregistering never reaches
    # bluetoothd.
    #
    # Made before this, IOBluetooth drops the registration unsent.
    await _wait_until_ready()
    number = _choose_channel(channel, avoiding)

    listener = _OpenListener.alloc().init()
    # A selector, not a string: given one, rubicon stores a selector made of
    # its first letter, the listener does not respond to it, and IOBluetooth
    # silently lets every channel go -- measured.
    notification = IOBluetoothRFCOMMChannel.registerForChannelOpenNotifications(
        listener,
        selector=SEL("channelOpened:channel:"),
        withChannelID=number,
        direction=_INCOMING,
    )

    try:
        record = IOBluetoothSDPServiceRecord.publishedServiceRecordWithDictionary(
            _record(service, name, number)
        )
        if record is None:
            raise ConnectionFailedError(
                f"could not publish {service} on RFCOMM channel {number}"
            )

        try:
            yield Service(number, listener)
        finally:
            # Refused by this macOS (see _choose_channel), but asked, in case
            # another is not.
            record.removeServiceRecord()
    finally:
        notification.unregister()
        # Channels nobody took are let go of, which closes them.
        with autoreleasepool():
            while not listener.arrived.empty():
                cast(_Channel, listener.arrived.get_nowait()).closeChannel()


# --------------------------------------------------------------------------
# Odds and ends
# --------------------------------------------------------------------------


def _each(service: UUID | Collection[UUID] | None) -> list[UUID]:
    """
    Normalise the service filter to a list.

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


def _sdp_uuid(service: UUID) -> _UUID:
    """
    Turn a Python UUID into the framework's own.

    Args:
        service: The service UUID.

    Returns:
        The matching ``IOBluetoothSDPUUID``.
    """
    return IOBluetoothSDPUUID.uuidWithData(service.bytes)


def _normalise(address: str) -> str:
    """
    Put a Bluetooth address into the form this library reports.

    IOBluetooth writes device addresses with dashes and in lower case, and
    controller addresses with colons and in upper case. Callers compare these
    against addresses from other platforms, so they are all made to look the
    same.

    Args:
        address: The address as IOBluetooth wrote it.

    Returns:
        The address, colon separated and upper case.
    """
    return address.replace("-", ":").upper()
