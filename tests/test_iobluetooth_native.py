# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Tests for the macOS native layer that need no Bluetooth hardware.

What CoreBluetooth's manager says about itself decides whether a channel can be
opened at all, and getting that wrong is either a hang or a misleading error.
A fake manager stands in for it: an ordinary Objective-C object with a
key-value observable property, which is all the real one is from here.
"""

# Measurements cited in this module were made on macOS 26.6.2 (25G83), with a
# BCM_4387 controller on firmware 23.5.636.5345, unless a claim says otherwise.
# The peers were a Linux machine, on BlueZ 5.72 until 2026-10-05 and BlueZ 5.85
# after, and a Pybricks EV3 on firmware 4.1.0b5. Claims that depend on the
# peer name its version.

import asyncio
import ctypes
import os
import socket
import sys
import threading
import time
from ctypes import c_int
from typing import Any, cast
from uuid import UUID

import pytest
from typing_extensions import override

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)

from rubicon.objc import NSObject, ObjCInstance, ns_from_py, objc_method, objc_property
from rubicon.objc.runtime import set_ivar

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth import _native
from aio_rfcomm.backend.iobluetooth._codes import CBManagerAuthorization, CBManagerState
from aio_rfcomm.backend.iobluetooth._native import _Device
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelInUseError,
    ConnectionFailedError,
    DeviceNotFoundError,
    ServiceNotFoundError,
    UnsupportedOperationError,
)
from tests.fake_iobluetooth import (
    FakeAuthorization,
    FakeCoordinatorClass,
    FakeDeviceClass,
    FakeLocalRecord,
    FakeManager,
    SettlingManager,
    change_state,
    clean_state,  # noqa: F401 -- an autouse fixture, used by being imported
    fake_framework_parts,
    is_observed,
    until_observed,
    wrap_channel,
)

# Long enough for a notification to cross the run loop, short enough that a
# test which hangs is still a quick failure.
SETTLE = 0.05


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    """
    A fake manager, reached exactly as the real one is.

    Only the Objective-C class lookup is replaced, so everything from
    _classic_manager down is the code under test.

    Returns:
        The fake, starting out in the unknown state and authorised.
    """
    fake = FakeManager.alloc().init()
    found = FakeCoordinatorClass(fake)
    monkeypatch.setattr(_native, "ObjCClass", lambda _name: found)
    monkeypatch.setattr(
        _native, "CBManager", FakeAuthorization(CBManagerAuthorization.ALLOWED_ALWAYS)
    )
    return fake


# --------------------------------------------------------------------------
# Waiting for the manager
# --------------------------------------------------------------------------


async def test_a_manager_already_powered_on_is_not_waited_for(
    manager: FakeManager,
) -> None:
    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await _native._wait_until_ready()


async def test_an_unknown_state_is_waited_out(manager: FakeManager) -> None:
    """
    Unknown is the state every process starts in, and it means nothing yet.
    """
    waiting = asyncio.ensure_future(_native._wait_until_ready())
    await asyncio.sleep(SETTLE)
    assert not waiting.done()

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await waiting


async def test_resetting_is_waited_out_rather_than_reported(
    manager: FakeManager,
) -> None:
    """
    A radio switched off and on again passes through resetting, which promises
    another value rather than being one.
    """
    manager.state = CBManagerState.RESETTING
    waiting = asyncio.ensure_future(_native._wait_until_ready())
    await asyncio.sleep(SETTLE)
    assert not waiting.done()

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await waiting


async def test_a_later_reset_is_waited_out_too(manager: FakeManager) -> None:
    """
    The state can go back to being provisional, so the wait cannot be done
    once per process and then assumed.
    """
    manager.state = CBManagerState.POWERED_ON
    await _native._wait_until_ready()

    manager.state = CBManagerState.RESETTING
    waiting = asyncio.ensure_future(_native._wait_until_ready())
    await asyncio.sleep(SETTLE)
    assert not waiting.done()

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await waiting


async def test_a_manager_that_cannot_be_reached_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A macOS without the private path is one this can say nothing about, so it
    carries on rather than refusing to work at all.
    """

    def gone(_name: str) -> object:
        raise ValueError("no Objective-C class named that")

    monkeypatch.setattr(_native, "ObjCClass", gone)
    async with asyncio.timeout(1):
        await _native._wait_until_ready()


