# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Tests for opening channels in the macOS native layer, with the framework
stood in for.
"""

import asyncio
import sys
import time
from typing import Any, cast
from uuid import UUID

import pytest
from typing_extensions import override

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)


from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth.codes import CBManagerState
from aio_rfcomm.backend.iobluetooth.helper import native
from aio_rfcomm.backend.iobluetooth.helper.native import (
    bindings,
    connecting,
    hooks,
)
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ConnectionFailedError,
    DeviceNotFoundError,
    ServiceNotFoundError,
    UnsupportedOperationError,
)
from tests.fake_iobluetooth import (
    ADDRESS,
    FakeChannel,
    FakeDevice,
    FakeDeviceClass,
    FakeManager,
    clean_state,  # noqa: F401 -- an autouse fixture, used by being imported
    fake_framework_parts,
    opened,
    stored_record,
    wrap_channel,
)

# --------------------------------------------------------------------------
# Letting a closed channel's link finish dying
# --------------------------------------------------------------------------


async def test_a_device_never_used_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    began = asyncio.get_running_loop().time()
    await connecting._wait_for_relink(ADDRESS)
    assert asyncio.get_running_loop().time() - began < connecting._RELINK_SECONDS / 2


async def test_a_channel_just_closed_is_waited_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reopening inside the window is swallowed by the framework, so the wait is
    what the next open has instead of a lost second.
    """
    connecting._closed_at[ADDRESS] = time.monotonic()
    began = time.monotonic()
    await connecting._wait_for_relink(ADDRESS)
    assert time.monotonic() - began >= connecting._RELINK_SECONDS * 0.9


async def test_only_the_remainder_of_the_window_is_waited_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    It is a deadline rather than a delay: a caller that did anything at all in
    between pays for none of it.
    """
    half = connecting._RELINK_SECONDS / 2
    connecting._closed_at[ADDRESS] = time.monotonic() - half
    began = time.monotonic()
    await connecting._wait_for_relink(ADDRESS)
    waited = time.monotonic() - began
    assert waited < connecting._RELINK_SECONDS
    assert waited > 0


async def test_a_window_long_gone_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connecting,
        "_closed_at",
        {ADDRESS: time.monotonic() - connecting._RELINK_SECONDS * 10},
    )
    began = time.monotonic()
    await connecting._wait_for_relink(ADDRESS)
    assert time.monotonic() - began < connecting._RELINK_SECONDS / 2


# --------------------------------------------------------------------------
# One channel per device
# --------------------------------------------------------------------------


@pytest.fixture
def devices(manager: FakeManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A powered-on manager and a device class that needs no radio.
    """
    manager.state = CBManagerState.POWERED_ON
    monkeypatch.setattr(bindings, "IOBluetoothDevice", FakeDeviceClass)
    monkeypatch.setattr(bindings, "IOBluetoothRFCOMMChannel", wrap_channel)


async def test_an_address_that_is_not_one_is_refused(devices: None) -> None:
    # A bug rather than a missing device: the library checks addresses first.
    with pytest.raises(ValueError, match="not a Bluetooth address"):
        async with native.open_channel(FakeDeviceClass.unknown, channel=4):
            pass


async def test_a_second_channel_to_one_device_is_refused(devices: None) -> None:
    """
    macOS 26.6.2 closes channels in the order they were opened rather than the
    one asked for, so holding two to a device loses both.
    """
    connecting._in_use.add(ADDRESS.upper())
    with pytest.raises(UnsupportedOperationError, match="already has"):
        async with native.open_channel(ADDRESS, channel=4):
            pass


