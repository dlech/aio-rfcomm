# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
CoreBluetooth's classic manager: waiting for it to settle, and reporting
its state.
"""

# Do not add ``from __future__ import annotations`` to this module. rubicon
# reads the annotations on the Objective-C methods below at runtime to build
# each one's Objective-C type encoding, and under PEP 563 an annotation is
# stored as its own source text rather than the object, so a callback would
# be given the wrong signature and read rubbish off the stack.

import asyncio
import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

assert not TYPE_CHECKING or sys.platform == "darwin"

from rubicon.objc import (
    NSObject,
    ObjCInstance,
    objc_method,
)

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth.codes import (
    CBManagerAuthorization,
    CBManagerState,
)
from aio_rfcomm.backend.iobluetooth.helper.native import bindings
from aio_rfcomm.backend.iobluetooth.helper.native.bindings import (
    COORDINATOR,
    find_class,
)
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ConnectionFailedError,
    RfcommError,
)

logger = logging.getLogger(__name__)


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


# kBluetoothHCIPowerStateON. The framework's power states are C enum
# constants rather than exported symbols, so there is nothing to read out of
# the dylib at runtime and the value has to be written down.
_POWER_ON = 1


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


async def wait_until_ready() -> None:
    """
    Wait for CoreBluetooth to settle, and object if it settled unusably.

    CoreBluetooth discards every command until its manager reaches the
    powered-on state, and IOBluetooth issues ours without waiting: on macOS
    26.6.2, for the first tens of milliseconds of a process an open is
    dropped, logged only as "API MISUSE", and never reported. Nothing bounds
    the wait, because a manager that never settles is one no open would have
    worked on either.

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
    # would wait forever.
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
        coordinator = cast(_CoordinatorClass, find_class(COORDINATOR))
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
    before ``addObserver`` returns, measured on macOS 26.6.2 at 50 to 800
    microseconds.

    Args:
        manager: The classic manager.
    """
    await _wait_for_state(manager, lambda state: state not in _UNSETTLED)


async def report_states(report: Callable[[int, int], None]) -> NoReturn:
    """
    Report the manager's state now, and again on every change.

    IOBluetooth was not seen to report the radio any other way on macOS
    26.6.2: a channel open at the time hears its link drop, and that was all.
    Measured across a real toggle: the manager goes from powered on to powered
    off, on a framework thread, about a millisecond before the channel is told.

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
        # Measured on macOS 26.6.2 in a fresh process: the state settles while
        # the observer is being added, after the initial notification has read
        # it and before the observer hears of changes, so nothing reports it.
        # Read again now that the observer is in place, queued behind that
        # notification.
        asyncio.get_running_loop().call_soon(once, manager.state, read_authorization())

        # Unknown is documented as temporary with an update to follow, but in
        # a fresh process on macOS 26.6.2 the update was measured going
        # unreported. So while it is unknown, look again.
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
            # Both, because macOS 26.6.2 reports a radio this program may not
            # use as switched off -- so telling someone to turn Bluetooth on
            # would send them to a switch that is already on. The authorization
            # is what separates the two, as it does in the reported
            # availability.
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


def read_authorization() -> int:
    """
    Read whether this process may use Bluetooth at all.

    macOS 26.6.2 reports an unauthorized radio as switched off, so without this
    a program whose permission was never granted -- or was refused -- is told
    to turn Bluetooth on, which it already is. Reading this neither prompts nor
    touches the radio.

    Returns:
        A ``CBManagerAuthorization``. rubicon hands ``NSInteger`` back as a
        Python ``int`` already, so there is nothing to convert.
    """
    return bindings.CBManager.authorization


def read_power_state() -> bool:
    """
    Read whether the radio is switched on.

    Returns:
        True if the adapter is powered.
    """
    controller = bindings.IOBluetoothHostController.defaultController()
    return controller is not None and controller.powerState == _POWER_ON
