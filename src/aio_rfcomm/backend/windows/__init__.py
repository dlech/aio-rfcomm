# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The Windows backend: WinRT to find things, a Bluetooth socket to talk.

The work is split between two worlds and the seam is the channel number.
WinRT enumerates adapters and paired devices and reads service records, but
its own ``StreamSocket`` is a poor fit for asyncio. Winsock has had
``AF_BLUETOOTH`` for years and Python exposes it, so once the channel number
is known the connection is an ordinary socket and everything after it is the
shared stream channel.

Two things about that socket are not ordinary. ``ProactorEventLoop`` is the
default on Windows and its ``sock_connect`` goes through ``ConnectEx``, which
only understands IP addresses -- so connecting happens on a worker thread and
only the finished socket is handed to asyncio, whose overlapped I/O does not
care what family it is. And connecting is flaky in a way that is not an error:
on an unrecorded Windows version it fails and then succeeds with nothing
changed, so it is retried.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncGenerator, Callable, Collection
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    ExitStack,
    asynccontextmanager,
)
from uuid import UUID

from typing_extensions import override
from winrt.windows.devices.bluetooth import (
    BluetoothAdapter,
    BluetoothDevice,
)
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
from winrt.windows.devices.enumeration import (
    DeviceInformation,
)
from winrt.windows.devices.radios import Radio
from winrt.windows.foundation import EventRegistrationToken

from aio_rfcomm._teardown import unwrap_lone_error
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    BackendService,
)
from aio_rfcomm.backend.stream import open_stream
from aio_rfcomm.backend.windows import publish
from aio_rfcomm.backend.windows.addresses import format_address
from aio_rfcomm.backend.windows.availability import follow_availability, stop_watcher
from aio_rfcomm.backend.windows.connection import (
    AbandonedConnects,
    WindowsChannel,
    WindowsService,
    bind_channel,
    open_socket,
)
from aio_rfcomm.backend.windows.devices import (
    ADDRESS_PROPERTY,
    look_up,
    offers_any,
    read_address,
)
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    ConnectionFailedError,
)

__all__ = ["WindowsBackend"]

logger = logging.getLogger(__name__)


# How many peers may be waiting to be accepted before Windows refuses them.
_BACKLOG = 4


def _each(service: UUID | Collection[UUID] | None) -> list[UUID]:
    """
    Normalize the service filter to a list.

    Args:
        service: One service, several, or ``None`` for no filter.

    Returns:
        The services to match, empty for no filter.
    """
    if service is None:
        return []
    if isinstance(service, UUID):
        return [service]
    return list(service)


def _deregister(service: UUID, name: str, channel: int) -> None:
    """
    Stop advertising, and say so if Windows refuses.

    Args:
        service: The service UUID.
        name: The name it was published under.
        channel: The channel it was published on.
    """
    failure = publish.delete(service, name, channel)
    if failure:
        # Not raised: Windows drops the record when the process exits anyway,
        # and the delete is refused even for a query set that registered
        # cleanly, on an unrecorded Windows version. Worth a line in the log
        # and nothing more.
        logger.debug("WSASetService would not withdraw %s: error %d", service, failure)


