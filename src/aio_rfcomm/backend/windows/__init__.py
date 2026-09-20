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
it fails and then succeeds with nothing changed, so it is retried.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import AsyncGenerator, Collection
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from uuid import UUID

from typing_extensions import override
from winrt.system import unbox_string
from winrt.windows.devices.bluetooth import (
    BluetoothAdapter,
    BluetoothCacheMode,
    BluetoothDevice,
    BluetoothError,
)
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
from winrt.windows.devices.enumeration import DeviceInformation
from winrt.windows.devices.radios import RadioState

from aio_rfcomm.backend.provider import BackendAdapter, BackendChannel, BackendProvider
from aio_rfcomm.backend.sdp import SdpError, read_channel
from aio_rfcomm.backend.stream import StreamChannel
from aio_rfcomm.discovery import RfcommAdapterInfo, RfcommDeviceInfo
from aio_rfcomm.errors import (
    AdapterNotFoundError,
    AdapterOffError,
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
)

__all__ = ["WindowsBackend"]

logger = logging.getLogger(__name__)

# A device interface carries its Bluetooth address, which saves opening
# every paired device just to ask. Written without separators and in lower
# case -- "001a7dda710c" -- so it needs putting into our own form.
_ADDRESS = "System.DeviceInterface.Bluetooth.DeviceAddress"

# Connecting is retried because it fails without anything being wrong: a run
# that eventually succeeded took four attempts, the first three timing out.
# These are not a limit on how long a caller may wait -- that is the caller's
# to decide with a cancel scope -- but the point at which retrying stops
# looking like flakiness and starts looking like a device that is not there.
_ATTEMPTS = 4
_BACKOFF_SECONDS = 0.5


class WindowsChannel(StreamChannel):
    """
    An RFCOMM channel over a Bluetooth socket.

    Nothing to add: once connected it is an ordinary stream. It stays a class
    of its own so that a backtrace names the platform.
    """