async def test_a_channel_to_another_device_is_not_refused(
    devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The claim is per device: two at once share nothing.
    """
    connecting._in_use.add("00:11:22:33:44:55")
    monkeypatch.setattr(FakeDeviceClass, "connected", False)
    monkeypatch.setattr(FakeDeviceClass, "answers", False)
    with pytest.raises(DeviceNotFoundError, match="did not answer"):
        async with native.open_channel(ADDRESS, channel=4):
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
        async with native.open_channel(ADDRESS, channel=4):
            pass
    assert connecting._in_use == set()


class LateLanding(FakeDevice):
    """
    A device where an open of channel 22, given up on, lands on the next open.

    Measured on macOS 26.6.2 against BlueZ 5.85 under load, 22 times in an
    hour: asked for 25, which nothing listens on, bluetoothd refused it with
    913 and then connected an earlier ask for 22. The framework gave that to
    the channel asked for 25, which said it had opened. The refusal came first
    20 times, and the connection first twice.

    Args:
        refused_first: Whether the refusal comes before the connection.
    """

    def __init__(self, refused_first: bool) -> None:
        super().__init__(ADDRESS, connected=True, answers=True)
        self.refuses = {25}
        self.refused_first = refused_first
        self.stray: FakeChannel | None = None

    @override
    def openRFCOMMChannelAsync(
        self, _out: object, /, *, withChannelID: int, delegate: object
    ) -> int:
        if self.stray is not None:
            return super().openRFCOMMChannelAsync(
                _out, withChannelID=withChannelID, delegate=delegate
            )

        channel = FakeChannel(self, withChannelID, delegate)
        channel.connected_to = 22
        self.stray = channel
        self.channels.append(channel)
        opened.append(channel)
        refusal = (native.record_open_result, ADDRESS, withChannelID, 913)
        loop = asyncio.get_running_loop()
        if self.refused_first:
            loop.call_soon(*refusal)
            loop.call_soon(channel.complete_open)
        else:
            loop.call_soon(channel.complete_open)
            loop.call_later(0.01, *refusal)
        return 0


@pytest.mark.parametrize("refused_first", [True, False])
async def test_a_late_open_of_another_channel_is_closed_not_handed_out(
    devices: None, monkeypatch: pytest.MonkeyPatch, refused_first: bool
) -> None:
    monkeypatch.setattr(connecting, "_OPEN_REPORT_SECONDS", 0.05)
    monkeypatch.setattr(hooks, "open_results", {})
    device = LateLanding(refused_first)
    FakeDeviceClass.made[ADDRESS] = device

    with pytest.raises(ServiceNotFoundError, match="refused channel 25"):
        async with native.open_channel(ADDRESS, channel=25):
            pass

    assert device.stray is not None
    assert not device.stray.open


# --------------------------------------------------------------------------
# Asking again when an open is never reported
# --------------------------------------------------------------------------


class HandedBack:
    """
    An ``IOBluetoothRFCOMMChannel`` that is only ever asked two things.
    """

    # Says nothing about which channel it holds, so it is taken as the one
    # asked for.
    rfcommchannel = None

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
            # As the framework reports an open: through the delegate.
            report = cast(Any, delegate).rfcommChannelOpenComplete_status_
            asyncio.get_running_loop().call_soon(report, None, 0)
        return 0


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The framework stood in for, and its waits shortened so tests stay quick.
    """
    monkeypatch.setattr(connecting, "_OPEN_REPORT_SECONDS", 0.05)
    monkeypatch.setattr(connecting, "_SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(
        "aio_rfcomm.backend.iobluetooth.helper.native.channel._CLOSE_TIMEOUT", 0.05
    )
    monkeypatch.setattr(hooks, "open_results", {})
    monkeypatch.setattr(
        bindings,
        "IOBluetoothRFCOMMChannel",
        lambda _handle: HandedBack(bool(_pending.pop(0)) if _pending else False),
    )


_pending: list[int] = []


async def test_an_open_reported_at_once_is_not_asked_for_twice(quick: None) -> None:
    device = FlakyDevice(report_on=1)
    await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_an_open_that_is_never_reported_is_asked_for_again(
    quick: None,
) -> None:
    """
    A failed open that reports no error reaches neither delegate method, so
    silence is the only symptom there is to act on.
    """
    device = FlakyDevice(report_on=3)
    await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 3


async def test_an_open_never_reported_at_all_gives_up_and_says_so(
    quick: None,
) -> None:
    device = FlakyDevice(report_on=99)
    with pytest.raises(ConnectionFailedError, match="never reported"):
        await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == connecting._OPEN_REPORT_ATTEMPTS


class RefusingDevice(FlakyDevice):
    """
    A device whose every open bluetoothd fails, without telling the delegate.

    Measured on macOS 26.6.2 against BlueZ 5.85, for a channel nothing listens
    on: bluetoothd reports result 913 for each attempt, within a second of it,
    and neither delegate method is ever called.

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
            native.record_open_result, ADDRESS, withChannelID, self.result
        )
        return 0


async def test_a_refused_channel_is_not_asked_for_again(quick: None) -> None:
    device = RefusingDevice()
    with pytest.raises(ServiceNotFoundError, match="refused channel 4"):
        await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_a_device_hanging_up_as_the_channel_opens_is_not_asked_again(
    quick: None,
) -> None:
    device = RefusingDevice(927)
    with pytest.raises(ConnectionFailedError, match="hung up as channel 4 opened"):
        await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_a_channel_open_elsewhere_is_not_asked_for_again(quick: None) -> None:
    device = RefusingDevice(911)
    with pytest.raises(ConnectionFailedError, match="already open or opening"):
        await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 1


async def test_any_other_answer_from_bluetoothd_is_not_asked_for_again(
    quick: None,
) -> None:
    # 454 came once on macOS 26.6.2, opening a Pybricks EV3 just switched on.
    device = RefusingDevice(454)
    with pytest.raises(ConnectionFailedError, match="bluetoothd said 454"):
        await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 1


class StillGoingDevice(FlakyDevice):
    """
    A device whose first open is slow, so asking again is answered with 1902.

    Measured on macOS 26.6.2 against BlueZ 5.85 under load: an ask went
    unanswered for 5 s, asking again was answered with 1902 at once, and the
    first ask then opened its channel and was reported to the newest one.
    """

    def __init__(self) -> None:
        super().__init__(report_on=99)

    @override
    def openRFCOMMChannelAsync(
        self, _out: object, /, *, withChannelID: int, delegate: object
    ) -> int:
        self.attempts += 1
        loop = asyncio.get_running_loop()
        if self.attempts == 2:
            loop.call_soon(native.record_open_result, ADDRESS, withChannelID, 1902)
            report = cast(Any, delegate).rfcommChannelOpenComplete_status_
            loop.call_later(4 * connecting._OPEN_REPORT_SECONDS, report, None, 0)
        return 0


async def test_an_open_still_going_is_waited_for(quick: None) -> None:
    """
    Giving up on it would leave it to land later, on nobody or on the next
    open of another channel.
    """
    device = StillGoingDevice()
    await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 2


class Leftover:
    """
    A channel nobody holds, which the framework hands back until it is closed.
    """

    rfcommchannel = None

    def __init__(self) -> None:
        self.open = True

    def isOpen(self) -> bool:
        return self.open

    def closeChannel(self) -> int:
        self.open = False
        return 0


async def test_a_channel_nobody_holds_is_closed(
    quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An open given up on can land with no channel waiting to be told, and stays
    open: on macOS 26.6.2, for 22 s.
    """
    leftover = Leftover()
    monkeypatch.setattr(bindings, "IOBluetoothRFCOMMChannel", lambda _handle: leftover)
    device = FlakyDevice(report_on=1)
    await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert not leftover.open
    # Asked for until halfway, then once more after closing it.
    assert device.attempts == connecting._OPEN_ATTEMPTS // 2 + 2


async def test_an_open_bluetoothd_says_succeeded_is_asked_for_again(
    quick: None,
) -> None:
    """
    The open went through, so the silence is the framework's own.
    """
    device = RefusingDevice(0)
    with pytest.raises(ConnectionFailedError, match="never reported"):
        await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == connecting._OPEN_REPORT_ATTEMPTS


async def test_a_refusal_of_another_channel_is_not_taken_for_this_one(
    quick: None,
) -> None:
    native.record_open_result(ADDRESS, 5, 913)
    device = FlakyDevice(report_on=99)
    with pytest.raises(ConnectionFailedError, match="never reported"):
        await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)


