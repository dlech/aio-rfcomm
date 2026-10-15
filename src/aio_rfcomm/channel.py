# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The channel handle callers hold.
"""

import asyncio
import copy
from collections.abc import Generator
from contextlib import contextmanager

from aio_rfcomm._scope import Scope
from aio_rfcomm.backend.provider import BackendChannel
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelBrokenError,
    ChannelBusyError,
    ChannelClosedError,
    DelimiterNotFoundError,
    RfcommError,
)

__all__ = ["RfcommChannel"]


class RfcommChannel:
    """
    An open RFCOMM channel.

    A byte stream, not a message stream: nothing preserves the boundaries
    between sends. macOS can look as though it does, because data arrives in
    fixed-size pieces, but that must not be relied on.

    Every ending is raised, the peer hanging up included: once the channel is
    over, :meth:`receive` -- after handing over whatever arrived before the
    end -- and :meth:`send` raise the same error every time.
    :class:`~aio_rfcomm.errors.ChannelClosedError` means the connection
    ended; :class:`~aio_rfcomm.errors.BluetoothUnavailableError` means
    Bluetooth did.

    One task sends and one task receives. Two tasks using the same direction
    raises :class:`~aio_rfcomm.errors.ChannelBusyError`.
    """

    def __init__(self, backend: BackendChannel, scope: Scope) -> None:
        """
        Not called directly. Channels come from opening or accepting one.

        Args:
            backend: The backend-specific channel object.
            scope: The channel's scope. Using the channel outside it raises.
        """
        self._backend = backend
        self._scope = scope
        self._sending = False
        self._receiving = False
        self._send_cancelled = False
        self._ending: RfcommError | None = None
        # What was taken from the backend and not yet handed out, shared by
        # every way of receiving so that mixing them loses nothing.
        self._unread = bytearray()

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
            ChannelClosedError: The connection is over.
            BluetoothUnavailableError: Bluetooth went away, ending the channel.
            ChannelBrokenError: An earlier send on this channel was cancelled.
        """
        self._scope.check()
        if self._sending:
            raise ChannelBusyError("another task is already sending on this channel")

        # The ending, when there is one, matters more than the mistake.
        if self._send_cancelled and not self._over():
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
        except ChannelClosedError as closed:
            raise self._settle(closed) from closed
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
            ChannelClosedError: The connection is over, and everything that
                arrived before the end has been taken.
            BluetoothUnavailableError: Bluetooth went away, ending the
                channel, and everything that arrived before has been taken.
            ValueError: ``max_bytes`` is less than 1.
        """
        if max_bytes is not None and max_bytes < 1:
            raise ValueError(f"max_bytes must be at least 1, not {max_bytes}")

        with self._reading():
            if self._unread:
                return self._take(len(self._unread) if max_bytes is None else max_bytes)
            return await self._more(max_bytes)

    async def receive_exactly(self, count: int) -> bytes:
        """
        Take exactly ``count`` bytes, waiting for as many as it takes.

        Cancelling it loses nothing: what it had gathered is left for the next
        call.

        Args:
            count: How many bytes, at least 1.

        Returns:
            Exactly ``count`` bytes.

        Raises:
            ChannelClosedError: The connection ended first. Whatever had
                arrived is left for :meth:`receive`.
            BluetoothUnavailableError: Bluetooth went away first, likewise.
            ValueError: ``count`` is less than 1.
        """
        if count < 1:
            raise ValueError(f"count must be at least 1, not {count}")

        with self._reading():
            while len(self._unread) < count:
                self._unread += await self._more(None)
            return self._take(count)

    async def receive_until(self, delimiter: bytes, *, max_bytes: int) -> bytes:
        """
        Take the bytes up to a delimiter, which is taken too and not returned.

        A line is ``await channel.receive_until(b"\\n", max_bytes=1024)``.
        Cancelling it loses nothing: what it had gathered is left for the next
        call.

        Args:
            delimiter: What ends the bytes wanted. Not empty.
            max_bytes: The most to look through for it, delimiter included,
                so that a peer that never sends one cannot make this buffer
                for ever. At least as long as the delimiter.

        Returns:
            The bytes before the delimiter.

        Raises:
            DelimiterNotFoundError: No delimiter within ``max_bytes``. Nothing
                is taken.
            ChannelClosedError: The connection ended first. Whatever had
                arrived is left for :meth:`receive`.
            BluetoothUnavailableError: Bluetooth went away first, likewise.
            ValueError: ``delimiter`` is empty, or longer than ``max_bytes``.
        """
        if not delimiter:
            raise ValueError("the delimiter must not be empty")
        if max_bytes < len(delimiter):
            raise ValueError(
                f"max_bytes must be at least the delimiter's length, not {max_bytes}"
            )

        with self._reading():
            searched = 0
            # Bounded, so only a delimiter that ends within max_bytes is found.
            while (found := self._unread.find(delimiter, searched, max_bytes)) < 0:
                if len(self._unread) >= max_bytes:
                    raise DelimiterNotFoundError(
                        f"no {delimiter!r} within {max_bytes} bytes"
                    )
                # A delimiter can straddle what was there and what arrives.
                searched = max(0, len(self._unread) - len(delimiter) + 1)
                self._unread += await self._more(None)

            line = self._take(found)
            del self._unread[: len(delimiter)]
            return line

    @contextmanager
    def _reading(self) -> Generator[None, None, None]:
        """
        Hold the receiving direction for one call.

        Raises:
            ScopeClosedError: The channel's block has ended.
            ChannelBusyError: Another task is receiving.
        """
        self._scope.check()
        if self._receiving:
            raise ChannelBusyError("another task is already receiving on this channel")

        self._receiving = True
        try:
            yield
        finally:
            self._receiving = False

    async def _more(self, max_bytes: int | None) -> bytes:
        """
        Wait for the backend to hand over more.

        Raises:
            RfcommError: How the channel ended.
        """
        try:
            return await self._backend.receive(max_bytes)
        except ChannelClosedError as closed:
            raise self._settle(closed) from closed

    def _take(self, count: int) -> bytes:
        taken = bytes(self._unread[:count])
        del self._unread[:count]
        return taken

    def _over(self) -> bool:
        return self._ending is not None or self._backend.ended

    def _settle(self, closed: ChannelClosedError) -> RfcommError:
        """
        Decide, once, how the channel ended, and give out a copy each time.

        Bluetooth going away is recorded by :meth:`_end` as it happens, which
        is before the channel hears its link drop -- measured on macOS, where
        the radio is reported off a millisecond earlier -- so by then this
        keeps Bluetooth's reason rather than the connection's.

        Args:
            closed: What the backend reported.

        Returns:
            The error to raise.
        """
        if self._ending is None:
            self._ending = closed

        # A copy, so that raising it again does not pile one traceback on
        # another.
        return copy.copy(self._ending)

    def _end(self, error: BluetoothUnavailableError) -> None:
        """
        End the channel because Bluetooth has gone.

        Args:
            error: What every later call raises.
        """
        # An end the backend has already explained came first, and stays.
        if self._ending is None and self._backend.reason is None:
            self._ending = error
        self._backend.abort()

    def _close(self) -> None:
        self._scope.close()