class WindowsAdapter(BackendAdapter):
    """
    One Bluetooth radio, found through WinRT.
    """

    def __init__(self, info: RfcommAdapterInfo) -> None:
        """
        Args:
            info: What was found out about the adapter when it was opened.
        """
        super().__init__()
        self._info = info

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        wanted = [RfcommServiceId.from_uuid(u) for u in _each(service)]

        found: list[RfcommDeviceInfo] = []
        for info in (
            await DeviceInformation.find_all_async_aqs_filter_and_additional_properties(
                BluetoothDevice.get_device_selector_from_pairing_state(True), [_ADDRESS]
            )
        ):
            address = _read_address(info)
            if wanted:
                device = await BluetoothDevice.from_id_async(info.id)
                if device is None:
                    # Paired, but the radio cannot produce it -- switched off,
                    # or removed between the listing and now.
                    continue
                if not await _offers_any(device, wanted):
                    continue
                if address is None:
                    address = _format(device.bluetooth_address)
            if address is None:
                # Nothing to identify it by, and the caller asked for no
                # filter, so there is no device object to fall back on.
                logger.debug("%s reports no Bluetooth address; skipping", info.name)
                continue
            found.append(RfcommDeviceInfo(address, info.name or None, info.id))
        return found

    @override
    def open_service(
        self, device: RfcommDeviceInfo | str, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, service=service)

    @override
    def open_channel(
        self, device: RfcommDeviceInfo | str, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, channel=channel)

    @asynccontextmanager
    async def _connect(
        self,
        device: RfcommDeviceInfo | str,
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
        address = device if isinstance(device, str) else device.address
        if channel is None:
            assert service is not None
            channel = await _look_up(device, address, service)

        async with AsyncExitStack() as stack:
            sock = await _open_socket(address, channel, self._info.address)
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
            reader, writer = await asyncio.open_connection(sock=sock)
            stack.callback(writer.close)

            yield WindowsChannel(reader, writer)


class WindowsBackend(BackendProvider):
    """
    The Windows implementation.
    """

    @override
    async def list_adapters(self) -> list[RfcommAdapterInfo]:
        return await _list_adapters()

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
        Open an adapter, refusing one whose radio is off.

        Args:
            adapter: Which adapter, or ``None`` for the default.

        Yields:
            The open adapter.
        """
        available = await _list_adapters()
        if not available:
            raise AdapterNotFoundError("this machine has no Bluetooth adapter")

        chosen = available[0] if adapter is None else adapter
        if not any(a.id == chosen.id for a in available):
            raise AdapterNotFoundError(chosen.id)

        radio = await (await _adapter(chosen.id)).get_radio_async()
        if radio.state != RadioState.ON:
            raise AdapterOffError(
                f"{chosen.name or chosen.id} is switched off; turn Bluetooth on"
            )

        yield WindowsAdapter(chosen)


async def _list_adapters() -> list[RfcommAdapterInfo]:
    """
    Read every Bluetooth radio the machine has.

    Returns:
        The adapters that support Bluetooth Classic. A Low Energy only radio
        cannot carry RFCOMM at all, so listing it would only offer a choice
        that cannot work.
    """
    found: list[RfcommAdapterInfo] = []
    for info in await DeviceInformation.find_all_async_aqs_filter(
        BluetoothAdapter.get_device_selector()
    ):
        # Opening each one is unavoidable, not laziness. Asking the listing
        # for the address instead was tried and measured: on an adapter
        # interface that property comes back empty, while other requested
        # properties come back populated and the same property on a device
        # interface gives the address -- so it is the adapter that does not
        # carry it, not the request that is wrong. Whether the radio does
        # Classic is only on the object either way. In practice there is one
        # adapter, so this is one extra call.
        adapter = await BluetoothAdapter.from_id_async(info.id)
        if adapter is None or not adapter.is_classic_supported:
            continue
        found.append(
            RfcommAdapterInfo(
                info.id, _format(adapter.bluetooth_address), info.name or None
            )
        )
    return found


def _read_address(info: DeviceInformation) -> str | None:
    """
    Take a device's Bluetooth address from its listing, if it is there.

    Args:
        info: The device as the listing described it.

    Returns:
        The address, or ``None`` if the listing did not carry one.
    """
    raw = info.properties.get(_ADDRESS)
    if raw is None:
        return None
    try:
        return _format(int(unbox_string(raw), 16))
    except (OSError, ValueError):
        return None


async def _adapter(identifier: str) -> BluetoothAdapter:
    """
    Open one adapter by its identifier.

    Args:
        identifier: The adapter's device identifier.

    Returns:
        The adapter.

    Raises:
        AdapterNotFoundError: It has gone since it was listed.
    """
    adapter = await BluetoothAdapter.from_id_async(identifier)
    if adapter is None:
        raise AdapterNotFoundError(identifier)
    return adapter


async def _device(device: RfcommDeviceInfo | str, address: str) -> BluetoothDevice:
    """
    Get hold of a device, however the caller named it.

    Args:
        device: A description from a listing, or a bare address.
        address: Its address, for the error message.

    Returns:
        The device.

    Raises:
        DeviceNotFoundError: The device could not be reached.
    """
    if isinstance(device, RfcommDeviceInfo) and device.id is not None:
        found = await BluetoothDevice.from_id_async(device.id)
    else:
        found = await BluetoothDevice.from_bluetooth_address_async(_parse(address))

    if found is None:
        raise DeviceNotFoundError(f"{address} could not be reached")
    return found


async def _look_up(device: RfcommDeviceInfo | str, address: str, service: UUID) -> int:
    """
    Ask a device which RFCOMM channel a service is on.

    The query is deliberately uncached. Windows will happily answer from a
    record it read months ago, and a channel number that has moved since is
    worse than a slow answer -- this is the one place the round trip is
    already being paid for, since the device has to be reached either way.

    Args:
        device: The device to ask.
        address: Its address, for the error messages.
        service: The service to find.

    Returns:
        The RFCOMM channel number of the first record offering the service.
        A device publishing several is rare and there is no way for a caller
        to say which it meant; see the note where that is decided.

    Raises:
        DeviceNotFoundError: The device could not be reached.
        ServiceNotFoundError: It does not offer that service.
        ConnectionFailedError: The query failed.
    """
    found = await _device(device, address)

    # Every service, then filtered here, rather than asking for the one we
    # want. Windows has a query that takes a service id, and on a device that
    # is not currently connected it answers nothing at all -- no error, no
    # connection attempt, an empty list that reads exactly like a device
    # which does not offer the service. Measured on a disconnected peer: the
    # filtered query returned nothing and left it disconnected, the
    # unfiltered query returned eleven services including the one wanted and
    # connected to do it, and the filtered query then worked. Asking for
    # everything is the only form that reliably goes and looks.
    result = await found.get_rfcomm_services_with_cache_mode_async(
        BluetoothCacheMode.UNCACHED
    )
    if result.error != BluetoothError.SUCCESS:
        raise _translate(result.error, address)

    offered = [s for s in result.services if s.service_id.uuid == service]
    if not offered:
        raise ServiceNotFoundError(f"{address} does not offer {service}")

    if len(offered) > 1:
        # A device may publish the same service class more than once, on
        # different channels. The first is taken, to match macOS, whose
        # getServiceRecordForUUID returns a single record and so cannot offer
        # a choice at all; BlueZ likewise picks inside ConnectProfile without
        # saying which. Windows is the only platform where the choice is even
        # visible, and making it behave differently here would mean the same
        # device answered differently depending on what it was called from.
        # Recorded rather than hidden, since a caller who needs a particular
        # one can ask for its channel number directly.
        logger.debug(
            "%s publishes %d records for %s; using the first",
            address,
            len(offered),
            service,
        )

    attributes = {
        key: bytes(value)
        for key, value in (
            await offered[0].get_sdp_raw_attributes_with_cache_mode_async(
                BluetoothCacheMode.UNCACHED
            )
        ).items()
    }
    try:
        return read_channel(attributes)
    except SdpError as error:
        raise ServiceNotFoundError(
            f"{address} offers {service} but its record does not say how to "
            f"reach it: {error}"
        ) from error


async def _offers_any(device: BluetoothDevice, wanted: list[RfcommServiceId]) -> bool:
    """
    Say whether a device offers any of these services.

    Answered from what Windows already knows rather than by asking the device.
    An uncached query would connect to every paired device in turn just to
    build a list, which is slow, intrusive, and would wake up things like
    headphones. Refreshing a stale record is a separate problem, and one every
    platform has.

    Args:
        device: The device to check.
        wanted: The services to look for.

    Returns:
        True if it offers at least one of them.
    """
    for service in wanted:
        result = await device.get_rfcomm_services_for_id_with_cache_mode_async(
            service, BluetoothCacheMode.CACHED
        )
        if result.error == BluetoothError.SUCCESS and result.services:
            return True
    return False


async def _open_socket(
    address: str, channel: int, adapter: str | None
) -> socket.socket:
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


def _translate(error: BluetoothError, address: str) -> RfcommError:
    """
    Turn a WinRT Bluetooth error into one of ours.

    Args:
        error: What WinRT reported.
        address: The device being reached.

    Returns:
        The error to raise instead.
    """
    if error == BluetoothError.DEVICE_NOT_CONNECTED:
        return DeviceNotFoundError(f"{address} did not answer")
    if error == BluetoothError.RADIO_NOT_AVAILABLE:
        return AdapterOffError("the Bluetooth radio is not available")
    return ConnectionFailedError(f"could not read {address}'s services: {error.name}")


def _each(service: UUID | Collection[UUID] | None) -> list[UUID]:
    """
    Normalise the service filter to a list.

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


def _format(address: int) -> str:
    """
    Put a Bluetooth address into the form this library reports.

    Args:
        address: The address as WinRT gives it, a 48-bit integer.

    Returns:
        The address, colon separated and upper case.
    """
    return address.to_bytes(6, "big").hex(":").upper()


def _parse(address: str) -> int:
    """
    Read an address back into the form WinRT wants.

    Args:
        address: A colon-separated Bluetooth address.

    Returns:
        The address as a 48-bit integer.

    Raises:
        DeviceNotFoundError: It is not a Bluetooth address.
    """
    try:
        return int(address.replace(":", "").replace("-", ""), 16)
    except ValueError as error:
        raise DeviceNotFoundError(f"{address} is not a Bluetooth address") from error
