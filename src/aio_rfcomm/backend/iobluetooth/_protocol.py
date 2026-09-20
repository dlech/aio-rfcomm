# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The control protocol between this library and its macOS helper process.

Both ends speak the same framing over one ``AF_UNIX`` stream socket: a
four-byte length and then a JSON object. Bulk data never travels here -- each
open channel gets a socket of its own, whose descriptor is passed over this
one -- so frames stay small and infrequent.

The library end of the socket is also the helper's watchdog. macOS has no
``PDEATHSIG``, so a helper whose parent has gone learns about it by reading
end of file here, and exits.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from collections import deque
from typing import Any

__all__ = ["MAX_MESSAGE", "Link"]

_HEADER = struct.Struct("!I")

MAX_MESSAGE = 1 << 16
"""
Longest JSON frame either end will send or accept.

A frame carries a request, a reply or an event, never payload bytes, so this
is generous. Refusing anything larger keeps a confused or hostile peer from
making us allocate without bound.
"""

_MAX_FDS = 4


class ProtocolError(Exception):
    """
    The peer sent something that is not this protocol.

    Not part of the public error hierarchy: it means the helper and the
    library disagree about the protocol, which is a bug in this package rather
    than anything a caller can act on.
    """


class Link:
    """
    One end of the control socket.

    Reads are driven by the running loop, so frames arrive whether or not
    anyone is waiting for them, and are queued until someone is. Writes go out
    immediately when the socket will take them and are queued behind a writer
    callback when it will not.
    """

    def __init__(self, sock: socket.socket) -> None:
        """
        Args:
            sock: The connected ``AF_UNIX`` socket. Taken over, including
                closing it.
        """
        sock.setblocking(False)
        self._sock = sock
        self._loop = asyncio.get_running_loop()
        self._arrived: asyncio.Queue[tuple[dict[str, Any], list[int]] | None] = (
            asyncio.Queue()
        )
        self._buffer = bytearray()
        self._carried: list[int] = []
        self._waiting: deque[tuple[bytes, list[int]]] = deque()
        self._sending = False
        self._closed = False
        self._loop.add_reader(sock.fileno(), self._readable)

    def send(self, message: dict[str, Any], fds: list[int] | None = None) -> None:
        """
        Send one frame.

        Args:
            message: The JSON object to send.
            fds: Descriptors to pass alongside it. They are duplicated here,
                so the caller keeps ownership of its own and may close them as
                soon as this returns -- which matters because the frame may
                not reach the socket until later.

        Raises:
            ProtocolError: The message is too large to send.
        """
        body = json.dumps(message).encode()
        if _HEADER.size + len(body) > MAX_MESSAGE:
            raise ProtocolError(f"message of {len(body)} bytes is too large to send")

        self._waiting.append(
            (_HEADER.pack(len(body)) + body, [os.dup(fd) for fd in fds or ()])
        )
        self._flush()

    async def receive(self) -> tuple[dict[str, Any], list[int]] | None:
        """
        Take the next frame to arrive.

        Returns:
            The message and any descriptors that came with it, or ``None``
            once the peer has gone.
        """
        return await self._arrived.get()

    def close(self) -> None:
        """
        Close this end, discarding anything not yet sent.

        Descriptors still queued for sending are closed, since nothing else
        will.
        """
        if self._closed:
            return
        self._closed = True
        self._loop.remove_reader(self._sock.fileno())
        if self._sending:
            self._loop.remove_writer(self._sock.fileno())
            self._sending = False
        for _frame, fds in self._waiting:
            for fd in fds:
                _close(fd)
        self._waiting.clear()
        self._sock.close()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """
        Push as much of the queue into the socket as it will take.
        """
        while self._waiting and not self._closed:
            frame, fds = self._waiting[0]
            try:
                # Descriptors ride on the first byte of their frame, so a
                # frame carrying them is never split: it goes out whole or not
                # at all, and a partial write can only happen without them.
                if fds:
                    sent = socket.send_fds(self._sock, [frame], fds)
                    # Gone as soon as sendmsg returns: they travelled with the
                    # frame's first byte, so a partial write does not leave
                    # any of them behind.
                    for fd in fds:
                        _close(fd)
                    fds = []
                    self._waiting[0] = (frame, fds)
                else:
                    sent = self._sock.send(frame)
            except BlockingIOError:
                break
            except OSError:
                # The peer is gone. The reader will notice and report it; a
                # send has nobody to report to.
                for _frame, waiting in self._waiting:
                    for fd in waiting:
                        _close(fd)
                self._waiting.clear()
                break
            if sent < len(frame):
                self._waiting[0] = (frame[sent:], [])
            else:
                self._waiting.popleft()

        wanted = bool(self._waiting) and not self._closed
        if wanted and not self._sending:
            self._loop.add_writer(self._sock.fileno(), self._flush)
            self._sending = True
        elif not wanted and self._sending:
            self._loop.remove_writer(self._sock.fileno())
            self._sending = False

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    def _readable(self) -> None:
        """
        Drain whatever the socket has and queue any complete frames.
        """
        try:
            data, fds, _flags, _addr = socket.recv_fds(self._sock, 65536, _MAX_FDS)
        except BlockingIOError:
            return
        except OSError:
            self._ended()
            return

        if not data:
            for fd in fds:
                _close(fd)
            self._ended()
            return

        # A descriptor is attached to the byte it was sent with, and the
        # kernel stops a read at that boundary, so these belong to the next
        # frame to complete -- which may need another read to finish arriving.
        self._carried.extend(fds)
        self._buffer.extend(data)

        while True:
            if len(self._buffer) < _HEADER.size:
                return
            (length,) = _HEADER.unpack_from(self._buffer)
            if _HEADER.size + length > MAX_MESSAGE:
                self._ended()
                return
            if len(self._buffer) < _HEADER.size + length:
                return
            body = bytes(self._buffer[_HEADER.size : _HEADER.size + length])
            del self._buffer[: _HEADER.size + length]
            with_frame, self._carried = self._carried, []
            try:
                message = json.loads(body)
            except ValueError:
                for fd in with_frame:
                    _close(fd)
                self._ended()
                return
            self._arrived.put_nowait((message, with_frame))

    def _ended(self) -> None:
        """
        Record that no more frames will arrive.
        """
        if not self._closed:
            self._loop.remove_reader(self._sock.fileno())
        for fd in self._carried:
            _close(fd)
        self._carried.clear()
        self._arrived.put_nowait(None)


def _close(fd: int) -> None:
    """
    Close a descriptor that nobody took ownership of.

    Args:
        fd: The descriptor.
    """
    try:
        os.close(fd)
    except OSError:
        pass