# --------------------------------------------------------------------------
# Settling on something unusable
# --------------------------------------------------------------------------


async def test_a_switched_off_radio_is_reported_not_waited_for(
    manager: FakeManager,
) -> None:
    """
    Powered off settles as firmly as powered on, so waiting for powered on
    alone would wait for ever.
    """
    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        async with asyncio.timeout(1):
            await _native._wait_until_ready()
    assert raised.value.reason is Availability.SWITCHED_OFF


async def test_a_refused_radio_does_not_look_switched_off(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    macOS reports a radio this program may not use as switched off, and
    telling someone to turn on Bluetooth that is already on helps nobody.
    """
    monkeypatch.setattr(
        _native, "CBManager", FakeAuthorization(CBManagerAuthorization.DENIED)
    )
    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        await _native._wait_until_ready()
    assert raised.value.reason is Availability.NOT_ALLOWED


async def test_an_unauthorized_state_is_a_permission_error(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _native, "CBManager", FakeAuthorization(CBManagerAuthorization.DENIED)
    )
    manager.state = CBManagerState.UNAUTHORIZED
    with pytest.raises(BluetoothUnavailableError) as raised:
        await _native._wait_until_ready()
    assert raised.value.reason is Availability.NOT_ALLOWED


async def test_a_machine_with_no_bluetooth_is_not_an_adapter_that_is_off(
    manager: FakeManager,
) -> None:
    manager.state = CBManagerState.UNSUPPORTED
    with pytest.raises(BluetoothUnavailableError) as raised:
        await _native._wait_until_ready()
    assert raised.value.reason is Availability.UNSUPPORTED


async def test_a_state_from_the_future_is_still_a_failure(
    manager: FakeManager,
) -> None:
    """
    Settling on something unrecognised must say so rather than be taken for
    one of the states that happens to be handled.
    """
    manager.state = 9
    with pytest.raises(ConnectionFailedError, match="9"):
        await _native._wait_until_ready()


async def test_a_powered_on_in_the_middle_of_a_toggle_is_taken_at_face_value(
    manager: FakeManager,
) -> None:
    """
    A radio switched off and on again was seen reporting resetting, then
    powered on, then powered off, then powered on, all within four seconds.
    Nothing here can tell that middle powered-on from a real one, and the
    documented contract is that each value supersedes the last -- so it is
    taken, and the one that matters is the state read by the next open.
    """
    manager.state = CBManagerState.RESETTING
    waiting = asyncio.ensure_future(_native._wait_until_ready())
    await asyncio.sleep(SETTLE)
    assert not waiting.done()

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await waiting

    # The radio really does go, a moment later.
    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        await _native._wait_until_ready()
    assert raised.value.reason is Availability.SWITCHED_OFF

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await _native._wait_until_ready()


async def test_a_power_on_from_a_framework_thread_is_heard(
    manager: FakeManager,
) -> None:
    """
    Measured at startup: the move to powered on arrived on a framework
    thread, where nothing that belongs to the event loop may be touched.
    """
    waiting = asyncio.ensure_future(_native._wait_until_ready())
    await asyncio.sleep(SETTLE)
    assert not waiting.done()

    change_state(manager, CBManagerState.POWERED_ON)
    async with asyncio.timeout(1):
        await waiting


# --------------------------------------------------------------------------
# Noticing the radio switched off
# --------------------------------------------------------------------------


async def _reported(
    reports: list[tuple[int, int]], count: int
) -> list[tuple[int, int]]:
    async with asyncio.timeout(1):
        while len(reports) < count:
            await asyncio.sleep(0)
    return reports


async def test_every_state_is_reported_as_it_comes(manager: FakeManager) -> None:
    """
    Measured on a clean toggle: powered on, then powered off, on a framework
    thread.
    """
    manager.state = CBManagerState.POWERED_ON
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        _native.report_states(lambda *each: reports.append(each))
    )
    try:
        await until_observed(manager)
        change_state(manager, CBManagerState.POWERED_OFF)
        allowed = CBManagerAuthorization.ALLOWED_ALWAYS
        assert await _reported(reports, 2) == [
            (CBManagerState.POWERED_ON, allowed),
            (CBManagerState.POWERED_OFF, allowed),
        ]
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)


async def test_a_restart_is_reported_state_by_state(manager: FakeManager) -> None:
    """
    Measured when switching off restarted bluetoothd: resetting, a powered-on
    from the restarted daemon, and only then powered off. What each means is
    for the other side of the socket to decide, so all are passed on.
    """
    manager.state = CBManagerState.POWERED_ON
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        _native.report_states(lambda state, _auth: reports.append((state, 0)))
    )
    try:
        await until_observed(manager)
        change_state(
            manager,
            CBManagerState.RESETTING,
            CBManagerState.POWERED_ON,
            CBManagerState.POWERED_OFF,
        )
        states = [state for state, _ in await _reported(reports, 4)]
        assert states == [
            CBManagerState.POWERED_ON,
            CBManagerState.RESETTING,
            CBManagerState.POWERED_ON,
            CBManagerState.POWERED_OFF,
        ]
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)


async def test_a_state_that_settles_unheard_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured in fresh processes: the first report said unknown, the state was
    powered on half a second later, and nothing ever said so.
    """
    settling = SettlingManager.alloc().init()
    monkeypatch.setattr(
        _native, "ObjCClass", lambda _name: FakeCoordinatorClass(settling)
    )
    monkeypatch.setattr(
        _native, "CBManager", FakeAuthorization(CBManagerAuthorization.ALLOWED_ALWAYS)
    )
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        _native.report_states(lambda state, _auth: reports.append((state, 0)))
    )
    try:
        states = [state for state, _ in await _reported(reports, 2)]
        assert states == [CBManagerState.UNKNOWN, CBManagerState.POWERED_ON]
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)


