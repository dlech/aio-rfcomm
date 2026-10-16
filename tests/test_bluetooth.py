# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The Bluetooth object: opening it, availability, and what Bluetooth going
away ends.
"""

import asyncio
from uuid import UUID

import pytest

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend import _Unsupported
from aio_rfcomm.backend.provider import AvailabilityState
from aio_rfcomm.bluetooth import Bluetooth, _open_bluetooth
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.errors import (
    BluetoothAlreadyOpenError,
    BluetoothUnavailableError,
    ChannelClosedError,
    CloseReason,
    ScopeClosedError,
)
from tests.fake import DEVICE, FakeChannel, FakeProvider

DEVICE_SERVICE = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")

# --------------------------------------------------------------------------
# Opening
# --------------------------------------------------------------------------


async def test_opening_waits_for_the_first_reading() -> None:
    provider = FakeProvider(first=None)
    opening = asyncio.ensure_future(_enter(provider))
    await asyncio.sleep(0.01)
    assert not opening.done()

    provider.report(Availability.SWITCHED_OFF)
    async with asyncio.timeout(1):
        assert await opening is Availability.SWITCHED_OFF


async def _enter(provider: FakeProvider) -> Availability:
    async with _open_bluetooth(provider) as bluetooth:
        return await anext(bluetooth.watch_availability())


async def test_the_first_reading_is_known_once_reported() -> None:
    state = AvailabilityState()
    assert not state.is_known

    state.set(Availability.SWITCHED_OFF)
    assert state.is_known


async def test_opening_succeeds_whatever_bluetooth_is_doing() -> None:
    async with _open_bluetooth(FakeProvider(Availability.NO_ADAPTER)) as bluetooth:
        assert await anext(bluetooth.watch_availability()) is Availability.NO_ADAPTER


async def test_a_platform_with_no_backend_has_no_bluetooth() -> None:
    async with _open_bluetooth(_Unsupported()) as bluetooth:
        assert await anext(bluetooth.watch_availability()) is Availability.UNSUPPORTED
        with pytest.raises(BluetoothUnavailableError) as caught:
            await bluetooth.list_known_devices()
        assert caught.value.reason is Availability.UNSUPPORTED


async def test_only_one_may_be_open_at_a_time(bluetooth: Bluetooth) -> None:
    with pytest.raises(BluetoothAlreadyOpenError):
        async with _open_bluetooth(FakeProvider()):
            pass


async def test_another_may_be_opened_once_the_first_is_closed() -> None:
    async with _open_bluetooth(FakeProvider()):
        pass
    async with _open_bluetooth(FakeProvider()):
        pass


# --------------------------------------------------------------------------
# Watching availability
# --------------------------------------------------------------------------


async def test_the_first_value_comes_at_once(bluetooth: Bluetooth) -> None:
    async with asyncio.timeout(1):
        assert await anext(bluetooth.watch_availability()) is Availability.USABLE


async def test_a_change_is_reported(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    watcher = bluetooth.watch_availability()
    await anext(watcher)

    waiting = asyncio.ensure_future(anext(watcher))
    await asyncio.sleep(0)
    assert not waiting.done()

    provider.report(Availability.SWITCHED_OFF)
    async with asyncio.timeout(1):
        assert await waiting is Availability.SWITCHED_OFF


async def test_a_change_and_back_between_reads_reports_nothing(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    watcher = bluetooth.watch_availability()
    await anext(watcher)
    provider.report(Availability.SERVICE_UNAVAILABLE)
    provider.report(Availability.USABLE)

    waiting = asyncio.ensure_future(anext(watcher))
    await asyncio.sleep(0.01)
    assert not waiting.done()

    provider.report(Availability.SWITCHED_OFF)
    async with asyncio.timeout(1):
        assert await waiting is Availability.SWITCHED_OFF


async def test_each_watcher_sees_every_change(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    first = bluetooth.watch_availability()
    second = bluetooth.watch_availability()
    assert await anext(first) is Availability.USABLE
    assert await anext(second) is Availability.USABLE

    provider.report(Availability.SWITCHED_OFF)
    async with asyncio.timeout(1):
        assert await anext(first) is Availability.SWITCHED_OFF
        assert await anext(second) is Availability.SWITCHED_OFF


async def test_a_watcher_left_waiting_past_the_block_is_told(
    provider: FakeProvider,
) -> None:
    async with _open_bluetooth(provider) as bluetooth:
        watcher = bluetooth.watch_availability()
        await anext(watcher)
        waiting = asyncio.ensure_future(anext(watcher))
        await asyncio.sleep(0)

    with pytest.raises(ScopeClosedError):
        async with asyncio.timeout(1):
            await waiting


# --------------------------------------------------------------------------
# Refusing while unusable
# --------------------------------------------------------------------------


async def test_nothing_is_attempted_while_bluetooth_is_unusable() -> None:
    provider = FakeProvider(Availability.SWITCHED_OFF)
    async with _open_bluetooth(provider) as bluetooth:
        with pytest.raises(BluetoothUnavailableError) as listing:
            await bluetooth.list_known_devices()
        with pytest.raises(BluetoothUnavailableError) as opening:
            async with bluetooth.open_channel(DEVICE, 1):
                pass
        with pytest.raises(BluetoothUnavailableError) as serving:
            async with bluetooth.serve(DEVICE_SERVICE, name="x"):
                pass

    assert listing.value.reason is Availability.SWITCHED_OFF
    assert opening.value.reason is Availability.SWITCHED_OFF
    assert serving.value.reason is Availability.SWITCHED_OFF
    assert provider.backend.listed == 0
    assert provider.backend.open_calls == 0
    assert provider.backend.services == []


async def test_the_platform_s_own_words_reach_the_caller() -> None:
    provider = FakeProvider(first=None)
    async with asyncio.timeout(1):
        opening = asyncio.ensure_future(_list(provider))
        await asyncio.sleep(0)
        provider.report(Availability.NOT_ALLOWED, "allow it in System Settings")
        with pytest.raises(BluetoothUnavailableError, match="System Settings"):
            await opening


async def _list(provider: FakeProvider) -> None:
    async with _open_bluetooth(provider) as bluetooth:
        await bluetooth.list_known_devices()


# --------------------------------------------------------------------------
# Bluetooth going away
# --------------------------------------------------------------------------


async def test_an_open_channel_ends_when_bluetooth_goes(
    opened: tuple[RfcommChannel, FakeChannel], provider: FakeProvider
) -> None:
    channel, fake = opened
    fake.deliver(b"before")
    provider.report(Availability.SWITCHED_OFF)

    assert fake.aborted
    assert await channel.receive() == b"before"
    with pytest.raises(BluetoothUnavailableError) as receiving:
        await channel.receive()
    with pytest.raises(BluetoothUnavailableError) as sending:
        await channel.send(b"after")

    assert receiving.value.reason is Availability.SWITCHED_OFF
    assert sending.value.reason is Availability.SWITCHED_OFF


async def test_a_waiting_receive_hears_bluetooth_go(
    opened: tuple[RfcommChannel, FakeChannel], provider: FakeProvider
) -> None:
    channel, _fake = opened
    receiving = asyncio.ensure_future(channel.receive())
    await asyncio.sleep(0)

    provider.report(Availability.SERVICE_UNAVAILABLE)
    with pytest.raises(BluetoothUnavailableError) as caught:
        async with asyncio.timeout(1):
            await receiving
    assert caught.value.reason is Availability.SERVICE_UNAVAILABLE


async def test_a_change_and_back_still_ends_the_channel(
    opened: tuple[RfcommChannel, FakeChannel], provider: FakeProvider
) -> None:
    """
    A service restart can be over before anyone looks, and the channels it
    killed on macOS are never told.
    """
    channel, _fake = opened
    provider.report(Availability.SERVICE_UNAVAILABLE)
    provider.report(Availability.USABLE)

    with pytest.raises(BluetoothUnavailableError) as caught:
        await channel.receive()
    assert caught.value.reason is Availability.SERVICE_UNAVAILABLE


async def test_an_end_heard_while_bluetooth_is_usable_is_the_connection_s(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.go_away(CloseReason.LINK_LOST)

    with pytest.raises(ChannelClosedError) as caught:
        await channel.receive()
    assert caught.value.reason is CloseReason.LINK_LOST


async def test_an_open_in_progress_ends_when_bluetooth_goes(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    provider.backend.opens_block = True

    async def open_one() -> None:
        async with bluetooth.open_channel(DEVICE, 1):
            pass

    opening = asyncio.ensure_future(open_one())
    await asyncio.sleep(0)
    provider.report(Availability.SWITCHED_OFF)

    with pytest.raises(BluetoothUnavailableError) as caught:
        async with asyncio.timeout(1):
            await opening
    assert caught.value.reason is Availability.SWITCHED_OFF


async def test_canceling_an_open_is_still_a_cancellation(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    provider.backend.opens_block = True

    async def open_one() -> None:
        async with bluetooth.open_channel(DEVICE, 1):
            pass

    opening = asyncio.ensure_future(open_one())
    await asyncio.sleep(0)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening


async def test_a_deadline_around_an_open_is_still_a_deadline(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    provider.backend.opens_block = True
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01), bluetooth.open_channel(DEVICE, 1):
            pass


# --------------------------------------------------------------------------
# Interruptions, the way asyncio.timeout behaves
# --------------------------------------------------------------------------


async def test_a_later_deadline_still_works_after_an_interrupted_open(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    """
    A cancel made to end an open, and not taken back, would turn the task's
    next deadline into a bare cancellation.
    """
    provider.backend.opens_block = True

    async def open_then_wait() -> None:
        with pytest.raises(BluetoothUnavailableError):
            async with bluetooth.open_channel(DEVICE, 1):
                pass
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await asyncio.Event().wait()

    task = asyncio.ensure_future(open_then_wait())
    await asyncio.sleep(0)
    provider.report(Availability.SWITCHED_OFF)
    async with asyncio.timeout(1):
        await task


async def test_a_failure_that_is_not_bluetooth_s_goes_on_up(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    provider.backend.opens_block = True
    provider.backend.open_cancel_raises = OSError("the platform's own trouble")

    async def open_one() -> None:
        async with bluetooth.open_channel(DEVICE, 1):
            pass

    task = asyncio.ensure_future(open_one())
    await asyncio.sleep(0)
    provider.report(Availability.SWITCHED_OFF)
    with pytest.raises(OSError, match="own trouble"):
        async with asyncio.timeout(1):
            await task
    # Taken back all the same.
    assert task.cancelling() == 0


async def test_an_open_that_finishes_as_bluetooth_goes_is_not_handed_over(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    """
    A backend that finishes an open rather than give it up leaves a channel
    nothing will ever end, since everything was ended before it was held.
    """
    provider.backend.opens_block = True
    provider.backend.open_cancel_finishes = True

    async def open_one() -> None:
        async with bluetooth.open_channel(DEVICE, 1):
            pytest.fail("handed over a channel opened as Bluetooth went")

    task = asyncio.ensure_future(open_one())
    await asyncio.sleep(0)
    provider.report(Availability.SWITCHED_OFF)
    with pytest.raises(BluetoothUnavailableError):
        async with asyncio.timeout(1):
            await task
    assert provider.backend.channels[-1].closed


async def test_reporting_from_the_task_being_interrupted_does_not_cancel_it(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    """
    BlueZ reports a radio going off from inside a channel's own receive. On
    3.11 a cancel taken back is still delivered at the next await, so the task
    reporting must not cancel itself.
    """
    with pytest.raises(BluetoothUnavailableError), bluetooth._interrupted():
        provider.report(Availability.SWITCHED_OFF)
    await asyncio.sleep(0)


async def test_a_change_between_two_unusable_reasons_keeps_the_first(
    opened: tuple[RfcommChannel, FakeChannel], provider: FakeProvider
) -> None:
    channel, _fake = opened
    provider.report(Availability.SWITCHED_OFF)
    provider.report(Availability.SERVICE_UNAVAILABLE)

    with pytest.raises(BluetoothUnavailableError) as caught:
        await channel.receive()
    assert caught.value.reason is Availability.SWITCHED_OFF


async def test_an_end_already_explained_is_kept(
    opened: tuple[RfcommChannel, FakeChannel], provider: FakeProvider
) -> None:
    """
    A peer that hung up before the radio went off hung up. The radio did not
    end that channel.
    """
    channel, fake = opened
    fake.go_away(CloseReason.PEER_CLOSED)
    provider.report(Availability.SWITCHED_OFF)

    with pytest.raises(ChannelClosedError) as caught:
        await channel.receive()
    assert caught.value.reason is CloseReason.PEER_CLOSED


async def test_an_open_made_but_entered_after_the_block_refuses(
    provider: FakeProvider,
) -> None:
    async with _open_bluetooth(provider) as bluetooth:
        opening = bluetooth.open_channel(DEVICE, 1)

    with pytest.raises(ScopeClosedError):
        async with opening:
            pass
    assert provider.backend.open_calls == 0