class WindowsBluetooth(BackendBluetooth):
    """
    Bluetooth through one radio, found through WinRT.
    """

    def __init__(self, address: str | None) -> None:
        """
        Args:
            address: The radio's own address, if WinRT reports one.
        """
        self.address = address
        """
        The radio's own address, if WinRT reports one.
        """

        self.adapter: str | None = None
        """
        The device identifier of the adapter in use.
        """

        self._radio: Radio | None = None
        self._radio_token: EventRegistrationToken | None = None
        self._abandoned = AbandonedConnects()

    def follow_radio(
        self,
        adapter: str | None,
        radio: Radio | None,
        notice: Callable[..., None],
    ) -> None:
        """
        Listen to the radio of the adapter in use, and only to it.

        Args:
            adapter: The adapter's device identifier, or ``None`` for none.
            radio: Its radio, as just read.
            notice: What the radio's state changes call.
        """
        # Subscribed afresh on every read, since an adapter unplugged and
        # plugged back in between two reads keeps its identifier but has a
        # new radio.
        if self._radio is not None and self._radio_token is not None:
            self._radio.remove_state_changed(self._radio_token)

        self._radio = None
        self._radio_token = None
        self.adapter = adapter

        if radio is not None:
            self._radio_token = radio.add_state_changed(notice)
            self._radio = radio

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        wanted = [RfcommServiceId.from_uuid(u) for u in _each(service)]

        found: list[RfcommDeviceInfo] = []
        for info in (
            await DeviceInformation.find_all_async_aqs_filter_and_additional_properties(
                BluetoothDevice.get_device_selector_from_pairing_state(True),
                [ADDRESS_PROPERTY],
            )
        ):
            address = read_address(info)
            if wanted:
                # Never None, measured on Windows 10.0.26200.9711: an unknown
                # address still gives a device, and a malformed id fails.
                device = await BluetoothDevice.from_id_async(info.id)
                if not await offers_any(device, wanted):
                    continue
                if address is None:
                    address = format_address(device.bluetooth_address)
            if address is None:
                # Nothing to identify it by, and the caller asked for no
                # filter, so there is no device object to fall back on.
                logger.debug("%s reports no Bluetooth address. Skipping", info.name)
                continue
            found.append(RfcommDeviceInfo(address, info.name or None, info.id))
        return found

    @override
    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, service=service)

    @override
    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, channel=channel)

    @override
    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[BackendService]:
        return self._serve(service, name, channel)

    @asynccontextmanager
    async def _serve(
        self, service: UUID, name: str, channel: int | None
    ) -> AsyncGenerator[BackendService, None]:
        """
        Listen on a channel and advertise it.

        Binding answers which channels are free, which is the part BlueZ
        cannot do: on an unrecorded Windows version, a channel another program
        holds is refused with ``WSAEADDRINUSE`` rather than quietly accepted.
        Binding channel zero does not pick one, though -- the socket stays on
        zero -- so a free channel is found by trying them.
        """
        async with AsyncExitStack() as stack:
            sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            stack.callback(sock.close)
            chosen = bind_channel(sock, channel)
            sock.listen(_BACKLOG)

            try:
                publish.register(service, name, chosen)
            except OSError as error:
                raise ConnectionFailedError(
                    f"could not advertise {service} on channel {chosen}: {error}"
                ) from error
            stack.callback(_deregister, service, name, chosen)

            yield WindowsService(sock, chosen)

    @asynccontextmanager
    async def _connect(
        self,
        device: RfcommDeviceInfo,
        *,
        service: UUID | None = None,
        channel: int | None = None,
    ) -> AsyncGenerator[BackendChannel, None]:
        """
        Open a channel, looking the number up first if only a service is known.

        Args:
            device: The device to reach.
            service: The service to look up, if connecting by service.
            channel: The channel number, if connecting by number.

        Yields:
            The open channel, closed on exit.
        """
        address = device.address
        if channel is None:
            assert service is not None
            channel = await look_up(device, address, service)

        async with AsyncExitStack() as stack:
            sock = await open_socket(address, channel, self._abandoned)
            stack.callback(sock.close)

            # An ordinary asyncio stream over a Bluetooth socket, which reads
            # like it should not work and does. What ProactorEventLoop cannot
            # do with this socket is connect it: sock_connect goes through
            # ConnectEx, whose address handling knows only AF_INET and
            # AF_INET6. Reading and writing go through overlapped I/O, which
            # takes a handle and a buffer and never looks at the address
            # family -- so once the socket is connected, by whatever means,
            # the standard APIs drive it unchanged. That is what lets this
            # share a stream channel with backends on other platforms.
            transport, protocol = await open_stream(sock)
            opened = WindowsChannel(transport, protocol)
            stack.push_async_callback(opened.close)

            yield opened


class WindowsBackend(BackendProvider):
    """
    The Windows implementation.
    """

    @override
    def open(
        self, state: AvailabilityState
    ) -> AbstractAsyncContextManager[BackendBluetooth]:
        return self._open(state)

    @asynccontextmanager
    async def _open(
        self, state: AvailabilityState
    ) -> AsyncGenerator[BackendBluetooth, None]:
        """
        Follow whether the radio is usable, for as long as Bluetooth is open.

        Args:
            state: Where to report availability.

        Yields:
            Bluetooth through the first radio.
        """
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()

        def notice(*_: object) -> None:
            # WinRT calls back on threads of its own.
            loop.call_soon_threadsafe(changed.set)

        bluetooth = WindowsBluetooth(None)
        # Adapters arriving and leaving, which is only documented. Switching
        # the radio off is not one: measured on Windows 10.0.26200.9457, the
        # adapter stays and the watcher says nothing, while the radio reports
        # StateChanged within 50 ms.
        watcher = DeviceInformation.create_watcher_aqs_filter(
            BluetoothAdapter.get_device_selector()
        )

        with ExitStack() as cleanup:
            # Undone in reverse, so the handlers go before the watcher stops,
            # and nothing stays subscribed if stopping fails.
            cleanup.callback(bluetooth.follow_radio, None, None, notice)
            cleanup.callback(stop_watcher, watcher)
            cleanup.callback(watcher.remove_added, watcher.add_added(notice))
            cleanup.callback(watcher.remove_removed, watcher.add_removed(notice))
            cleanup.callback(watcher.remove_updated, watcher.add_updated(notice))
            watcher.start()

            with unwrap_lone_error():
                async with asyncio.TaskGroup() as group:
                    following = group.create_task(
                        follow_availability(bluetooth, state, changed, notice)
                    )

                    try:
                        yield bluetooth
                    finally:
                        following.cancel()
