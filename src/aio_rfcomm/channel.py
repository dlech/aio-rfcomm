# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The channel handle callers hold.
"""

import asyncio

from aio_rfcomm._scope import Scope
from aio_rfcomm.backend.provider import BackendChannel
from aio_rfcomm.errors import ChannelBrokenError, ChannelBusyError

__all__ = ["RfcommChannel"]


class RfcommChannel:
    """
    An open RFCOMM channel.

    A byte stream, not a message stream: nothing preserves the boundaries
    between sends. macOS can look as though it does, because data arrives in
    fixed-size pieces, but that must not be relied on.

    Every ending is raised, the peer hanging up included: once the channel is
    over, :meth:`receive` -- after handing over whatever arrived before the
    end -- and :meth:`send` raise
    :class:`~aio_rfcomm.errors.ChannelClosedError`, with the same reason every
    time.

    One task sends and one task receives. Two tasks using the same direction
    raises :class:`~aio_rfcomm.errors.ChannelBusyError`.
    """

    def __init__(self, backend: BackendChannel, scope: Scope) -> None:
        """
        Not called directly. Channels come from opening one.

        Args:
            backend: The backend-specific channel object.
            scope: The channel's scope. Using the channel outside it raises.
        """
        self._backend = backend
        self._scope = scope
        self._sending = False
        self._receiving = False
        self._send_cancelled = False

    async def send(self, data: bytes) -> None:
        """
        Send all of ``data``.

        Returns once the data is on its way, which is not the same as the peer
        having it.

        If this is cancelled, whether the peer receives the data is unknown,
        so a protocol with any framing cannot trust what follows. Every later
        send raises :class:`~aio_rfcomm.errors.ChannelBrokenError` rather than
        let that go unnoticed; leave the channel's block and open a new one.
        Receiving is unaffected.

        Args:
            data: The bytes to send.

        Raises:
            ChannelClosedError: The channel is over.
            ChannelBrokenError: An earlier send on this channel was cancelled.
        """
        self._scope.check()
        if self._sending:
            raise ChannelBusyError("another task is already sending on this channel")

        # The ending, when there is one, matters more than the mistake.
        if self._send_cancelled and not self._backend.ended:
            raise ChannelBrokenError(
                "an earlier send on this channel was cancelled, so whether the "
                "peer received it is unknown; leave the channel's block and "
                "open a new one"
            )

        self._sending = True
        try:
            await self._backend.send(data)
        except asyncio.CancelledError:
            self._send_cancelled = True
            raise
        finally:
            self._sending = False

    async def receive(self, max_bytes: int | None = None) -> bytes:
        """
        Take the next bytes to arrive.

        Args:
            max_bytes: At most this many bytes. ``None`` for whatever has
                arrived.

        Returns:
            Between 1 and ``max_bytes`` bytes. Never empty: the end of the
            channel is raised, not returned.

        Raises:
            ChannelClosedError: The channel is over, and everything that
                arrived before the end has been taken.
            ValueError: ``max_bytes`` is less than 1.
        """
        if max_bytes is not None and max_bytes < 1:
            raise ValueError(f"max_bytes must be at least 1, not {max_bytes}")

        self._scope.check()
        if self._receiving:
            raise ChannelBusyError("another task is already receiving on this channel")

        self._receiving = True
        try:
            return await self._backend.receive(max_bytes)
        finally:
            self._receiving = False
