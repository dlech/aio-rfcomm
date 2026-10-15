# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
A channel over a connected socket, which is what every backend ends up with.

The platforms differ in how a connected socket is obtained -- BlueZ hands one
over D-Bus, macOS passes one back from its helper, Windows connects one -- but
once there is one, reading and writing it is the same work everywhere, so it
is written once here.

What does differ is how the end of the stream is explained. The socket says
the stream is over but not why, and a platform that can tell a peer hanging up
from a link dropping says so by overriding :meth:`StreamChannel.explain_end`.

The channel keeps its own receive buffer rather than using asyncio's
``StreamReader``, which discards what it holds as soon as the transport
reports an error. RFCOMM reports a peer's orderly hang-up as exactly that, so
the peer's last words would be lost.
"""

from __future__ import annotations

import asyncio
import socket
from typing import cast

from typing_extensions import override

from aio_rfcomm.backend.provider import BackendChannel
from aio_rfcomm.errors import ChannelClosedError, CloseReason

__all__ = ["StreamChannel", "StreamProtocol", "open_stream"]

# How much may arrive unread before reading from the socket pauses, leaving the
# rest to the kernel's buffer and the peer's flow control. asyncio's default.
_HIGH_WATER = 64 * 1024


class StreamProtocol(asyncio.Protocol):
    """
    Keeps what the transport reports until the channel asks for it.

    Nothing here is lost when the connection ends: bytes that arrived before
    the end stay in :attr:`buffer` until they are taken.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        """
        Bytes that have arrived and not been taken.
        """

        self.ended = False
        """
        Whether the stream is over: the peer closed it, or the connection
        failed. Bytes may still be waiting in :attr:`buffer`.
        """

        self._transport: asyncio.Transport | None = None
        self._paused = False
        self._arrived = asyncio.Event()
        self._writable = asyncio.Event()
        self._writable.set()
        self._closed = asyncio.Event()

    @override
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.Transport, transport)

    @override
    def data_received(self, data: bytes) -> None:
        self.buffer += data
        self._arrived.set()
        if len(self.buffer) > _HIGH_WATER and self._transport is not None:
            self._transport.pause_reading()
            self._paused = True

    @override
    def eof_received(self) -> bool | None:
        self._end()
        # Not keeping the connection half open: RFCOMM has no half close.
        return None

    @override
    def connection_lost(self, exc: Exception | None) -> None:
        # The cause is not kept. RFCOMM reports a peer's orderly hang-up as a
        # reset, at least to the side that accepted the connection -- measured
        # against BlueZ, where a macOS peer leaving its ``async with`` block
        # arrives as ECONNRESET -- so a failure here says nothing reliable
        # about why, and the backend is asked instead.
        self._end()
        self._closed.set()

    @override
    def pause_writing(self) -> None:
        self._writable.clear()

    @override
    def resume_writing(self) -> None:
        self._writable.set()

    async def wait_for_data(self) -> None:
        """
        Wait until there is something to take, or the stream is over.
        """
        while not self.buffer and not self.ended:
            self._arrived.clear()
            await self._arrived.wait()

    async def wait_until_writable(self) -> None:
        """
        Wait until the transport will take more, or the stream is over.
        """
        await self._writable.wait()

    async def wait_until_closed(self) -> None:
        """
        Wait until the transport has let go of the socket.
        """
        await self._closed.wait()

    def take(self, max_bytes: int | None) -> bytes:
        """
        Take bytes from the front of the buffer.

        Args:
            max_bytes: At most this many. ``None`` for all of them.

        Returns:
            What was taken, which is empty only if the buffer was.
        """
        count = len(self.buffer) if max_bytes is None else max_bytes
        taken = bytes(self.buffer[:count])
        del self.buffer[:count]
        if self._paused and len(self.buffer) <= _HIGH_WATER // 2:
            assert self._transport is not None
            self._transport.resume_reading()
            self._paused = False
        return taken

    def _end(self) -> None:
        self.ended = True
        self._arrived.set()
        # A sender waiting for room would otherwise wait for ever.
        self._writable.set()


async def open_stream(sock: socket.socket) -> tuple[asyncio.Transport, StreamProtocol]:
    """
    Start carrying a connected socket.

    Args:
        sock: The connected socket. The transport takes it over.

    Returns:
        The transport, which closes the socket when it is closed, and what it
        reports to.
    """
    try:
        transport, protocol = await asyncio.get_running_loop().create_connection(
            StreamProtocol, sock=sock
        )
    except BaseException:
        # Only once there is a transport does it own the socket.
        sock.close()
        raise
    return transport, protocol


class StreamChannel(BackendChannel):
    """
    An RFCOMM channel carried over an already-connected socket.
    """

    def __init__(self, transport: asyncio.Transport, protocol: StreamProtocol) -> None:
        """
        Args:
            transport: Carries the connected socket.
            protocol: What the transport reports to.
        """
        super().__init__()
        self._transport = transport
        self._protocol = protocol
        self._explaining = asyncio.Lock()

    @property
    @override
    def ended(self) -> bool:
        return self._protocol.ended or self._transport.is_closing()

    @override
    async def send(self, data: bytes) -> None:
        if self.ended:
            raise await self._end_error()
        self._transport.write(data)
        await self._protocol.wait_until_writable()
        # A write that failed at once leaves the transport closing before the
        # loop has said the connection is lost.
        if self.ended:
            raise await self._end_error()

    @override
    async def receive(self, max_bytes: int | None = None) -> bytes:
        await self._protocol.wait_for_data()
        if not self._protocol.buffer:
            raise await self._end_error()
        return self._protocol.take(max_bytes)

    async def explain_end(self) -> CloseReason:
        """
        Say why the stream ended.

        The socket itself cannot tell an orderly hang-up from a lost link, so
        this assumes the kinder of the two. Backends that know better override
        it.

        Returns:
            Why the channel went away.
        """
        return CloseReason.PEER_CLOSED

    async def close(self) -> None:
        """
        Close the channel, letting what was sent go first.

        Waits for as long as the peer takes to accept what is still queued,
        with no limit of its own; cancelled, it drops the rest and closes at
        once.
        """
        self._transport.close()
        try:
            await self._protocol.wait_until_closed()
        except asyncio.CancelledError:
            self._transport.abort()
            raise

    async def _end_error(self) -> ChannelClosedError:
        """
        Build the error every call raises once the stream is over.

        The backend is asked why only once, even by a sender and a receiver
        that notice the end together, and every call reports that answer.

        Returns:
            The error to raise.
        """
        async with self._explaining:
            if self.reason is None:
                self._mark_gone(await self.explain_end())
        assert self.reason is not None
        return ChannelClosedError(self.reason)
