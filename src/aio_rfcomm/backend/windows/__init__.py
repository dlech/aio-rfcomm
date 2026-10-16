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

# Measurements cited in this module were made by 2026-09-20 on a Windows
# machine whose version was not recorded, unless a claim says it was made on
# 2026-10-15. Those were made on Windows 10.0.26200.9457 (25H2), with
# bthport.sys, bthenum.sys and bthusb.sys 10.0.26100.9444, and rfcomm.sys and
# bthserv.dll 10.0.26100.9278. With any new one, record the same: Windows
# updates the Bluetooth stack through servicing, apart from its own version.

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import threading
from collections.abc import AsyncGenerator, Callable, Collection
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    ExitStack,
    asynccontextmanager,
)
from typing import assert_never
from uuid import UUID

from typing_extensions import override
from winrt.system import unbox_string
from winrt.windows.devices.bluetooth import (
    BluetoothAdapter,
    BluetoothCacheMode,
    BluetoothConnectionStatus,
    BluetoothDevice,
    BluetoothError,
)
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
from winrt.windows.devices.enumeration import (
    DeviceInformation,
    DeviceWatcher,
    DeviceWatcherStatus,
)
from winrt.windows.devices.radios import Radio, RadioState
from winrt.windows.foundation import EventRegistrationToken

from aio_rfcomm._teardown import unwrap_lone_error
from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.provider import (
    FIRST_CHANNEL,
    LAST_CHANNEL,
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    BackendRequest,
    BackendService,
)
from aio_rfcomm.backend.sdp import SdpError, read_channel
from aio_rfcomm.backend.stream import StreamChannel, open_stream
from aio_rfcomm.backend.windows import _service
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelInUseError,
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
# How many peers may be waiting to be accepted before Windows refuses them.
_BACKLOG = 4

_ATTEMPTS = 4
_BACKOFF_SECONDS = 0.5

# What a connect to a channel the device refuses fails with.
_WSAEHOSTDOWN = 10064


class WindowsChannel(StreamChannel):
    """
    An RFCOMM channel over a Bluetooth socket.

    Nothing to add: once connected it is an ordinary stream. It stays a class
    of its own so that a backtrace names the platform.
    """


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
        ``WSAEPROTONOSUPPORT``. ``asyncio.start_server`` fails the same way,
        only later and into the loop's exception handler. So the blocking
        accept runs on a daemon thread, the same shape the outgoing connect
        uses and for the same reasons (see ``_reach``). The accepted
        socket then works with overlapped I/O like any other.
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
        # addresses WinRT deals in, so _format has nothing to do here.
        try:
            peer = RfcommDeviceInfo(address)
        except ValueError:
            # The socket is ours until a request holds it.
            conn.close()
            raise
        return _Request(peer, conn)


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
        async with _take(conn) as channel:
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