async def test_a_refusal_left_from_before_is_forgotten(quick: None) -> None:
    """
    Something may be listening by now, so only this attempt's result counts.
    """
    native.record_open_result(ADDRESS, 4, 913)
    device = FlakyDevice(report_on=2)
    await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 2


async def test_a_channel_still_open_from_before_is_waited_out(quick: None) -> None:
    """
    The framework keeps handing back the previous channel until it has
    finished tearing it down, and a wrapper bound to one is write-only.
    """
    _pending[:] = [1, 1, 0]
    device = FlakyDevice(report_on=1)
    await connecting._open_reported(cast(bindings.NativeDevice, device), 4, ADDRESS)
    assert device.attempts == 3
    assert _pending == []


async def test_a_radio_switched_off_after_a_connection_is_reported(
    devices: None, quick: None, manager: FakeManager
) -> None:
    """
    The radio can go while a program is holding an adapter, and on macOS 26.6.2
    nothing reports that on its own -- so the state is read again at every open
    rather than settled once and assumed.
    """
    async with native.open_channel(ADDRESS, channel=4):
        pass

    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        async with native.open_channel(ADDRESS, channel=4):
            pass
    assert raised.value.reason is Availability.SWITCHED_OFF

    manager.state = CBManagerState.POWERED_ON
    async with native.open_channel(ADDRESS, channel=4):
        pass


