# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The channel every backend shares, over a real socket pair.

A connected socket is what every platform ends up with, so these run the
shared channel exactly as a backend does, with an ordinary socket pair in
place of the Bluetooth one.
"""

import asyncio
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from typing_extensions import override

from aio_rfcomm.backend.stream import StreamChannel, StreamProtocol, open_stream
from aio_rfcomm.errors import ChannelClosedError

# On Windows the socket pair is two TCP sockets over loopback, which take a
# write after the far end has closed and swallow 4 MiB at once. Measured on
# Windows 10.0.26200.9457. Neither is what RFCOMM does, so the tests that rest
# on it say nothing there.
_TCP_PAIR = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows makes the socket pair from TCP"
)


@asynccontextmanager
async def _channel() -> AsyncGenerator[tuple[StreamChannel, socket.socket], None]:
    """
    A stream channel on one end of a socket pair.

    Yields:
        The channel, and the far end of the pair, playing the peer.
    """
    ours, theirs = socket.socketpair()
    theirs.setblocking(False)
    transport, protocol = await open_stream(ours)
    channel = StreamChannel(transport, protocol)
    try:
        yield channel, theirs
    finally:
        theirs.close()
        await channel.close()


class _Transport(asyncio.Transport):
    """
    Stands in for the event loop's transport, for the cases a socket pair
    cannot produce on demand.
    """

    def __init__(self) -> None:
        super().__init__()
        self.paused = False
        self.written: list[bytes] = []

    @override
    def write(self, data: bytes | bytearray | memoryview) -> None:
        self.written.append(bytes(data))

    @override
    def is_closing(self) -> bool:
        return False

    @override
    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        self.protocol = protocol

    @override
    def abort(self) -> None:
        # What the loop's transport does: the protocol hears the connection
        # is gone, later.
        asyncio.get_running_loop().call_soon(self.protocol.connection_lost, None)

    @override
    def pause_reading(self) -> None:
        self.paused = True

    @override
    def resume_reading(self) -> None:
        self.paused = False


def _driven() -> tuple[StreamChannel, StreamProtocol, _Transport]:
    transport = _Transport()
    protocol = StreamProtocol()
    transport.set_protocol(protocol)
    protocol.connection_made(transport)
    return StreamChannel(transport, protocol), protocol, transport


async def test_bytes_cross_both_ways() -> None:
    async with _channel() as (channel, peer):
        await channel.send(b"hello")
        assert peer.recv(100) == b"hello"

        peer.send(b"back")
        async with asyncio.timeout(1):
            assert await channel.receive() == b"back"


async def test_a_receive_takes_no_more_than_it_is_allowed() -> None:
    async with _channel() as (channel, peer):
        peer.send(b"abcdef")
        async with asyncio.timeout(1):
            assert await channel.receive(4) == b"abcd"
            assert await channel.receive(4) == b"ef"


async def test_what_arrived_before_a_hang_up_comes_out_first() -> None:
    async with _channel() as (channel, peer):
        peer.send(b"last words")
        peer.close()
        async with asyncio.timeout(1):
            assert await channel.receive() == b"last words"
            with pytest.raises(ChannelClosedError):
                await channel.receive()


async def test_the_end_is_raised_every_time_it_is_asked() -> None:
    async with _channel() as (channel, peer):
        peer.close()
        async with asyncio.timeout(1):
            with pytest.raises(ChannelClosedError):
                await channel.receive()
            with pytest.raises(ChannelClosedError):
                await channel.receive()
            with pytest.raises(ChannelClosedError):
                await channel.send(b"too late")


@_TCP_PAIR
async def test_a_send_straight_after_the_peer_has_gone_is_not_a_success() -> None:
    """
    The write itself fails, before the loop has heard the connection is lost.
    """
    async with _channel() as (channel, peer):
        peer.close()
        with pytest.raises(ChannelClosedError):
            await channel.send(b"into the void")


async def test_closing_lets_what_was_sent_go_first() -> None:
    """
    Data still queued when the block ends goes out before the socket closes,
    rather than being dropped with it.
    """
    ours, theirs = socket.socketpair()
    theirs.setblocking(False)
    transport, protocol = await open_stream(ours)
    channel = StreamChannel(transport, protocol)
    payload = bytes(range(256)) * 4096

    # Written without waiting for room, so most of it is still queued here
    # when the close starts.
    transport.write(payload)
    closing = asyncio.ensure_future(channel.close())

    received = bytearray()
    async with asyncio.timeout(5):
        while len(received) < len(payload):
            try:
                received += theirs.recv(65536)
            except BlockingIOError:
                await asyncio.sleep(0.001)
        await closing
    theirs.close()
    assert bytes(received) == payload


async def test_a_reset_does_not_take_what_had_already_arrived() -> None:
    """
    RFCOMM reports a peer's orderly hang-up as a reset, at least to the side
    that accepted the connection, which asyncio's own stream reader answers
    by throwing away what it holds.
    """
    channel, protocol, _transport = _driven()
    protocol.data_received(b"goodbye")
    protocol.connection_lost(ConnectionResetError())

    assert await channel.receive() == b"goodbye"
    with pytest.raises(ChannelClosedError):
        await channel.receive()


async def test_the_end_is_settled_once() -> None:
    """
    A backend may have to wait before the end is settled -- macOS waits for
    its helper -- and a sender and a receiver noticing at once must not settle
    it twice.
    """
    settled: list[None] = []
    answer = asyncio.Event()

    class Settling(StreamChannel):
        @override
        async def settle_end(self) -> None:
            settled.append(None)
            await answer.wait()

    transport = _Transport()
    protocol = StreamProtocol()
    protocol.connection_made(transport)
    channel = Settling(transport, protocol)
    protocol.connection_lost(None)

    receiving = asyncio.ensure_future(channel.receive())
    sending = asyncio.ensure_future(channel.send(b"x"))
    await asyncio.sleep(0)
    answer.set()

    first, second = await asyncio.gather(receiving, sending, return_exceptions=True)
    assert isinstance(first, ChannelClosedError)
    assert isinstance(second, ChannelClosedError)
    assert len(settled) == 1


async def test_a_canceled_receive_loses_nothing() -> None:
    async with _channel() as (channel, peer):
        waiting = asyncio.ensure_future(channel.receive())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

        peer.send(b"still here")
        async with asyncio.timeout(1):
            assert await channel.receive() == b"still here"


async def test_data_that_arrives_as_a_receive_is_canceled_survives() -> None:
    """
    The receive has been woken by the data but not yet run when the cancel
    lands. Nothing may have been taken by then.
    """
    channel, protocol, _transport = _driven()
    waiting = asyncio.ensure_future(channel.receive())
    await asyncio.sleep(0)
    protocol.data_received(b"arrived as it gave up")
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert await channel.receive() == b"arrived as it gave up"


async def test_a_full_buffer_stops_reading_until_it_is_taken() -> None:
    """
    Unread data must not grow without bound: past a point the rest is left to
    the socket's own buffer, and the peer's flow control.
    """
    channel, protocol, transport = _driven()
    protocol.data_received(bytes(100 * 1024))
    assert transport.paused

    await channel.receive(90 * 1024)
    assert not transport.paused


@_TCP_PAIR
async def test_a_canceled_send_still_sends_everything() -> None:
    data = bytes(range(256)) * 16 * 1024
    async with _channel() as (channel, theirs):
        sending = asyncio.ensure_future(channel.send(data))
        await asyncio.sleep(0)
        assert not sending.done(), "the pair's buffers should have filled"
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending

        received = bytearray()
        loop = asyncio.get_running_loop()
        while len(received) < len(data):
            received += await loop.sock_recv(theirs, 65536)
        assert received == data


async def test_a_sender_waiting_for_room_hears_the_end() -> None:
    channel, protocol, transport = _driven()
    protocol.pause_writing()
    sending = asyncio.ensure_future(channel.send(b"stuck"))
    await asyncio.sleep(0)
    assert not sending.done()

    protocol.connection_lost(None)
    with pytest.raises(ChannelClosedError):
        await sending
    assert transport.written == [b"stuck"]


async def test_an_aborted_channel_still_hands_over_what_arrived() -> None:
    channel, protocol, _transport = _driven()
    protocol.data_received(b"before the radio went")
    channel.abort()

    assert await channel.receive() == b"before the radio went"
    with pytest.raises(ChannelClosedError):
        await channel.receive()


async def test_an_abort_wakes_a_waiting_receive() -> None:
    async with _channel() as (channel, _peer):
        receiving = asyncio.ensure_future(channel.receive())
        await asyncio.sleep(0)
        channel.abort()
        with pytest.raises(ChannelClosedError):
            async with asyncio.timeout(1):
                await receiving
