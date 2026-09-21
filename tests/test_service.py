# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Serving: what happens to a handler, and to the connections it is given.

A server differs from a client in who is in charge of a channel's lifetime.
The library opens the channel, calls the handler, and closes it again, so
these check that the handler gets what it should and that nothing is left
behind when it -- or the service -- ends.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import ChannelInUseError, ScopeClosedError
from aio_rfcomm.service import ConnectionHandler, RfcommService
from tests.fake import DEVICE, FakeAdapter, FakeService

SERVICE = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")


@asynccontextmanager
async def serving(
    backend: FakeAdapter, handler: ConnectionHandler, **kwargs: Any
) -> AsyncGenerator[tuple[RfcommService, FakeService], None]:
    """
    A published service, with the fake behind it.
    """
    adapter = RfcommAdapter(backend)
    kwargs.setdefault("name", "fake service")
    async with adapter.serve(SERVICE, handler, **kwargs) as service:
        yield service, backend.services[-1]


async def noop(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
    return None


# -- what the service says about itself ------------------------------------


async def test_the_service_reports_what_was_published(backend: FakeAdapter) -> None:
    async with serving(backend, noop, name="chat") as (service, _):
        assert service.service == SERVICE
        assert service.name == "chat"
        assert service.channel == 30


async def test_an_explicit_channel_is_used(backend: FakeAdapter) -> None:
    async with serving(backend, noop, channel=7) as (service, _):
        assert service.channel == 7


async def test_a_second_service_gets_a_different_channel(
    backend: FakeAdapter,
) -> None:
    async with serving(backend, noop) as (first, _):  # noqa: SIM117
        async with serving(backend, noop) as (second, _):
            assert first.channel != second.channel


async def test_a_taken_channel_is_refused(backend: FakeAdapter) -> None:
    async with serving(backend, noop) as (first, _):
        with pytest.raises(ChannelInUseError):
            async with serving(backend, noop, channel=first.channel):
                pass


async def test_the_service_is_withdrawn_on_the_way_out(backend: FakeAdapter) -> None:
    async with serving(backend, noop) as (_, fake):
        assert not fake.withdrawn
    assert fake.withdrawn


# -- what the handler is given ---------------------------------------------


async def test_the_handler_gets_the_peer_then_the_channel(
    backend: FakeAdapter,
) -> None:
    seen: list[tuple[RfcommDeviceInfo, RfcommChannel]] = []
    done = asyncio.Event()

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        seen.append((peer, channel))
        done.set()

    async with serving(backend, handler) as (_, fake):
        fake.connect()
        await asyncio.wait_for(done.wait(), 1)

    peer, channel = seen[0]
    assert peer == DEVICE
    assert isinstance(channel, RfcommChannel)


async def test_the_handler_can_talk_on_its_channel(backend: FakeAdapter) -> None:
    done = asyncio.Event()

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        await channel.send(b"hello")
        done.set()

    async with serving(backend, handler) as (_, fake):
        channel = fake.connect()
        await asyncio.wait_for(done.wait(), 1)
        assert bytes(channel.sent) == b"hello"


async def test_each_peer_gets_its_own_handler(backend: FakeAdapter) -> None:
    started = asyncio.Semaphore(0)

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        started.release()
        await asyncio.Event().wait()

    async with serving(backend, handler) as (_, fake):
        fake.connect()
        fake.connect()
        await asyncio.wait_for(started.acquire(), 1)
        await asyncio.wait_for(started.acquire(), 1)


# -- what happens at the end -----------------------------------------------


async def test_the_channel_closes_when_the_handler_returns(
    backend: FakeAdapter,
) -> None:
    done = asyncio.Event()

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        done.set()

    async with serving(backend, handler) as (_, fake):
        channel = fake.connect()
        await asyncio.wait_for(done.wait(), 1)
        await asyncio.sleep(0)
        assert channel.closed


async def test_the_handler_s_channel_is_dead_afterwards(backend: FakeAdapter) -> None:
    escaped: list[RfcommChannel] = []
    done = asyncio.Event()

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        escaped.append(channel)
        done.set()

    async with serving(backend, handler) as (_, fake):
        fake.connect()
        await asyncio.wait_for(done.wait(), 1)
        await asyncio.sleep(0)

    with pytest.raises(ScopeClosedError):
        await escaped[0].send(b"too late")


async def test_leaving_the_block_stops_a_handler_that_is_still_talking(
    backend: FakeAdapter,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with serving(backend, handler) as (_, fake):
        channel = fake.connect()
        await asyncio.wait_for(started.wait(), 1)

    assert cancelled.is_set()
    assert channel.closed


async def test_a_connection_nobody_took_is_still_closed(
    backend: FakeAdapter,
) -> None:
    """
    A peer can connect in the instant before the service is withdrawn. BlueZ
    has already handed over a descriptor by then, so dropping it silently
    would leak it and leave the peer holding a connection nothing will answer.
    """
    async with serving(backend, noop) as (_, fake):
        channel = fake.connect()

    assert channel not in fake.accepted
    assert channel.closed


# -- a handler that raises -------------------------------------------------


async def test_a_failing_handler_does_not_end_the_service(
    backend: FakeAdapter,
) -> None:
    reported: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: reported.append(context)
    )
    survived = asyncio.Event()

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        if not reported:
            raise ValueError("handler blew up")
        survived.set()

    try:
        async with serving(backend, handler) as (_, fake):
            fake.connect()
            for _ in range(10):
                await asyncio.sleep(0)
                if reported:
                    break
            fake.connect()
            await asyncio.wait_for(survived.wait(), 1)
    finally:
        asyncio.get_running_loop().set_exception_handler(None)

    assert len(reported) == 1
    assert isinstance(reported[0]["exception"], ValueError)
    assert reported[0]["peer"] == DEVICE.address


async def test_a_failing_handler_still_closes_its_channel(
    backend: FakeAdapter,
) -> None:
    asyncio.get_running_loop().set_exception_handler(lambda loop, context: None)

    async def handler(peer: RfcommDeviceInfo, channel: RfcommChannel) -> None:
        raise ValueError("handler blew up")

    try:
        async with serving(backend, handler) as (_, fake):
            channel = fake.connect()
            for _ in range(10):
                await asyncio.sleep(0)
                if channel.closed:
                    break
    finally:
        asyncio.get_running_loop().set_exception_handler(None)

    assert channel.closed
