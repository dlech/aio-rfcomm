# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Tests for the macOS native layer that need no Bluetooth hardware.

What CoreBluetooth's manager says about itself decides whether a channel can be
opened at all, and getting that wrong is either a hang or a misleading error.
A fake manager stands in for it: an ordinary Objective-C object with a
key-value observable property, which is all the real one is from here.
"""

import asyncio
import sys
import time
from typing import cast

import pytest

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)

from aio_rfcomm.backend.iobluetooth import _native
from aio_rfcomm.backend.iobluetooth._native import CBManagerState, _Device
from aio_rfcomm.errors import (
    AdapterNotFoundError,
    AdapterOffError,
    ConnectionFailedError,
    DeviceNotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
)
from tests.fake_iobluetooth import (
    ALLOWED,
    DENIED,
    FakeAuthorization,
    FakeCoordinatorClass,
    FakeDeviceClass,
    FakeManager,
    clean_state,  # noqa: F401 -- an autouse fixture, used by being imported
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
    monkeypatch.setattr(_native, "CBManager", FakeAuthorization(ALLOWED))
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
    with pytest.raises(AdapterOffError):
        async with asyncio.timeout(1):
            await _native._wait_until_ready()


async def test_a_refused_radio_does_not_look_switched_off(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    macOS reports a radio this program may not use as switched off, and
    telling someone to turn on Bluetooth that is already on helps nobody.
    """
    monkeypatch.setattr(_native, "CBManager", FakeAuthorization(DENIED))
    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(PermissionDeniedError):
        await _native._wait_until_ready()


async def test_an_unauthorized_state_is_a_permission_error(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_native, "CBManager", FakeAuthorization(DENIED))
    manager.state = CBManagerState.UNAUTHORIZED
    with pytest.raises(PermissionDeniedError):
        await _native._wait_until_ready()


async def test_a_machine_with_no_bluetooth_is_not_an_adapter_that_is_off(
    manager: FakeManager,
) -> None:
    manager.state = CBManagerState.UNSUPPORTED
    with pytest.raises(AdapterNotFoundError):
        await _native._wait_until_ready()


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
    with pytest.raises(AdapterOffError):
        await _native._wait_until_ready()

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await _native._wait_until_ready()


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
    with pytest.raises(AdapterOffError):
        async with _native.open_channel(ADDRESS, channel=4):
            pass

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
    with pytest.raises(AdapterOffError):
        async with _native.open_channel(ADDRESS, channel=4):
            pass
    assert _native._in_use == set()
