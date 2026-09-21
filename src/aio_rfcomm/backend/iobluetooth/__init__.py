# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS backend, driving IOBluetooth through a helper process.

IOBluetooth will only work from the main thread of a process running a
CFRunLoop, and macOS ends any process that touches Bluetooth without an
application bundle saying why it wants to. A library cannot impose either
condition on the program that imports it, so it brings its own process that
meets both; see :mod:`aio_rfcomm.backend.iobluetooth.__main__`.

Nothing on this side imports rubicon or the framework. What comes back from
the helper is an ordinary connected file descriptor, which is exactly what the
other backends produce, so everything after the connection is shared code.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncGenerator, Collection
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from uuid import UUID

from typing_extensions import override

from aio_rfcomm.backend.iobluetooth._helper import Helper, run_helper
from aio_rfcomm.backend.provider import (
    BackendAdapter,
    BackendChannel,
    BackendProvider,
    BackendService,
)
from aio_rfcomm.backend.stream import StreamChannel
from aio_rfcomm.discovery import RfcommAdapterInfo, RfcommDeviceInfo
from aio_rfcomm.errors import (
    AdapterNotFoundError,
    AdapterOffError,
    CloseReason,
    ConnectionFailedError,
    UnsupportedOperationError,
)

__all__ = ["IOBluetoothBackend"]


class IOBluetoothChannel(StreamChannel):
    """
    An RFCOMM channel over the socket the helper handed back.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ending: asyncio.Future[str],
    ) -> None:
        """
        Args:
            reader: Reads bytes the device sent.
            writer: Writes bytes to send.
            ending: Resolved with the reason once the helper reports the
                channel has ended.
        """
        super().__init__(reader, writer)
        self._ending = ending
        ending.add_done_callback(self._ended)

    def _ended(self, ending: asyncio.Future[str]) -> None:
        """
        Record the reason as soon as the helper reports it.

        Args:
            ending: The future the reason arrived on.
        """
        if not ending.cancelled() and ending.exception() is None:
            self._mark_gone(_reason(ending.result()))

    @override
    async def explain_end(self) -> CloseReason:
        """
        Say why the channel ended, as the helper saw it.

        macOS can tell a peer closing one channel from the whole link
        dropping, by looking at whether the baseband connection is still up.
        The helper sends that reason before it closes the socket, so by the
        time the stream here reads empty the answer is already on its way and
        this waits for it rather than guessing.

        Returns:
            Why the channel went away.
        """
        return _reason(await self._ending)


class IOBluetoothAdapter(BackendAdapter):
    """
    The machine's Bluetooth adapter, worked through a running helper.
    """

    def __init__(self, helper: Helper, info: RfcommAdapterInfo) -> None:
        """
        Args:
            helper: The running helper process.
            info: What was found out about the adapter when it was opened.
        """
        super().__init__()
        self._helper = helper
        self._info = info

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        rows, _fds = await self._helper.ask("devices", service=_wanted(service))
        return [RfcommDeviceInfo(address, name) for address, name in rows]

    @override
    def open_service(
        self, device: RfcommDeviceInfo | str, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, service=str(service))

    @override
    def open_channel(
        self, device: RfcommDeviceInfo | str, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, channel=channel)

    @override
    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[BackendService]:
        raise UnsupportedOperationError(
            "serving a service is not implemented on macOS yet. It needs "
            "an SDP record encoder, which this library does not have yet. Connecting to a service from this machine works."
        )

    @asynccontextmanager
    async def _connect(
        self,
        device: RfcommDeviceInfo | str,
        *,
        service: str | None = None,
        channel: int | None = None,
    ) -> AsyncGenerator[BackendChannel, None]:
        """
        Ask the helper for a channel and wrap what comes back.

        Args:
            device: The device to reach.
            service: The service UUID to look up, if connecting by service.
            channel: The channel number, if connecting by number.

        Yields:
            The open channel, closed on exit.
        """
        address = device if isinstance(device, str) else device.address
        handle, fds = await self._helper.ask(
            "open", address=address, service=service, channel=channel
        )
        if len(fds) != 1:
            for fd in fds:
                os.close(fd)
            raise ConnectionFailedError(
                "the macOS Bluetooth helper opened a channel but did not "
                "hand back a socket for it"
            )

        async with AsyncExitStack() as stack:
            sock = socket.socket(fileno=fds[0])
            stack.callback(sock.close)
            reader, writer = await asyncio.open_connection(sock=sock)
            stack.callback(writer.close)

            ending = self._helper.expect(handle)
            stack.callback(self._helper.forget, handle)

            yield IOBluetoothChannel(reader, writer, ending)


class IOBluetoothBackend(BackendProvider):
    """
    The macOS implementation.
    """

    @override
    async def list_adapters(self) -> list[RfcommAdapterInfo]:
        async with run_helper() as helper:
            return await _read_adapters(helper)

    @override
    def open_adapter(
        self, adapter: RfcommAdapterInfo | None = None
    ) -> AbstractAsyncContextManager[BackendAdapter]:
        return self._open(adapter)

    @asynccontextmanager
    async def _open(
        self, adapter: RfcommAdapterInfo | None
    ) -> AsyncGenerator[BackendAdapter, None]:
        """
        Start a helper and open an adapter through it.

        The helper lives exactly as long as the adapter does, which is why
        adapters are opened with ``async with`` rather than handed out.

        Args:
            adapter: Which adapter, or ``None`` for the only one.

        Yields:
            The open adapter.
        """
        async with run_helper() as helper:
            available = await _read_adapters(helper)
            if not available:
                raise AdapterNotFoundError("this machine has no Bluetooth adapter")

            # macOS exposes a single controller and no way to choose between
            # radios, so there is nothing to be clever about here.
            chosen = available[0] if adapter is None else adapter
            if not any(a.id == chosen.id for a in available):
                raise AdapterNotFoundError(chosen.id)

            powered, _fds = await helper.ask("powered")
            if not powered:
                raise AdapterOffError(
                    f"{chosen.name or 'this Mac'}'s Bluetooth is switched off; "
                    "turn it on"
                )

            yield IOBluetoothAdapter(helper, chosen)


async def _read_adapters(helper: Helper) -> list[RfcommAdapterInfo]:
    """
    Ask the helper what adapters the machine has.

    Args:
        helper: The running helper.

    Returns:
        The adapters, at most one of them.
    """
    rows, _fds = await helper.ask("adapters")
    return [RfcommAdapterInfo(id, address, name) for id, address, name in rows]


def _wanted(service: UUID | Collection[UUID] | None) -> list[str]:
    """
    Normalise the service filter into something the protocol can carry.

    Args:
        service: One service, several, or ``None`` for no filter.

    Returns:
        The UUIDs to match, empty for no filter.
    """
    if service is None:
        return []
    if isinstance(service, UUID):
        return [str(service)]
    return [str(u) for u in service]


def _reason(name: str) -> CloseReason:
    """
    Turn the helper's word for a close back into one of ours.

    Args:
        name: The reason's name, as the helper sent it.

    Returns:
        Why the channel went away, or a lost link if the name is not one we
        know -- which can only mean the helper is a different version.
    """
    try:
        return CloseReason[name]
    except KeyError:
        return CloseReason.LINK_LOST
