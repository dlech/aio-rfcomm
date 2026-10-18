# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Tests for waiting on CoreBluetooth's manager, and for reporting its state,
that need no Bluetooth hardware.

What CoreBluetooth's manager says about itself decides whether a channel can be
opened at all, and getting that wrong is either a hang or a misleading error.
A fake manager stands in for it: an ordinary Objective-C object with a
key-value observable property, which is all the real one is from here.
"""

import asyncio
import sys
from ctypes import c_int
from typing import TYPE_CHECKING

assert not TYPE_CHECKING or sys.platform == "darwin"

import pytest

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)

from rubicon.objc.runtime import set_ivar

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth.codes import CBManagerAuthorization, CBManagerState
from aio_rfcomm.backend.iobluetooth.helper import native
from aio_rfcomm.backend.iobluetooth.helper.native import (
    bindings,
)
from aio_rfcomm.backend.iobluetooth.helper.native.manager import wait_until_ready
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ConnectionFailedError,
)
from tests.fake_iobluetooth import (
    FakeAuthorization,
    FakeCoordinatorClass,
    FakeManager,
    SettlingManager,
    change_state,
    # An autouse fixture, used by being imported.
    clean_state,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    is_observed,
    until_observed,
)

# Long enough for a notification to cross the run loop, short enough that a
# test which hangs is still a quick failure.
SETTLE = 0.05


# --------------------------------------------------------------------------
# Waiting for the manager
# --------------------------------------------------------------------------


async def test_a_manager_already_powered_on_is_not_waited_for(
    manager: FakeManager,
) -> None:
    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await wait_until_ready()


async def test_an_unknown_state_is_waited_out(manager: FakeManager) -> None:
    """
    Unknown is the state every process starts in, and it means nothing yet.
    """
    waiting = asyncio.ensure_future(wait_until_ready())
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
    waiting = asyncio.ensure_future(wait_until_ready())
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
    await wait_until_ready()

    manager.state = CBManagerState.RESETTING
    waiting = asyncio.ensure_future(wait_until_ready())
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

    monkeypatch.setattr(bindings, "ObjCClass", gone)
    async with asyncio.timeout(1):
        await wait_until_ready()


# --------------------------------------------------------------------------
# Settling on something unusable
# --------------------------------------------------------------------------


async def test_a_switched_off_radio_is_reported_not_waited_for(
    manager: FakeManager,
) -> None:
    """
    Powered off settles as firmly as powered on, so waiting for powered on
    alone would wait forever.
    """
    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        async with asyncio.timeout(1):
            await wait_until_ready()
    assert raised.value.reason is Availability.SWITCHED_OFF


async def test_a_refused_radio_does_not_look_switched_off(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    macOS 26.6.2 reports a radio this program may not use as switched off, and
    telling someone to turn on Bluetooth that is already on helps nobody.
    """
    monkeypatch.setattr(
        bindings, "CBManager", FakeAuthorization(CBManagerAuthorization.DENIED)
    )
    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        await wait_until_ready()
    assert raised.value.reason is Availability.NOT_ALLOWED


async def test_an_unauthorized_state_is_a_permission_error(
    manager: FakeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bindings, "CBManager", FakeAuthorization(CBManagerAuthorization.DENIED)
    )
    manager.state = CBManagerState.UNAUTHORIZED
    with pytest.raises(BluetoothUnavailableError) as raised:
        await wait_until_ready()
    assert raised.value.reason is Availability.NOT_ALLOWED


async def test_a_machine_with_no_bluetooth_is_not_an_adapter_that_is_off(
    manager: FakeManager,
) -> None:
    manager.state = CBManagerState.UNSUPPORTED
    with pytest.raises(BluetoothUnavailableError) as raised:
        await wait_until_ready()
    assert raised.value.reason is Availability.UNSUPPORTED


async def test_a_state_from_the_future_is_still_a_failure(
    manager: FakeManager,
) -> None:
    """
    Settling on something unrecognized must say so rather than be taken for
    one of the states that happens to be handled.
    """
    manager.state = 9
    with pytest.raises(ConnectionFailedError, match="9"):
        await wait_until_ready()


async def test_a_powered_on_in_the_middle_of_a_toggle_is_taken_at_face_value(
    manager: FakeManager,
) -> None:
    """
    On macOS 26.6.2, a radio switched off and on again was seen reporting
    resetting, then powered on, then powered off, then powered on, all within
    four seconds. Nothing here can tell that middle powered-on from a real one,
    and the documented contract is that each value supersedes the last -- so it
    is taken, and the one that matters is the state read by the next open.
    """
    manager.state = CBManagerState.RESETTING
    waiting = asyncio.ensure_future(wait_until_ready())
    await asyncio.sleep(SETTLE)
    assert not waiting.done()

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await waiting

    # The radio really does go, a moment later.
    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        await wait_until_ready()
    assert raised.value.reason is Availability.SWITCHED_OFF

    manager.state = CBManagerState.POWERED_ON
    async with asyncio.timeout(1):
        await wait_until_ready()


async def test_a_power_on_from_a_framework_thread_is_heard(
    manager: FakeManager,
) -> None:
    """
    Measured on macOS 26.6.2 at startup: the move to powered on arrived on a
    framework thread, where nothing that belongs to the event loop may be
    touched.
    """
    waiting = asyncio.ensure_future(wait_until_ready())
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
    Measured on a clean toggle on macOS 26.6.2: powered on, then powered off,
    on a framework thread.
    """
    manager.state = CBManagerState.POWERED_ON
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        native.report_states(
            lambda state, authorization: reports.append((state, authorization))
        )
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
    Measured on macOS 26.6.2 when switching off restarted bluetoothd:
    resetting, a powered-on from the restarted daemon, and only then powered
    off. What each means is for the other side of the socket to decide, so all
    are passed on.
    """
    manager.state = CBManagerState.POWERED_ON
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        native.report_states(lambda state, _auth: reports.append((state, 0)))
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
    Measured on macOS 26.6.2 in fresh processes: the first report said unknown,
    the state was powered on half a second later, and nothing ever said so.
    """
    settling = SettlingManager.alloc().init()

    def find_class(_name: str) -> FakeCoordinatorClass:
        return FakeCoordinatorClass(settling)

    monkeypatch.setattr(bindings, "ObjCClass", find_class)
    monkeypatch.setattr(
        bindings, "CBManager", FakeAuthorization(CBManagerAuthorization.ALLOWED_ALWAYS)
    )
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        native.report_states(lambda state, _auth: reports.append((state, 0)))
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
        native.report_states(lambda state, _auth: reports.append((state, 0)))
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
    watching = asyncio.ensure_future(
        native.report_states(lambda _state, _authorization: None)
    )
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

    monkeypatch.setattr(bindings, "ObjCClass", gone)
    monkeypatch.setattr(bindings, "IOBluetoothHostController", _Controller)
    monkeypatch.setattr(
        bindings, "CBManager", FakeAuthorization(CBManagerAuthorization.ALLOWED_ALWAYS)
    )
    reports: list[tuple[int, int]] = []
    watching = asyncio.ensure_future(
        native.report_states(
            lambda state, authorization: reports.append((state, authorization))
        )
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
