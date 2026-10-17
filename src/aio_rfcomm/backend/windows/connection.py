# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Connecting and listening over Winsock's Bluetooth sockets.

ProactorEventLoop can neither connect nor accept these sockets itself, so
both happen on threads of their own, and only the finished socket is handed
to asyncio.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import threading
from collections.abc import AsyncGenerator
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)

from typing_extensions import override

from aio_rfcomm.backend.provider import (
    FIRST_CHANNEL,
    LAST_CHANNEL,
    BackendChannel,
    BackendRequest,
    BackendService,
)
from aio_rfcomm.backend.stream import StreamChannel, StreamProtocol, open_stream
from aio_rfcomm.backend.windows.errors import translate_refusal
from aio_rfcomm.backend.windows.link import LinkWatch, watch_link
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    ChannelInUseError,
    CloseReason,
    ConnectionFailedError,
)

__all__ = ["WindowsChannel", "WindowsService", "bind_channel", "open_socket"]

logger = logging.getLogger(__name__)


# Connecting is retried because it fails without anything being wrong: a run
# on an unrecorded Windows version that eventually succeeded took four
# attempts, the first three timing out. These are not a limit on how long a
# caller may wait -- that is the caller's to decide with a cancel scope -- but
# the point at which retrying stops looking like flakiness and starts looking
# like a device that is not there.
_ATTEMPTS = 4

_BACKOFF_SECONDS = 0.5


class WindowsChannel(StreamChannel):
    """
    An RFCOMM channel over a Bluetooth socket.
    """

    def __init__(
        self,
        transport: asyncio.Transport,
        protocol: StreamProtocol,
        link: LinkWatch | None,
    ) -> None:
        """
        Args:
            transport: Carries the connected socket.
            protocol: What the transport reports to.
            link: Hears when the link to the peer goes down, or ``None`` if
                the peer could not be found to watch.
        """
        super().__init__(transport, protocol)
        self._link = link

    @override
    async def explain_end(self) -> CloseReason:
        """
        Tell a hang-up from a lost link by whether the link went down too.

        Returns:
            Why the channel went away.
        """
        # Measured on Windows 10.0.26200.9457: the socket itself cannot tell.
        # It ends cleanly both when the peer hangs up and when it switches off.
        if self._link is not None and await self._link.went_down():
            return CloseReason.LINK_LOST

        return CloseReason.PEER_CLOSED

    @override
    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if self._link is not None:
                self._link.close()


def _settle(reached: asyncio.Future[None], error: BaseException | None) -> None:
    """
    Hand a connect thread's outcome back to whoever is waiting.

    Args:
        reached: The future the waiter is on.
        error: What went wrong, or ``None`` if the socket connected.
    """
    if reached.done():
        # The caller gave up while the thread was still going. Nothing to
        # tell, and the socket has already been closed underneath it.
        return
    if error is None:
        reached.set_result(None)
    else:
        reached.set_exception(error)


async def _reach(sock: socket.socket, address: str, channel: int) -> None:
    """
    Connect one socket, on a thread so the loop keeps running.

    ``ProactorEventLoop``'s own ``sock_connect`` cannot do this: it goes
    through ``ConnectEx``, which only understands IP addresses. A blocking
    connect on another thread can, and the finished socket works with
    overlapped I/O afterwards.

    The thread is made here rather than taken from the loop's executor, and
    it is a daemon. A blocking connect cannot be interrupted, and the default
    executor's threads are neither daemons nor can be made into them --
    ``asyncio.run`` waits for that executor as it shuts down, so a single
    connect that never returns would keep the whole process alive after
    everything else had finished. A daemon thread cannot do that: the worst
    it costs is itself, until the process ends.

    Closing the socket when the caller gives up is what usually makes the
    blocking call return early, and it is worth doing, but nothing here
    depends on it working.

    Args:
        sock: The socket to connect.
        address: The device's Bluetooth address.
        channel: The RFCOMM channel number.

    Raises:
        OSError: The connection failed.
    """
    loop = asyncio.get_running_loop()
    reached: asyncio.Future[None] = loop.create_future()

    def connect() -> None:
        try:
            sock.connect((address, channel))
        except BaseException as error:  # noqa: BLE001  (carried to the waiter)
            loop.call_soon_threadsafe(_settle, reached, error)
        else:
            loop.call_soon_threadsafe(_settle, reached, None)

    threading.Thread(
        target=connect, name=f"aio-rfcomm connect {address}", daemon=True
    ).start()

    try:
        await reached
    except asyncio.CancelledError:
        sock.close()
        raise


async def open_socket(address: str, channel: int, adapter: str | None) -> socket.socket:
    """
    Connect a Bluetooth socket, retrying while it looks like flakiness.

    Args:
        address: The device's Bluetooth address.
        channel: The RFCOMM channel number.
        adapter: The adapter's address, if one should be insisted on.

    Returns:
        The connected socket.

    Raises:
        ConnectionFailedError: Every attempt failed.
    """
    last: OSError | None = None
    for attempt in range(_ATTEMPTS):
        sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
        )
        try:
            await _reach(sock, address, channel)
        except OSError as error:
            sock.close()
            refused = translate_refusal(error, address, channel)

            if refused is not None:
                raise refused from error

            last = error

            if attempt + 1 < _ATTEMPTS:
                await asyncio.sleep(_BACKOFF_SECONDS * (attempt + 1))
            continue
        except BaseException:
            sock.close()
            raise
        return sock

    raise ConnectionFailedError(
        f"could not reach {address} on channel {channel} after "
        f"{_ATTEMPTS} attempts: {last}"
    )


