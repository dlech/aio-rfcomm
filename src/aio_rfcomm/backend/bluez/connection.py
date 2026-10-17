# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Channels and requests over the file descriptors BlueZ hands over.
"""

import asyncio
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
    ) -> None:
        """
        Args:
            transport: Carries the descriptor.
            protocol: What the transport reports to.
            refresh: Reads whether Bluetooth is still usable, and reports it.
        """
        super().__init__(transport, protocol)
        self._refresh = refresh

    @override
    async def settle_end(self) -> None:
        """
        Make sure a radio going off is known before the end is put down to
        the connection.

        Measured on BlueZ 5.85: switching the radio off ends the channel about
        50 ms before the adapter is reported off, while its power state
        already reads as going off. So the adapter is read again here, and if
        Bluetooth is no longer usable that is reported first, and becomes why
        the channel ended.
        """
        try:
            await self._refresh()
        except Exception:
            # The end is still settled, only without the latest reading.
            logger.debug("could not read whether Bluetooth is usable", exc_info=True)


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
    fd: int, refresh: Callable[[], Awaitable[None]]
) -> AsyncGenerator[BackendChannel, None]:
    """
    Turn a descriptor BlueZ handed over into a channel, closed on exit.

    Args:
        fd: The connected file descriptor.
        refresh: Reads whether Bluetooth is still usable.

    Yields:
        The channel.
    """
    async with AsyncExitStack() as stack:
        sock = socket.socket(fileno=fd)
        transport, protocol = await open_stream(sock)
        opened = BlueZChannel(transport, protocol, refresh)
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
    ) -> None:
        self._peer = peer
        self._fd: int | None = fd
        self._refresh = refresh

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
        async with _take_connection(fd, self._refresh) as channel:
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
    ) -> None:
        """
        Args:
            profile: The registered profile BlueZ calls back.
            channel: The channel it was registered on.
            refresh: Reads whether Bluetooth is still usable, for the
                channels accepted here.
        """
        self._profile = profile
        self._channel = channel
        self._refresh = refresh

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
        return _Request(peer, fd, self._refresh)
