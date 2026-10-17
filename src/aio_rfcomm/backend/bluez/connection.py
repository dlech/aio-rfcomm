# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Channels and requests over the file descriptors BlueZ hands over.
"""

import asyncio
import functools
import logging
import os
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

from typing_extensions import override

from aio_rfcomm.backend.bluez.profile import ServerProfile
from aio_rfcomm.backend.provider import (
    BackendChannel,
    BackendRequest,
    BackendService,
)
from aio_rfcomm.backend.stream import (
    StreamChannel,
    StreamProtocol,
    hang_up,
    open_stream,
)
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    CloseReason,
)

__all__ = ["BlueZChannel", "BlueZService"]

logger = logging.getLogger(__name__)


class BlueZChannel(StreamChannel):
    """
    An RFCOMM channel over the file descriptor BlueZ supplied.
    """

    def __init__(
        self,
        transport: asyncio.Transport,
        protocol: StreamProtocol,
        refresh: Callable[[], Awaitable[None]],
        link_up: Callable[[], Awaitable[bool | None]],
    ) -> None:
        """
        Args:
            transport: Carries the descriptor.
            protocol: What the transport reports to.
            refresh: Reads whether Bluetooth is still usable, and reports it.
            link_up: Asks BlueZ whether the link to the peer is still up, or
                gives ``None`` if it cannot tell.
        """
        super().__init__(transport, protocol)
        self._refresh = refresh
        self._link_up = link_up

    @override
    async def explain_end(self) -> CloseReason:
        """
        Tell a hang-up from a lost link, once a radio going off here is known.

        Returns:
            Why the channel went away.
        """
        # Measured on BlueZ 5.85: switching the radio off ends the channel
        # about 50 ms before the adapter is reported off, while its power state
        # already reads as going off. So the adapter is read again first, and
        # if Bluetooth is no longer usable, that becomes why the channel ended.
        try:
            await self._refresh()
        except Exception:
            # The end still has to be explained. The reason is only less sure.
            logger.debug("could not read whether Bluetooth is usable", exc_info=True)

        # Measured on BlueZ 5.85 with kernel 7.0.0-38: a link that times out
        # fails the socket with ETIMEDOUT. A hang-up and a peer switching off
        # both fail it with ECONNRESET, but by then BlueZ already knows whether
        # the link went down: its Disconnected signal came 31 ms before the
        # socket failed.
        if isinstance(self._protocol.error, TimeoutError):
            return CloseReason.LINK_LOST

        try:
            link_up = await self._link_up()
        except Exception:
            logger.debug("could not ask whether the link is up", exc_info=True)
            link_up = None

        match link_up:
            case False:
                return CloseReason.LINK_LOST
            case True | None:
                return CloseReason.PEER_CLOSED


def _peer_from_path(device_path: str) -> RfcommDeviceInfo:
    """
    Describe the peer BlueZ named by object path.

    The address is read out of the path rather than fetched over D-Bus, so
    that accepting a connection costs no round trip. The name is left unset:
    finding it means another call, and a server that wants one can look the
    device up itself.

    Args:
        device_path: The peer's object path, ending in ``dev_AA_BB_...``.

    Returns:
        What we know about the peer.
    """
    tail = device_path.rsplit("/dev_", 1)[-1]
    return RfcommDeviceInfo(tail.replace("_", ":").upper(), None, device_path)


@asynccontextmanager
async def _take_connection(
    fd: int,
    refresh: Callable[[], Awaitable[None]],
    link_up: Callable[[], Awaitable[bool | None]],
) -> AsyncGenerator[BackendChannel, None]:
    """
    Turn a descriptor BlueZ handed over into a channel, closed on exit.

    Args:
        fd: The connected file descriptor.
        refresh: Reads whether Bluetooth is still usable.
        link_up: Asks BlueZ whether the link to the peer is still up.

    Yields:
        The channel.
    """
    async with AsyncExitStack() as stack:
        sock = socket.socket(fileno=fd)
        transport, protocol = await open_stream(sock)
        opened = BlueZChannel(transport, protocol, refresh, link_up)
        stack.push_async_callback(opened.close)
        yield opened


class _Request(BackendRequest):
    """
    A peer BlueZ has connected, and the descriptor it handed over.
    """

    def __init__(
        self,
        peer: RfcommDeviceInfo,
        fd: int,
        refresh: Callable[[], Awaitable[None]],
        link_up: Callable[[], Awaitable[bool | None]],
    ) -> None:
        self._peer = peer
        self._fd: int | None = fd
        self._refresh = refresh
        self._link_up = link_up

    @property
    @override
    def info(self) -> RfcommDeviceInfo:
        return self._peer

    @override
    def open(self) -> AbstractAsyncContextManager[BackendChannel]:
        return self._take()

    @asynccontextmanager
    async def _take(self) -> AsyncGenerator[BackendChannel, None]:
        fd, self._fd = self._fd, None
        assert fd is not None, "a request's connection was taken twice"
        async with _take_connection(fd, self._refresh, self._link_up) as channel:
            yield channel

    @override
    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            with socket.socket(fileno=fd) as sock:
                hang_up(sock)


class BlueZService(BackendService):
    """
    A service published through BlueZ, listening for peers.
    """

    def __init__(
        self,
        profile: ServerProfile,
        channel: int,
        refresh: Callable[[], Awaitable[None]],
        link_up: Callable[[str], Awaitable[bool | None]],
    ) -> None:
        """
        Args:
            profile: The registered profile BlueZ calls back.
            channel: The channel it was registered on.
            refresh: Reads whether Bluetooth is still usable, for the
                channels accepted here.
            link_up: Asks BlueZ whether the link to a device, by object path,
                is still up.
        """
        self._profile = profile
        self._channel = channel
        self._refresh = refresh
        self._link_up = link_up

    @property
    @override
    def channel(self) -> int:
        return self._channel

    @override
    async def accept(self) -> BackendRequest:
        device_path, fd = await self._profile.next_connection()
        try:
            peer = _peer_from_path(device_path)
        except ValueError:
            # The descriptor is ours until a request holds it.
            os.close(fd)
            raise
        return _Request(
            peer, fd, self._refresh, functools.partial(self._link_up, device_path)
        )
