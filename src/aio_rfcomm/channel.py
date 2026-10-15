# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The channel handle callers hold.
"""

import copy
from collections.abc import Generator
from contextlib import contextmanager

from aio_rfcomm._scope import Scope
from aio_rfcomm.backend.provider import BackendChannel
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelBusyError,
    ChannelClosedError,
    DelimiterNotFoundError,
    RfcommError,
)

__all__ = ["RfcommChannel"]


class RfcommChannel:
    """
    An open RFCOMM channel.

    It carries a stream of bytes, not separate messages. One send on the other end
    may arrive in several pieces, or joined to the next.

    When the connection ends, :meth:`receive` first returns any bytes that arrived
    before the end. After that, every call to :meth:`receive` or :meth:`send`
    raises the same error: :class:`~aio_rfcomm.errors.ChannelClosedError`, or
    :class:`~aio_rfcomm.errors.BluetoothUnavailableError` if Bluetooth stopped
    being usable.

    One task can send while another receives. Two tasks receiving at once, or two
    sending, raises :class:`~aio_rfcomm.errors.ChannelBusyError`.
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
        self._ending: RfcommError | None = None
        # What was taken from the backend and not yet handed out, shared by
        # every way of receiving so that mixing them loses nothing.
        self._unread = bytearray()

    async def send(self, data: bytes) -> None:
        """
        Send all of ``data``.

        It returns once the data is on its way, which does not mean the other device
        has it yet. If it is cancelled, the data is still sent, unless the connection
        ends first.

        Args:
            data: The bytes to send.

        Raises:
            ChannelClosedError: The connection has ended.
            BluetoothUnavailableError: Bluetooth stopped being usable.
        """
        self._scope.check()
        if self._sending:
            raise ChannelBusyError("another task is already sending on this channel")

        self._sending = True
        try:
            await self._backend.send(data)
        except ChannelClosedError as closed:
            raise self._settle(closed) from closed
        finally:
            self._sending = False

    async def receive(self, max_bytes: int | None = None) -> bytes:
        """
        Wait for bytes to arrive and return them.

        Args:
            max_bytes: Return at most this many bytes. ``None`` returns everything
                that has arrived. It does not wait for this many.

        Returns:
            At least one byte, and at most ``max_bytes``.

        Raises:
            ChannelClosedError: The connection has ended, and every byte that arrived
                before the end has been returned.
            BluetoothUnavailableError: Bluetooth stopped being usable, and every byte
                that arrived before then has been returned.
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
        Wait for exactly ``count`` bytes and return them.

        If it is cancelled, the bytes it has read so far are kept for the next call.

        Args:
            count: How many bytes, at least 1.

        Returns:
            Exactly ``count`` bytes.

        Raises:
            ChannelClosedError: The connection ended first. :meth:`receive` returns
                the bytes that did arrive.
            BluetoothUnavailableError: Bluetooth stopped being usable first.
                :meth:`receive` returns the bytes that did arrive.
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
        Read up to a delimiter, and return the bytes before it.

        The delimiter is read too, but not returned. To read a line::

            line = await channel.receive_until(b"\n", max_bytes=1024)

        If it is cancelled, the bytes it has read so far are kept for the next call.

        Args:
            delimiter: The bytes that end each message. Not empty.
            max_bytes: The most to read, delimiter included. This stops a device that
                never sends a delimiter from using up memory. At least the length of
                the delimiter.

        Returns:
            The bytes before the delimiter.

        Raises:
            DelimiterNotFoundError: No delimiter arrived within ``max_bytes``. The
                bytes are left for the next call.
            ChannelClosedError: The connection ended first. :meth:`receive` returns
                the bytes that did arrive.
            BluetoothUnavailableError: Bluetooth stopped being usable first.
                :meth:`receive` returns the bytes that did arrive.
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