async def test_a_radio_switched_off_does_not_strand_the_device_claim(
    devices: None, quick: None, manager: FakeManager
) -> None:
    """
    Refusing an open before the claim is taken must leave nothing behind.
    """
    async with native.open_channel(ADDRESS, channel=4):
        pass

    manager.state = CBManagerState.POWERED_OFF
    with pytest.raises(BluetoothUnavailableError) as raised:
        async with native.open_channel(ADDRESS, channel=4):
            pass
    assert raised.value.reason is Availability.SWITCHED_OFF
    assert connecting._in_use == set()


# --------------------------------------------------------------------------
# Finding a service in the stored records
# --------------------------------------------------------------------------

SERIAL_PORT = UUID("00001101-0000-1000-8000-00805f9b34fb")
GENERIC_AUDIO = UUID("00001203-0000-1000-8000-00805f9b34fb")
HANDSFREE_GATEWAY = UUID("0000111f-0000-1000-8000-00805f9b34fb")
PYBRICKS = UUID("c5f50101-8280-46da-89f4-6d8051e4aeef")


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> FakeDevice:
    """
    A paired device whose stored record is a Pybricks EV3's: its own service
    class on channel 1, naming Serial Port only as the profile it follows.

    Returns:
        The device, connected.
    """
    fake_framework_parts(monkeypatch)
    monkeypatch.setattr(connecting, "_OPEN_REPORT_SECONDS", 0.05)
    monkeypatch.setattr(
        "aio_rfcomm.backend.iobluetooth.helper.native.channel._CLOSE_TIMEOUT", 0.05
    )
    monkeypatch.setattr(hooks, "open_results", {})
    monkeypatch.setattr(FakeDeviceClass, "paired", [ADDRESS])
    device = cast(FakeDevice, FakeDeviceClass.deviceWithAddressString(ADDRESS))
    device.services = [stored_record(PYBRICKS, channel=1, profile=0x1101)]
    return device


def test_a_service_named_only_as_a_profile_is_not_offered(stored: FakeDevice) -> None:
    """
    The framework's own getServiceRecordForUUID finds the EV3 offering Serial
    Port, because it matches a UUID anywhere in the record.
    """
    assert native.list_known_devices(SERIAL_PORT) == []


def test_a_device_is_listed_by_its_service_class(stored: FakeDevice) -> None:
    [found] = native.list_known_devices([SERIAL_PORT, PYBRICKS])
    assert found.address == ADDRESS


def test_every_class_of_a_record_naming_several_is_offered(
    stored: FakeDevice,
) -> None:
    stored.services = [stored_record([HANDSFREE_GATEWAY, GENERIC_AUDIO], channel=13)]
    assert native.list_known_devices(GENERIC_AUDIO) != []


async def test_a_service_opens_on_the_channel_its_class_names(
    stored: FakeDevice,
) -> None:
    async with native.open_channel(ADDRESS, service=PYBRICKS):
        assert stored.channels[-1].number == 1


async def test_a_service_named_only_as_a_profile_cannot_be_opened(
    stored: FakeDevice,
) -> None:
    with pytest.raises(ServiceNotFoundError, match="does not offer"):
        async with native.open_channel(ADDRESS, service=SERIAL_PORT):
            pass


async def test_a_refused_stored_channel_may_be_out_of_date(stored: FakeDevice) -> None:
    """
    Measured on macOS 26.6.2 against BlueZ 5.85: a service that moved channel
    while the link was up is still stored on the old one, and the device
    refuses that. Only a link brought up afresh reads the services again.
    """
    stored.refuses.add(1)
    with pytest.raises(ServiceNotFoundError, match="may be out of date"):
        async with native.open_channel(ADDRESS, service=PYBRICKS):
            pass


async def test_a_refusal_just_after_connecting_is_not_blamed_on_the_copy(
    stored: FakeDevice,
) -> None:
    """
    Connecting is what refreshes the copy, so it is current.
    """
    stored.connected = False
    stored.refuses.add(1)
    with pytest.raises(ServiceNotFoundError, match="refused channel 1") as raised:
        async with native.open_channel(ADDRESS, service=PYBRICKS):
            pass
    assert "out of date" not in str(raised.value)