def bind_channel(sock: socket.socket, channel: int | None) -> int:
    """
    Put a listening socket on a channel, or find it one.

    Args:
        sock: The socket to bind.
        channel: The channel asked for, or ``None`` for any free one.

    Returns:
        The channel bound.

    Raises:
        ChannelInUseError: The requested channel is taken, or none is free.
    """
    if channel is not None:
        try:
            sock.bind((socket.BDADDR_ANY, channel))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise ChannelInUseError(
                    f"channel {channel} is already in use on this machine"
                ) from error
            raise ConnectionFailedError(
                f"could not listen on channel {channel}: {error}"
            ) from error
        return channel

    # Counted down from the top, where the profiles Windows publishes itself
    # are least likely to be.
    for candidate in range(LAST_CHANNEL, FIRST_CHANNEL - 1, -1):
        try:
            sock.bind((socket.BDADDR_ANY, candidate))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                continue
            raise ConnectionFailedError(
                f"could not listen on channel {candidate}: {error}"
            ) from error
        return candidate

    raise ChannelInUseError("every RFCOMM channel on this machine is in use")


@asynccontextmanager
async def _take(
    conn: socket.socket, address: str
) -> AsyncGenerator[BackendChannel, None]:
    """
    Turn an accepted socket into a channel, closed on exit.

    Args:
        conn: The accepted socket.
        address: The peer's address.

    Yields:
        The channel.
    """
    async with AsyncExitStack() as stack:
        link = await watch_link(address)
        transport, protocol = await open_stream(conn)
        opened = WindowsChannel(transport, protocol, link)
        stack.push_async_callback(opened.close)
        yield opened


class _Request(BackendRequest):
    """
    A peer that has connected, and the socket accepted for it.
    """

    def __init__(self, peer: RfcommDeviceInfo, conn: socket.socket) -> None:
        self._peer = peer
        self._conn: socket.socket | None = conn

    @property
    @override
    def info(self) -> RfcommDeviceInfo:
        return self._peer

    @override
    def open(self) -> AbstractAsyncContextManager[BackendChannel]:
        return self._take()

    @asynccontextmanager
    async def _take(self) -> AsyncGenerator[BackendChannel, None]:
        conn, self._conn = self._conn, None
        assert conn is not None, "a request's connection was taken twice"
        async with _take(conn, self._peer.address) as channel:
            yield channel

    @override
    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()


def _deliver(
    arrived: asyncio.Future[tuple[socket.socket, str]],
    result: tuple[socket.socket, str] | None,
    error: BaseException | None,
) -> None:
    """
    Hand an accept thread's outcome back to whoever is waiting.

    Args:
        arrived: The future the waiter is on.
        result: The accepted socket and the peer's address.
        error: What went wrong instead.
    """
    if arrived.done():
        if result is not None:
            # Nobody is waiting any more, so this connection is ours to close.
            result[0].close()
        return
    if error is None:
        assert result is not None
        arrived.set_result(result)
    else:
        arrived.set_exception(error)


class WindowsService(BackendService):
    """
    A listening RFCOMM socket with a published record behind it.
    """

    def __init__(self, sock: socket.socket, channel: int) -> None:
        """
        Args:
            sock: The listening socket.
            channel: The channel it is bound to.
        """
        self._sock = sock
        self._channel = channel

    @property
    @override
    def channel(self) -> int:
        return self._channel

    @override
    async def accept(self) -> BackendRequest:
        """
        Wait for a peer, on a thread, because the loop cannot do it.

        ``ProactorEventLoop`` cannot accept an RFCOMM connection at all:
        asyncio builds the socket to accept onto with ``socket.socket(family)``
        and no protocol, which ``AF_BLUETOOTH`` rejects with
        ``WSAEPROTONOSUPPORT`` on an unrecorded Windows version.
        ``asyncio.start_server`` fails the same way, only later and into the
        loop's exception handler. So the blocking accept runs on a daemon
        thread, the same shape the outgoing connect uses and for the same
        reasons (see ``_reach``). The accepted socket then works with
        overlapped I/O like any other.
        """
        loop = asyncio.get_running_loop()
        arrived: asyncio.Future[tuple[socket.socket, str]] = loop.create_future()

        def wait() -> None:
            try:
                conn, peer = self._sock.accept()
            except BaseException as error:  # noqa: BLE001  (carried to the waiter)
                loop.call_soon_threadsafe(_deliver, arrived, None, error)
            else:
                loop.call_soon_threadsafe(_deliver, arrived, (conn, peer[0]), None)

        threading.Thread(
            target=wait, name=f"aio-rfcomm accept {self._channel}", daemon=True
        ).start()

        try:
            conn, address = await arrived
        except asyncio.CancelledError:
            # The service is being withdrawn: closing the listening socket is
            # what makes the blocked accept return, and is wanted anyway.
            self._sock.close()
            raise

        # accept() hands the peer back already formatted, unlike the integer
        # addresses WinRT deals in, so format_address has nothing to do here.
        try:
            peer = RfcommDeviceInfo(address)
        except ValueError:
            # The socket is ours until a request holds it.
            conn.close()
            raise
        return _Request(peer, conn)
