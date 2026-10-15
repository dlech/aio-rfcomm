# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Serving, like a listening socket: each peer arrives as a request the program
accepts or declines, in task groups of its own.
"""

import asyncio
import gc
from uuid import UUID

import pytest

from aio_rfcomm.availability import Availability
from aio_rfcomm.bluetooth import Bluetooth
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelInUseError,
    ScopeClosedError,
)
from aio_rfcomm.service import ConnectionRequests
from tests.fake import DEVICE, FakeProvider, FakeService

CHAT = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")


def _published(provider: FakeProvider) -> FakeService:
    return provider.backend.services[-1]


async def test_the_service_reports_what_was_published(
    bluetooth: Bluetooth,
) -> None:
    async with bluetooth.serve(CHAT, name="chat", channel=7) as service:
        assert service.service == CHAT
        assert service.name == "chat"
        assert service.channel == 7


async def test_a_channel_is_chosen_when_none_is_asked_for(
    bluetooth: Bluetooth,
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        assert 1 <= service.channel <= 30


async def test_a_taken_channel_is_refused(bluetooth: Bluetooth) -> None:
    async with bluetooth.serve(CHAT, name="chat", channel=7):
        with pytest.raises(ChannelInUseError):
            async with bluetooth.serve(CHAT, name="other", channel=7):
                pass


@pytest.mark.parametrize("channel", [0, 31])
async def test_a_channel_number_out_of_range_is_refused(
    bluetooth: Bluetooth, channel: int
) -> None:
    with pytest.raises(ValueError, match="not an RFCOMM channel"):
        bluetooth.serve(CHAT, name="chat", channel=channel)


async def test_the_service_is_withdrawn_on_the_way_out(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat"):
        assert not _published(provider).withdrawn
    assert _published(provider).withdrawn


async def test_an_accepted_request_is_a_channel(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())
        assert request.info is DEVICE

        async with request.accept() as channel:
            fake.deliver(b"hello")
            assert await channel.receive() == b"hello"
            await channel.send(b"hi")
            assert fake.sent == b"hi"
        assert fake.closed


async def test_each_request_goes_to_one_reader(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        first = asyncio.ensure_future(anext(service.connection_requests()))
        second = asyncio.ensure_future(anext(service.connection_requests()))
        await asyncio.sleep(0)

        _published(provider).connect()
        await asyncio.sleep(0)
        assert first.done() != second.done()

        _published(provider).connect()
        async with asyncio.timeout(1):
            for request in (await first, await second):
                await request.decline()


async def test_declining_hangs_up(bluetooth: Bluetooth, provider: FakeProvider) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())
        await request.decline()
        assert fake.closed

        # Twice is harmless; taking it after all is not.
        await request.decline()
        with pytest.raises(RuntimeError, match="declined"):
            request.accept()


async def test_a_request_cannot_be_declined_once_accepted(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        _published(provider).connect()
        request = await anext(service.connection_requests())
        async with request.accept():
            with pytest.raises(RuntimeError, match="accepted"):
                await request.decline()
            with pytest.raises(RuntimeError, match="accepted"):
                request.accept()


async def _take_and_drop(requests: ConnectionRequests) -> None:
    # Dropped as this returns, the way a request nobody kept is.
    await anext(requests)
    gc.collect()


async def test_a_forgotten_request_is_hung_up_on_with_a_warning(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        requests = service.connection_requests()

        with pytest.warns(ResourceWarning, match="neither accepted nor declined"):
            await _take_and_drop(requests)
        assert fake.closed


async def test_requests_left_unanswered_are_closed_with_the_service(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())
    assert fake.closed

    # Answered by the service's exit, so nothing is left to warn about.
    del request
    gc.collect()


async def test_an_accepted_channel_outlives_the_service(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    """
    Serving can stop while a conversation finishes, as with a listening
    socket.
    """
    async with asyncio.TaskGroup() as group:
        talked = asyncio.Event()
        finish = asyncio.Event()

        async with bluetooth.serve(CHAT, name="chat") as service:
            fake = _published(provider).connect()
            request = await anext(service.connection_requests())

            async def talk() -> None:
                async with request.accept() as channel:
                    talked.set()
                    await finish.wait()
                    await channel.send(b"still here")

            group.create_task(talk())
            await talked.wait()

        assert _published(provider).withdrawn
        finish.set()

    assert fake.sent == b"still here"


async def test_a_request_is_answered_inside_the_service_s_block(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        _published(provider).connect()
        request = await anext(service.connection_requests())
    with pytest.raises(ScopeClosedError):
        request.accept()


async def test_requests_raise_once_bluetooth_goes(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        requests = service.connection_requests()
        waiting = asyncio.ensure_future(anext(requests))
        await asyncio.sleep(0)

        provider.report(Availability.SWITCHED_OFF)
        with pytest.raises(BluetoothUnavailableError) as first:
            async with asyncio.timeout(1):
                await waiting

        # Ended, not paused: Bluetooth coming back does not bring it back.
        provider.report(Availability.USABLE)
        with pytest.raises(BluetoothUnavailableError) as second:
            await anext(requests)

    assert first.value.reason is second.value.reason is Availability.SWITCHED_OFF


async def test_an_accepted_channel_ends_when_bluetooth_goes(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        _published(provider).connect()
        request = await anext(service.connection_requests())
        async with request.accept() as channel:
            provider.report(Availability.NOT_ALLOWED)
            with pytest.raises(BluetoothUnavailableError) as caught:
                await channel.receive()
    assert caught.value.reason is Availability.NOT_ALLOWED


async def test_accepting_while_bluetooth_is_unusable_is_refused(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())
        provider.report(Availability.SWITCHED_OFF)
        with pytest.raises(BluetoothUnavailableError):
            async with request.accept():
                pass
    assert fake.closed


async def test_an_accept_that_fails_still_hangs_up(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    """
    The request is answered by then, so nothing else would ever close it.
    """
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())
        provider.report(Availability.SWITCHED_OFF)
        provider.report(Availability.USABLE)

        with pytest.raises(BluetoothUnavailableError):
            async with request.accept():
                pass
        assert fake.closed


async def test_an_accept_never_entered_is_hung_up_on_when_dropped(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())
        request.accept()
        del request
        gc.collect()
        assert fake.closed


async def test_requests_waiting_when_bluetooth_goes_are_hung_up_on(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    """
    They arrived over links that went with the radio, so accepting one after
    Bluetooth comes back would hand over a dead connection.
    """
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())

        provider.report(Availability.SERVICE_UNAVAILABLE)
        assert fake.closed

        provider.report(Availability.USABLE)
        with pytest.raises(BluetoothUnavailableError) as caught:
            async with request.accept():
                pass
        assert caught.value.reason is Availability.SERVICE_UNAVAILABLE


async def test_declining_a_request_bluetooth_ended_does_nothing(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        _published(provider).connect()
        request = await anext(service.connection_requests())
        provider.report(Availability.SWITCHED_OFF)
        await request.decline()


async def test_an_accept_entered_after_the_service_s_block_refuses(
    bluetooth: Bluetooth, provider: FakeProvider
) -> None:
    async with bluetooth.serve(CHAT, name="chat") as service:
        fake = _published(provider).connect()
        request = await anext(service.connection_requests())
        accepting = request.accept()

    with pytest.raises(ScopeClosedError):
        async with accepting:
            pass
    assert fake.closed