async def test_a_state_still_unknown_after_observing_is_looked_at_again(
    manager: FakeManager,
) -> None:
    """
    The same unreported settling, landing after the observer is in place.
    """
    manager.state = CBManagerState.UNKNOWN
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        _native.report_states(lambda state, _auth: reports.append((state, 0)))
    )
    try:
        await until_observed(manager)
        await asyncio.sleep(SETTLE)
        set_ivar(manager, "_state", c_int(int(CBManagerState.POWERED_ON)))
        states = [state for state, _ in await _reported(reports, 2)]
        assert states == [CBManagerState.UNKNOWN, CBManagerState.POWERED_ON]
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)


async def test_giving_up_on_the_watch_stops_observing(manager: FakeManager) -> None:
    """
    An observer left registered would be called on a framework thread for the
    rest of the process, into a loop that may have gone.
    """
    manager.state = CBManagerState.POWERED_ON
    watching = asyncio.ensure_future(_native.report_states(lambda *_each: None))
    await until_observed(manager)
    watching.cancel()
    await asyncio.gather(watching, return_exceptions=True)
    assert not is_observed(manager)


class _Controller:
    """
    Stands in for ``IOBluetoothHostController``, asked only its power state.
    """

    powerState = 1  # kBluetoothHCIPowerStateON

    @classmethod
    def defaultController(cls) -> "_Controller":
        return cls()


async def test_a_manager_that_cannot_be_reached_reports_the_power_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def gone(_name: str) -> object:
        raise ValueError("no Objective-C class named that")

    monkeypatch.setattr(_native, "ObjCClass", gone)
    monkeypatch.setattr(_native, "IOBluetoothHostController", _Controller)
    monkeypatch.setattr(
        _native, "CBManager", FakeAuthorization(CBManagerAuthorization.ALLOWED_ALWAYS)
    )
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        _native.report_states(lambda *each: reports.append(each))
    )
    try:
        await asyncio.sleep(SETTLE)
        assert reports == [
            (CBManagerState.POWERED_ON, CBManagerAuthorization.ALLOWED_ALWAYS)
        ]
        assert not watching.done()
    finally:
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)


# --------------------------------------------------------------------------
# Letting a closed channel's link finish dying
# --------------------------------------------------------------------------

ADDRESS = "00:1A:7D:DA:71:0C"