@asynccontextmanager
async def _take(conn: socket.socket) -> AsyncGenerator[BackendChannel, None]:
    """
    Turn an accepted socket into a channel, closed on exit.

    Args:
        conn: The accepted socket.

    Yields:
        The channel.
    """
    async with AsyncExitStack() as stack:
        transport, protocol = await open_stream(conn)
        opened = WindowsChannel(transport, protocol)
        stack.push_async_callback(opened.close)
        yield opened


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
        cannot do: a channel another program holds is refused with
        ``WSAEADDRINUSE`` rather than quietly accepted. Binding channel zero
        does not pick one, though -- the socket stays on zero -- so a free
        channel is found by trying them.
        """
        async with AsyncExitStack() as stack:
            sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            stack.callback(sock.close)
            chosen = _bind_channel(sock, channel)
            sock.listen(_BACKLOG)

            try:
                _service.register(service, name, chosen)
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
            channel = await _look_up(device, address, service)

        async with AsyncExitStack() as stack:
            sock = await _open_socket(address, channel, self.address)
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


def _stop(watcher: DeviceWatcher) -> None:
    """
    Stop a device watcher, unless it already has.

    Args:
        watcher: The watcher.
    """
    # Stop raises for a watcher that is not running, and one aborts by itself
    # when it fails. Only documented.
    if watcher.status in (
        DeviceWatcherStatus.STARTED,
        DeviceWatcherStatus.ENUMERATION_COMPLETED,
    ):
        watcher.stop()


def _availability_of(radio: RadioState, name: str) -> tuple[Availability, str | None]:
    """
    Say whether a radio in a given state can be used.

    Args:
        radio: The radio's state.
        name: What to call the radio.

    Returns:
        The availability, and what to tell the user.
    """
    match radio:
        case RadioState.ON:
            return Availability.USABLE, None
        case RadioState.OFF | RadioState.DISABLED | RadioState.UNKNOWN:
            return (
                Availability.SWITCHED_OFF,
                f"{name} is switched off. Turn Bluetooth on",
            )
        case unexpected:
            assert_never(unexpected)


async def _read(
    bluetooth: WindowsBluetooth, state: AvailabilityState, notice: Callable[..., None]
) -> None:
    """
    Read whether Bluetooth is usable, and which radio to use.

    Args:
        bluetooth: Where the radio in use is kept.
        state: Where to report availability.
        notice: What the radio's events call.
    """
    available = await _list_adapters()
    # The one in use is kept while it is there. Otherwise the first.
    chosen = next((a for a in available if a[0] == bluetooth.adapter), None)
    chosen = chosen or (available[0] if available else None)
    found = None if chosen is None else await BluetoothAdapter.from_id_async(chosen[0])
    radio = None if found is None else await found.get_radio_async()

    if chosen is None or radio is None:
        bluetooth.follow_radio(None, None, notice)
        state.set(Availability.NO_ADAPTER)
        return

    identifier, address, name = chosen
    bluetooth.follow_radio(identifier, radio, notice)
    bluetooth.address = address
    state.set(*_availability_of(radio.state, name or identifier))


async def _follow(
    bluetooth: WindowsBluetooth,
    state: AvailabilityState,
    changed: asyncio.Event,
    notice: Callable[..., None],
) -> None:
    """
    Read whether the radio is usable, and again after anything that may have
    changed it.

    Args:
        bluetooth: Where the radio in use is kept.
        state: Where to report availability.
        changed: Set by every event that may matter.
        notice: What the radio's events call.
    """
    while True:
        changed.clear()

        try:
            await _read(bluetooth, state, notice)
        except Exception as error:
            # A failed read says nothing about Bluetooth, so what was last
            # known stands until the next event. With nothing known yet,
            # opening Bluetooth would wait for ever.
            logger.exception("could not read whether Bluetooth is usable")

            if not state.is_known:
                state.set(
                    Availability.SERVICE_UNAVAILABLE,
                    f"could not read whether Bluetooth is usable: {error}",
                )

        await changed.wait()


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
        # the radio off is not one: measured on 2026-10-15, the adapter stays
        # and the watcher says nothing, while the radio reports StateChanged
        # within 50 ms.
        watcher = DeviceInformation.create_watcher_aqs_filter(
            BluetoothAdapter.get_device_selector()
        )

        with ExitStack() as cleanup:
            # Undone in reverse, so the handlers go before the watcher stops,
            # and nothing stays subscribed if stopping fails.
            cleanup.callback(bluetooth.follow_radio, None, None, notice)
            cleanup.callback(_stop, watcher)
            cleanup.callback(watcher.remove_added, watcher.add_added(notice))
            cleanup.callback(watcher.remove_removed, watcher.add_removed(notice))
            cleanup.callback(watcher.remove_updated, watcher.add_updated(notice))
            watcher.start()

            with unwrap_lone_error():
                async with asyncio.TaskGroup() as group:
                    following = group.create_task(
                        _follow(bluetooth, state, changed, notice)
                    )

                    try:
                        yield bluetooth
                    finally:
                        following.cancel()


async def _list_adapters() -> list[tuple[str, str | None, str | None]]:
    """
    Read every Bluetooth radio the machine has.

    Returns:
        The identifier, address and name of each adapter that supports
        Bluetooth Classic. A Low Energy only radio cannot carry RFCOMM at
        all, so listing it would only offer a choice that cannot work.
    """
    found: list[tuple[str, str | None, str | None]] = []
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
        found.append((info.id, _format(adapter.bluetooth_address), info.name or None))
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


async def _device(device: RfcommDeviceInfo, address: str) -> BluetoothDevice:
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
    if device.id is not None:
        found = await BluetoothDevice.from_id_async(device.id)
    else:
        found = await BluetoothDevice.from_bluetooth_address_async(_parse(address))

    if found is None:
        raise DeviceNotFoundError(f"{address} could not be reached")
    return found


async def _look_up(device: RfcommDeviceInfo, address: str, service: UUID) -> int:
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
        to say which it meant. See the note where that is decided.

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

    # A device that does not answer is not an error to this query. Measured
    # on 2026-10-15, for a paired device switched off and for an address
    # Windows had never seen: SUCCESS with no services after about 5 s, the
    # device still disconnected. One that answered is connected by then.
    if (
        not result.services
        and found.connection_status == BluetoothConnectionStatus.DISCONNECTED
    ):
        raise DeviceNotFoundError(f"{address} did not answer")

    offered = [s for s in result.services if s.service_id.uuid == service]
    if not offered:
        raise ServiceNotFoundError(f"{address} does not offer {service}")

    if len(offered) > 1:
        # A device may publish the same service class more than once, on
        # different channels. The first is taken, to match macOS, whose
        # getServiceRecordForUUID returns a single record and so cannot offer
        # a choice at all. BlueZ likewise picks inside ConnectProfile without
        # saying which. Windows is the only platform where the choice is even
        # visible, and making it behave differently here would mean the same
        # device answered differently depending on what it was called from.
        # Recorded rather than hidden, since a caller who needs a particular
        # one can ask for its channel number directly.
        logger.debug(
            "%s publishes %d records for %s. Using the first",
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


def _refused(error: OSError, address: str, channel: int) -> RfcommError | None:
    """
    Tell a refused channel from the flakiness that is worth trying again.

    Args:
        error: What the connect raised.
        address: The device's Bluetooth address.
        channel: The RFCOMM channel number.

    Returns:
        The error to raise for a refusal, or ``None`` for anything else.
    """
    # Measured on 2026-10-15 against a macOS 26.6.2 peer: a channel nothing
    # listens on fails with WSAEHOSTDOWN as soon as the peer refuses it, while
    # a device that is switched off, and an address never seen, time out with
    # WSAETIMEDOUT. A link refused for authentication is not measured. Asking
    # again is refused again, and Windows holds the next attempt on that
    # device for about a minute, until its link to it has gone.
    if getattr(error, "winerror", None) == _WSAEHOSTDOWN:
        return ServiceNotFoundError(
            f"{address} refused channel {channel}. Usually nothing there is "
            "listening on it"
        )

    return None


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
            refused = _refused(error, address, channel)

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


def _bind_channel(sock: socket.socket, channel: int | None) -> int:
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


def _deregister(service: UUID, name: str, channel: int) -> None:
    """
    Stop advertising, and say so if Windows refuses.

    Args:
        service: The service UUID.
        name: The name it was published under.
        channel: The channel it was published on.
    """
    failure = _service.delete(service, name, channel)
    if failure:
        # Not raised: Windows drops the record when the process exits anyway,
        # and the delete is refused even for a query set that registered
        # cleanly. Worth a line in the log and nothing more.
        logger.debug("WSASetService would not withdraw %s: error %d", service, failure)


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
        return BluetoothUnavailableError(
            Availability.SWITCHED_OFF, "the Bluetooth radio is not available"
        )
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
