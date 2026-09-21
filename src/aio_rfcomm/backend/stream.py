# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
A channel over a file descriptor, which is what every backend ends up with.

The platforms differ in how a connected descriptor is obtained -- BlueZ hands
one over D-Bus, macOS passes one back from its helper, Windows connects a
socket -- but once there is one, reading and writing it is the same work
everywhere, so it is written once here.

What does differ is how the end of the stream is explained. Reading zero bytes
says the stream is over but not why, and a platform that can tell a peer
hanging up from a link dropping says so by overriding
:meth:`StreamChannel.explain_end`.
"""

from __future__ import annotations

import asyncio
import errno

from typing_extensions import override

from aio_rfcomm.backend.provider import BackendChannel
from aio_rfcomm.errors import ChannelClosedError, CloseReason

__all__ = ["StreamChannel"]

_DEFAULT_READ_BYTES = 4096


class StreamChannel(BackendChannel):
    """
    An RFCOMM channel carried over an already-connected descriptor.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        Args:
            reader: Reads from the connected descriptor.
            writer: Writes to it.
        """
        super().__init__()
        self._reader = reader
        self._writer = writer

    @override
    async def send(self, data: bytes) -> None:
        try:
            self._writer.write(data)
            await self._writer.drain()
        except OSError as error:
            raise await self._closed_error() from error

    @override
    async def receive(self, max_bytes: int | None = None) -> bytes:
        try:
            data = await self._reader.read(
                max_bytes if max_bytes is not None else _DEFAULT_READ_BYTES
            )
        except OSError as error:
            if error.errno != errno.ECONNRESET:
                raise await self._closed_error() from error
            # RFCOMM reports a peer's orderly disconnect as a reset rather
            # than as an end of stream, at least to the side that accepted
            # the connection -- measured against BlueZ, where a macOS peer
            # leaving its ``async with`` block arrives here as ECONNRESET.
            # Treating it as a failure would make every ordinary hang-up
            # raise, so it is the same event as a read of nothing and is
            # explained the same way.
            data = b""

        if not data:
            reason = await self.explain_end()
            self._mark_gone(reason)
            # A peer hanging up ends the stream rather than failing it, which
            # is the one close a reader is expected to handle inline. Anything
            # else is a failure and is raised as one.
            if reason is not CloseReason.PEER_CLOSED:
                raise ChannelClosedError(reason)
        return data

    async def explain_end(self) -> CloseReason:
        """
        Say why the stream ended.

        The descriptor itself cannot tell an orderly hang-up from a lost link,
        so this assumes the kinder of the two. Backends that know better
        override it.

        Returns:
            Why the channel went away.
        """
        return CloseReason.PEER_CLOSED

    async def _closed_error(self) -> ChannelClosedError:
        """
        Turn a failed read or write into the end of the channel.

        No bare :class:`OSError` should reach the caller, and the backend is
        asked why rather than told, so that a platform which can tell a
        hang-up from a lost link says which it was.

        Returns:
            The error to raise instead.
        """
        reason = await self.explain_end()
        self._mark_gone(reason)
        return ChannelClosedError(reason)