async def test_a_device_never_used_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    began = asyncio.get_running_loop().time()
    await _native._wait_for_relink(ADDRESS)
    assert asyncio.get_running_loop().time() - began < _native._RELINK_SECONDS / 2


async def test_a_channel_just_closed_is_waited_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reopening inside the window is swallowed by the framework, so the wait is
    what the next open has instead of a lost second.
    """
    _native._closed_at[ADDRESS] = time.monotonic()
    began = time.monotonic()
    await _native._wait_for_relink(ADDRESS)
    assert time.monotonic() - began >= _native._RELINK_SECONDS * 0.9


async def test_only_the_remainder_of_the_window_is_waited_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    It is a deadline rather than a delay: a caller that did anything at all in
    between pays for none of it.
    """
    half = _native._RELINK_SECONDS / 2
    _native._closed_at[ADDRESS] = time.monotonic() - half
    began = time.monotonic()
    await _native._wait_for_relink(ADDRESS)
    waited = time.monotonic() - began
    assert waited < _native._RELINK_SECONDS
    assert waited > 0


async def test_a_window_long_gone_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _native,
        "_closed_at",
        {ADDRESS: time.monotonic() - _native._RELINK_SECONDS * 10},
    )
    began = time.monotonic()
    await _native._wait_for_relink(ADDRESS)
    assert time.monotonic() - began < _native._RELINK_SECONDS / 2


# --------------------------------------------------------------------------
# One channel per device
# --------------------------------------------------------------------------


@pytest.fixture
def devices(manager: FakeManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A powered-on manager and a device class that needs no radio.
    """
    manager.state = CBManagerState.POWERED_ON
    monkeypatch.setattr(_native, "IOBluetoothDevice", FakeDeviceClass)
    monkeypatch.setattr(_native, "IOBluetoothRFCOMMChannel", wrap_channel)


async def test_an_address_that_is_not_one_is_refused(devices: None) -> None:
    with pytest.raises(DeviceNotFoundError):
        async with _native.open_channel(FakeDeviceClass.unknown, channel=4):
            pass


async def test_a_second_channel_to_one_device_is_refused(devices: None) -> None:
    """
    macOS closes channels in the order they were opened rather than the one
    asked for, so holding two to a device loses both.
    """
    _native._in_use.add(ADDRESS.upper())
    with pytest.raises(UnsupportedOperationError, match="already has"):
        async with _native.open_channel(ADDRESS, channel=4):
            pass


async def test_a_channel_to_another_device_is_not_refused(
    devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The claim is per device: two at once share nothing.
    """
    _native._in_use.add("00:11:22:33:44:55")
    monkeypatch.setattr(FakeDeviceClass, "connected", False)
    monkeypatch.setattr(FakeDeviceClass, "answers", False)
    with pytest.raises(DeviceNotFoundError, match="did not answer"):
        async with _native.open_channel(ADDRESS, channel=4):
            pass


async def test_the_claim_is_let_go_of_when_the_open_fails(
    devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A device whose open failed must not be left unopenable for the rest of the
    process.
    """

    monkeypatch.setattr(FakeDeviceClass, "connected", False)
    monkeypatch.setattr(FakeDeviceClass, "answers", False)
    with pytest.raises(DeviceNotFoundError):
        async with _native.open_channel(ADDRESS, channel=4):
            pass
    assert _native._in_use == set()


# --------------------------------------------------------------------------
# Asking again when an open is never reported
# --------------------------------------------------------------------------


class HandedBack:
    """
    An ``IOBluetoothRFCOMMChannel`` that is only ever asked two things.
    """

    def __init__(self, open_already: bool) -> None:
        self.open_already = open_already

    def isOpen(self) -> bool:
        return self.open_already

    def closeChannel(self) -> int:
        return 0


class FlakyDevice:
    """
    A device that reports an open only once it has been asked enough times.

    Implements only what ``_open_reported`` touches, so it is cast to the
    device protocol at the call sites rather than pretending to be a whole one.

    Args:
        report_on: Which attempt finally reaches the delegate.
        open_already: How many times the framework hands back the previous
            channel before letting go of it.
    """

    def __init__(self, report_on: int, open_already: int = 0) -> None:
        self.report_on = report_on
        self.open_already = open_already
        self.attempts = 0

    def isConnected(self) -> bool:
        return True

    def openRFCOMMChannelAsync(
        self, _out: object, /, *, withChannelID: int, delegate: object
    ) -> int:
        self.attempts += 1
        if self.attempts >= self.report_on:
            opened = delegate.opened  # pyright: ignore[reportAttributeAccessIssue]
            asyncio.get_running_loop().call_soon(opened.set_result, None)
        return 0


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The framework stood in for, and its waits shortened so tests stay quick.
    """
    monkeypatch.setattr(_native, "_OPEN_REPORT_SECONDS", 0.05)
    monkeypatch.setattr(_native, "_SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(_native, "_CLOSE_TIMEOUT", 0.05)
    monkeypatch.setattr(_native, "_open_results", {})
    monkeypatch.setattr(
        _native,
        "IOBluetoothRFCOMMChannel",
        lambda _handle: HandedBack(bool(_pending.pop(0)) if _pending else False),
    )


_pending: list[int] = []


async def test_an_open_reported_at_once_is_not_asked_for_twice(quick: None) -> None:
    device = FlakyDevice(report_on=1)
    await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_an_open_that_is_never_reported_is_asked_for_again(
    quick: None,
) -> None:
    """
    A failed open that reports no error reaches neither delegate method, so
    silence is the only symptom there is to act on.
    """
    device = FlakyDevice(report_on=3)
    await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == 3


async def test_an_open_never_reported_at_all_gives_up_and_says_so(
    quick: None,
) -> None:
    device = FlakyDevice(report_on=99)
    with pytest.raises(ConnectionFailedError, match="never reported"):
        await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == _native._OPEN_REPORT_ATTEMPTS


class RefusingDevice(FlakyDevice):
    """
    A device whose every open bluetoothd fails, without telling the delegate.

    Measured against BlueZ 5.85, for a channel nothing listens on: bluetoothd
    reports result 913 for each attempt, within a second of it, and neither
    delegate method is ever called.

    Args:
        result: What bluetoothd reports.
    """

    def __init__(self, result: int = 913) -> None:
        super().__init__(report_on=99)
        self.result = result

    @override
    def openRFCOMMChannelAsync(
        self, _out: object, /, *, withChannelID: int, delegate: object
    ) -> int:
        self.attempts += 1
        asyncio.get_running_loop().call_soon(
            _native.record_open_result, ADDRESS, withChannelID, self.result
        )
        return 0


async def test_a_refused_channel_is_not_asked_for_again(quick: None) -> None:
    device = RefusingDevice()
    with pytest.raises(ServiceNotFoundError, match="refused channel 4"):
        await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_a_device_hanging_up_as_the_channel_opens_is_not_asked_again(
    quick: None,
) -> None:
    device = RefusingDevice(927)
    with pytest.raises(ConnectionFailedError, match="hung up as channel 4 opened"):
        await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_any_other_answer_from_bluetoothd_is_not_asked_for_again(
    quick: None,
) -> None:
    device = RefusingDevice(911)
    with pytest.raises(ConnectionFailedError, match="bluetoothd said 911"):
        await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_an_open_bluetoothd_says_succeeded_is_asked_for_again(
    quick: None,
) -> None:
    """
    The open went through, so the silence is the framework's own.
    """
    device = RefusingDevice(0)
    with pytest.raises(ConnectionFailedError, match="never reported"):
        await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == _native._OPEN_REPORT_ATTEMPTS


class _Peer(NSObject):
    """
    A ``CBClassicPeer``, which the hook only asks for its address.
    """

    addressString = objc_property()


def _peer() -> NSObject:
    peer = _Peer.alloc().init()
    # As measured: CBClassicPeer writes the address upper case with colons.
    cast(Any, peer).addressString = "00:1A:7D:DA:71:0C"
    return peer


def _pointer(thing: object) -> int:
    return cast(int, cast(Any, thing).ptr.value)


def _read(info: dict[str, int]) -> _native._OpenResult:
    ns = ns_from_py(cast(Any, info))
    return _native._read_open_result(_pointer(_peer()), _pointer(ns))


async def test_the_result_the_daemon_sent_is_noted_by_address_and_channel(
    quick: None,
) -> None:
    _native._note_open_result(
        _read({"kCBMsgArgResult": 913, "kCBMsgArgRFCOMMChannelID": 12})
    )
    assert _native._open_results == {(ADDRESS, 12): 913}


async def test_a_result_without_a_channel_is_ignored(quick: None) -> None:
    _native._note_open_result(_read({"kCBMsgArgResult": 913}))
    assert _native._open_results == {}


async def test_a_value_of_another_type_is_left_unread(quick: None) -> None:
    """
    Sending it intValue would raise an Objective-C exception on the
    framework's thread, which nothing could catch.
    """
    said = _read({"kCBMsgArgResult": 913, "kCBMsgArgSocket": cast(Any, "eight")})
    assert said.result == 913
    assert said.socket is None


def test_the_daemon_s_report_is_read_off_the_main_thread() -> None:
    """
    Measured: the framework calls the hook on a thread of its own, where
    rubicon racing the main thread to load a class's methods ended a helper.
    """
    peer = _peer()
    info = cast(
        ObjCInstance,
        ns_from_py({"kCBMsgArgResult": 913, "kCBMsgArgRFCOMMChannelID": 12}),
    )
    read: list[_native._OpenResult] = []
    framework = threading.Thread(
        target=lambda: read.append(
            _native._read_open_result(_pointer(peer), _pointer(info))
        )
    )
    framework.start()
    framework.join()
    assert read == [_native._OpenResult(ADDRESS, 12, 913, None)]


def _failed_open(result: int, descriptor: int) -> _native._OpenResult:
    return _native._OpenResult(ADDRESS, 4, result, descriptor)


def _is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return False

    return True


# Never set: the stream's own read waits, as it did on macOS 26.6.2 on a socket
# that had already ended.
_never = threading.Event()


class _Stream(NSObject):
    """
    The input stream of a channel, over a real socket.
    """

    @objc_method
    def hasBytesAvailable(self) -> bool:
        # Yes even with nothing there, as measured on macOS 26.6.2.
        return True

    @objc_method
    def propertyForKey_(self, key: ObjCInstance) -> ObjCInstance:
        descriptor: int | None = cast(Any, self).descriptor
        said = b""

        if str(key) == "kCFStreamPropertySocketNativeHandle" and descriptor is not None:
            said = bytes(c_int(descriptor))

        return cast(ObjCInstance, ns_from_py(said))

    @objc_method
    def read_maxLength_(self, buffer: ctypes.c_void_p, length: ctypes.c_ulong) -> int:
        _never.wait()
        return 0


class _RFCOMMChannel(NSObject):
    """
    CoreBluetooth's ``CBRFCOMMChannel``, which only gives its stream here.
    """

    inputStream = objc_property()


class _ClosingChannel(NSObject):
    """
    An ``IOBluetoothRFCOMMChannel`` as its removal finds it.
    """

    rfcommchannel = objc_property()


def _closing_channel(descriptor: int | None) -> NSObject:
    stream = _Stream.alloc().init()
    cast(Any, stream).descriptor = descriptor
    rfcomm = _RFCOMMChannel.alloc().init()
    cast(Any, rfcomm).inputStream = stream
    channel = _ClosingChannel.alloc().init()
    cast(Any, channel).rfcommchannel = rfcomm
    return channel


def _read_unread_on_a_thread(channel: NSObject) -> bytes:
    # A thread with a bound, so that a read that waits fails the test rather
    # than hanging the suite.
    read: list[bytes] = []
    framework = threading.Thread(
        target=lambda: read.append(_native._read_unread(_pointer(channel))),
        daemon=True,
    )
    framework.start()
    framework.join(5)
    assert read, "the read waited"
    return read[0]


def test_what_a_closing_channel_had_not_read_is_read_out() -> None:
    left = bytes(range(256)) * 20 + b"tail"
    ours, theirs = socket.socketpair()

    with ours, theirs:
        theirs.sendall(left)
        theirs.shutdown(socket.SHUT_WR)

        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == left


def test_a_closing_channel_with_nothing_unread_does_not_wait() -> None:
    ours, theirs = socket.socketpair()

    with ours, theirs:
        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == b""


def test_a_socket_that_has_ended_with_nothing_left_does_not_wait() -> None:
    ours, theirs = socket.socketpair()

    with ours:
        theirs.close()

        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == b""


def test_what_arrived_is_read_out_without_waiting_for_the_end() -> None:
    ours, theirs = socket.socketpair()

    with ours, theirs:
        theirs.sendall(b"tail")

        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == b"tail"


def test_a_stream_that_does_not_say_its_socket_has_nothing_unread() -> None:
    assert _read_unread_on_a_thread(_closing_channel(None)) == b""


def test_a_channel_with_no_stream_left_has_nothing_unread() -> None:
    channel = _ClosingChannel.alloc().init()

    assert _native._read_unread(_pointer(channel)) == b""


async def test_bytes_read_out_at_close_arrive_before_the_end() -> None:
    delegate = cast(Any, _native._ChannelDelegate.alloc().init())
    channel = _ClosingChannel.alloc().init()
    _native._unread[_pointer(channel)] = b"tail"

    delegate.rfcommChannelClosed_(channel)

    assert delegate.arrived.get_nowait() == b"tail"
    assert delegate.ended.is_set()
    assert not _native._unread


def test_the_null_device_a_failed_open_leaves_is_closed() -> None:
    """
    Measured: one /dev/null descriptor left open per refused channel.
    """
    descriptor = os.open(os.devnull, os.O_RDONLY)
    _native._close_unclaimed_socket(_failed_open(913, descriptor))
    assert not _is_open(descriptor)


def test_a_real_socket_is_never_closed_whatever_the_result() -> None:
    ours, theirs = socket.socketpair()
    with ours, theirs:
        _native._close_unclaimed_socket(_failed_open(913, ours.fileno()))
        assert _is_open(ours.fileno())


def test_nothing_is_closed_after_a_success() -> None:
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        _native._close_unclaimed_socket(_failed_open(0, descriptor))
        assert _is_open(descriptor)
    finally:
        os.close(descriptor)


async def test_a_refusal_of_another_channel_is_not_taken_for_this_one(
    quick: None,
) -> None:
    _native.record_open_result(ADDRESS, 5, 913)
    device = FlakyDevice(report_on=99)
    with pytest.raises(ConnectionFailedError, match="never reported"):
        await _native._open_reported(cast(_Device, device), 4, ADDRESS)


async def test_a_refusal_left_from_before_is_forgotten(quick: None) -> None:
    """
    Something may be listening by now, so only this attempt's result counts.
    """
    _native.record_open_result(ADDRESS, 4, 913)
    device = FlakyDevice(report_on=2)
    await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == 2


async def test_a_channel_still_open_from_before_is_waited_out(quick: None) -> None:
    """
    The framework keeps handing back the previous channel until it has
    finished tearing it down, and a wrapper bound to one is write-only.
    """
    _pending[:] = [1, 1, 0]
    device = FlakyDevice(report_on=1)
    await _native._open_reported(cast(_Device, device), 4, ADDRESS)
    assert device.attempts == 3
    assert _pending == []


async def test_a_radio_switched_off_after_a_connection_is_reported(
    devices: None, quick: None, manager: FakeManager
) -> None:
    """
    The radio can go while a program is holding an adapter, and on macOS
    nothing reports that on its own -- so the state is read again at every
    open rather than settled once and assumed.
    """
    async with _native.open_channel(ADDRESS, channel=4):
        pass

    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        async with _native.open_channel(ADDRESS, channel=4):
            pass
    assert raised.value.reason is Availability.SWITCHED_OFF

    manager.state = CBManagerState.POWERED_ON
    async with _native.open_channel(ADDRESS, channel=4):
        pass


async def test_a_radio_switched_off_does_not_strand_the_device_claim(
    devices: None, quick: None, manager: FakeManager
) -> None:
    """
    Refusing an open before the claim is taken must leave nothing behind.
    """
    async with _native.open_channel(ADDRESS, channel=4):
        pass

    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        async with _native.open_channel(ADDRESS, channel=4):
            pass
    assert raised.value.reason is Availability.SWITCHED_OFF
    assert _native._in_use == set()


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

SERVICE = UUID("00eeffc0-1dea-4b1d-9f00-a100c0ffee03")


async def test_a_service_is_published_on_the_channel_it_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with _native.serve(SERVICE, name="chat", channel=None) as service:
        [published] = framework.records.published
        record = cast(Any, published.record)
        assert record["0100"] == "chat"
        rfcomm = record["0004"][1]
        assert rfcomm[1]["DataElementValue"] == service.channel
    assert published.withdrawn


async def test_one_registration_is_made_and_it_reaches_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured: given a string, the selector stored is its first letter, and
    every channel is silently let go -- so the registration has to be one the
    listener really answers.
    """
    framework = fake_framework_parts(monkeypatch)
    async with _native.serve(SERVICE, name="chat", channel=None) as service:
        fake = framework.channels.connect_in(ADDRESS, service.channel)
        async with asyncio.timeout(1):
            channel = await service.accept()
        try:
            assert channel.peer().address == ADDRESS
            fake.arrive(b"hello")
            async with asyncio.timeout(1):
                assert await channel.arrived.get() == b"hello"
            await channel.send(b"echo")
            assert bytes(fake.written) == b"echo"
        finally:
            await channel.close()
    assert len(framework.channels.registrations) == 1
    assert not fake.open


async def test_registering_waits_until_the_manager_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured: made before the manager is ready, a registration is never sent.
    """
    framework = fake_framework_parts(monkeypatch, state=CBManagerState.UNKNOWN)
    serving = asyncio.ensure_future(_native.serve(SERVICE, name="chat").__aenter__())
    await until_observed(framework.manager)
    assert not serving.done()
    change_state(framework.manager, CBManagerState.POWERED_ON)
    async with asyncio.timeout(1):
        await serving
    assert len(framework.channels.registrations) == 1


@pytest.mark.parametrize(
    ("in_use", "chosen"),
    [
        ([1, 8], 30),
        ([1, 8, 30], 29),
        # Never 15 or 10, which bluetoothd does not register with its stack.
        ([*range(16, 31), 11, 12, 13, 14], 9),
    ],
)
async def test_a_channel_is_chosen_away_from_this_mac_s_own(
    monkeypatch: pytest.MonkeyPatch, in_use: list[int], chosen: int
) -> None:
    framework = fake_framework_parts(monkeypatch)
    framework.devices.shared.records = [FakeLocalRecord(n) for n in in_use]
    async with _native.serve(SERVICE, name="chat") as service:
        assert service.channel == chosen


async def test_a_channel_this_mac_already_serves_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    with pytest.raises(ChannelInUseError, match="8"):
        async with _native.serve(SERVICE, name="chat", channel=8):
            pass


async def test_a_withdrawn_service_lets_new_channels_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Unregistering only stops this process listening. bluetoothd still takes
    the channel, and the framework, with nobody to give it to, closes it.
    """
    framework = fake_framework_parts(monkeypatch)
    async with _native.serve(SERVICE, name="chat") as service:
        number = service.channel
    late = framework.channels.connect_in(ADDRESS, number)
    assert not late.open


async def test_channels_nobody_took_are_closed_on_withdrawal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with _native.serve(SERVICE, name="chat") as service:
        waiting = framework.channels.connect_in(ADDRESS, service.channel)
        await asyncio.sleep(0)
    assert not waiting.open


async def test_a_channel_served_before_is_never_chosen_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Its record cannot be withdrawn, and a later registration on it may take a
    connection bluetoothd kept from before instead of listening -- measured.
    """
    framework = fake_framework_parts(monkeypatch)
    async with _native.serve(SERVICE, name="chat") as first:
        number = first.channel
    async with _native.serve(SERVICE, name="chat") as again:
        assert again.channel != number
    with pytest.raises(ChannelInUseError, match="switched off and on"):
        async with _native.serve(SERVICE, name="chat", channel=number):
            pass
    assert len(framework.records.published) == 2


async def test_channels_another_service_here_holds_are_avoided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    async with _native.serve(SERVICE, name="chat", avoiding=(30, 29)) as service:
        assert service.channel == 28
