# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Stand-ins for WinRT's Bluetooth classes and Winsock's Bluetooth sockets, and
nothing above them.

Each reproduces what Windows was measured doing, or what is documented where
nothing was measured, and calls back from threads of its own, as WinRT does.
Measured latencies are noted but not reproduced, since none of the code under
test depends on them.

Windows only: the enums, service ids and boxed strings are WinRT's own, so only
what would reach a radio is stood in for. A connected socket is a real one, one
end of a socket pair, so asyncio carries it exactly as it carries a Bluetooth
socket.
"""

import ctypes
import errno
import queue
import socket
import sys
import threading
import uuid
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

assert not TYPE_CHECKING or sys.platform == "win32"

import pytest
from typing_extensions import override
from winrt.system import box_string
from winrt.windows.devices.bluetooth import (
    BluetoothCacheMode,
    BluetoothConnectionStatus,
    BluetoothError,
)
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
from winrt.windows.devices.enumeration import DeviceWatcherStatus
from winrt.windows.devices.radios import RadioState

from aio_rfcomm.backend import windows
from aio_rfcomm.backend.windows import availability, connection, devices, publish

ADDRESS_PROPERTY = "System.DeviceInterface.Bluetooth.DeviceAddress"

# What Winsock fails a connect with, measured on Windows 10.0.26200.9457 and
# 10.0.26200.9711.
WSAEADDRINUSE = 10048
WSAEHOSTDOWN = 10064
WSAETIMEDOUT = 10060

_ADAPTERS = "adapters"
_PAIRED = "paired"


def _later(call: Callable[[], None]) -> None:
    """
    Make a call from a thread of its own, the way WinRT reports events.
    """
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join()


def winsock_error(code: int) -> OSError:
    """
    Make the error Python raises for a failed Winsock call.

    Python builds it from the Winsock code, so it is a ``TimeoutError`` for
    ``WSAETIMEDOUT``, as the real one is.
    """
    return OSError(None, f"Winsock error {code}", None, code)


class _Handlers:
    """
    One WinRT event: handlers added and removed by token.
    """

    def __init__(self) -> None:
        self._handlers: dict[int, Callable[..., None]] = {}
        self._next = 0

    def add(self, handler: Callable[..., None]) -> int:
        self._next += 1
        self._handlers[self._next] = handler
        return self._next

    def remove(self, token: int) -> None:
        del self._handlers[token]

    def __len__(self) -> int:
        return len(self._handlers)

    def fire(self, sender: object, args: object) -> None:
        for handler in list(self._handlers.values()):
            _later(lambda handler=handler: handler(sender, args))


class FakeRadio:
    """
    A stand-in for ``Radio``.

    Measured on Windows 10.0.26200.9457: switching the radio reports
    StateChanged within 50 ms.
    """

    def __init__(self, state: RadioState) -> None:
        self.state = state
        self.state_changed = _Handlers()

    def add_state_changed(self, handler: Callable[..., None]) -> int:
        return self.state_changed.add(handler)

    def remove_state_changed(self, token: int) -> None:
        self.state_changed.remove(token)


class FakeAdapter:
    """
    A stand-in for ``BluetoothAdapter``.
    """

    def __init__(self, id: str, address: int, name: str, radio: FakeRadio) -> None:
        self.id = id
        self.name = name
        self.bluetooth_address = address
        self.is_classic_supported = True
        self.radio = radio

        self.vanishing = False
        """
        Whether it is unplugged just after the next listing, before it can be
        opened.
        """

    async def get_radio_async(self) -> FakeRadio:
        return self.radio


class FakeInformation:
    """
    A stand-in for ``DeviceInformation``, as a listing gives it.
    """

    def __init__(self, id: str, name: str, properties: dict[str, object]) -> None:
        self.id = id
        self.name = name
        self.properties = properties


class FakeWatcher:
    """
    A stand-in for ``DeviceWatcher`` over adapters.

    Measured on Windows 10.0.26200.9457: switching the radio off leaves the
    adapter in place, and the watcher says nothing.
    """

    def __init__(self) -> None:
        self.status = DeviceWatcherStatus.CREATED
        self.added = _Handlers()
        self.removed = _Handlers()
        self.updated = _Handlers()

    def add_added(self, handler: Callable[..., None]) -> int:
        return self.added.add(handler)

    def remove_added(self, token: int) -> None:
        self.added.remove(token)

    def add_removed(self, handler: Callable[..., None]) -> int:
        return self.removed.add(handler)

    def remove_removed(self, token: int) -> None:
        self.removed.remove(token)

    def add_updated(self, handler: Callable[..., None]) -> int:
        return self.updated.add(handler)

    def remove_updated(self, token: int) -> None:
        self.updated.remove(token)

    def start(self) -> None:
        self.status = DeviceWatcherStatus.ENUMERATION_COMPLETED

    def stop(self) -> None:
        # Documented to raise for a watcher that is not running.
        assert self.status in (
            DeviceWatcherStatus.STARTED,
            DeviceWatcherStatus.ENUMERATION_COMPLETED,
        )
        self.status = DeviceWatcherStatus.STOPPED

    @property
    def subscribed(self) -> int:
        return len(self.added) + len(self.removed) + len(self.updated)


def protocol_list(channel: int) -> bytes:
    """
    The ProtocolDescriptorList of an RFCOMM record, as it came off the wire
    from BlueZ, with the channel put in.
    """
    return bytes.fromhex("350c35031901003505190003") + bytes([0x08, channel])


class FakeService:
    """
    A stand-in for ``RfcommDeviceService``.
    """

    def __init__(self, service: uuid.UUID, attributes: dict[int, bytes]) -> None:
        self.service_id = RfcommServiceId.from_uuid(service)
        self._attributes = attributes

    async def get_sdp_raw_attributes_with_cache_mode_async(
        self, mode: BluetoothCacheMode
    ) -> dict[int, bytes]:
        return self._attributes


class FakeServicesResult:
    """
    A stand-in for ``RfcommDeviceServicesResult``.
    """

    def __init__(self, error: BluetoothError, services: list[FakeService]) -> None:
        self.error = error
        self.services = services


class FakePeer:
    """
    A remote device, and what Windows knows about it.
    """

    def __init__(self, address: str, name: str, paired: bool) -> None:
        self.address = address
        self.name = name
        self.paired = paired
        self.id = f"Bluetooth#Bluetooth{address.replace(':', '').lower()}"

        self.on = True
        """
        Whether it answers at all.
        """

        self.records: dict[uuid.UUID, dict[int, bytes]] = {}
        """
        The service records it publishes now.
        """

        self.stored: dict[uuid.UUID, dict[int, bytes]] = {}
        """
        The records Windows last read from it.
        """

        self.listening: set[int] = set()
        """
        The channels something on it accepts.
        """

        self.query_error = BluetoothError.SUCCESS
        """
        What an uncached service query fails with, when a test says so.
        """

        self.address_listed = True
        """
        Whether its listing carries its address.
        """

        self.timeouts = 0
        """
        How many connects time out before one gets through, as happens to
        a device that is on.
        """

        self.hold: threading.Event | None = None
        """
        Holds every connect until set.
        """

        self.connected = False
        self.attempts = 0

    def publish(self, service: uuid.UUID, channel: int) -> None:
        """
        Publish a record and listen on its channel.
        """
        self.records[service] = {0x0004: protocol_list(channel)}
        self.listening.add(channel)

    def withdraw(self, service: uuid.UUID) -> None:
        """
        Withdraw a record and stop listening on its channel.
        """
        attributes = self.records.pop(service)
        self.listening.discard(attributes[0x0004][-1])


class FakeDevice:
    """
    A stand-in for ``BluetoothDevice``.
    """

    def __init__(self, peer: FakePeer) -> None:
        self._peer = peer
        self.bluetooth_address = int(peer.address.replace(":", ""), 16)

    @property
    def connection_status(self) -> BluetoothConnectionStatus:
        if self._peer.connected:
            return BluetoothConnectionStatus.CONNECTED
        return BluetoothConnectionStatus.DISCONNECTED

    @staticmethod
    def _services(records: dict[uuid.UUID, dict[int, bytes]]) -> list[FakeService]:
        return [FakeService(s, a) for s, a in records.items()]

    async def get_rfcomm_services_with_cache_mode_async(
        self, mode: BluetoothCacheMode
    ) -> FakeServicesResult:
        peer = self._peer

        if mode == BluetoothCacheMode.CACHED:
            return FakeServicesResult(
                BluetoothError.SUCCESS, self._services(peer.stored)
            )

        if peer.query_error != BluetoothError.SUCCESS:
            return FakeServicesResult(peer.query_error, [])

        # Measured on Windows 10.0.26200.9457: a device that does not answer
        # gives SUCCESS and no services, after about 5 s, and stays
        # disconnected. One that answers is connected afterwards.
        if not peer.on:
            return FakeServicesResult(BluetoothError.SUCCESS, [])

        peer.connected = True
        peer.stored = dict(peer.records)
        return FakeServicesResult(BluetoothError.SUCCESS, self._services(peer.records))

    async def get_rfcomm_services_for_id_with_cache_mode_async(
        self, service: RfcommServiceId, mode: BluetoothCacheMode
    ) -> FakeServicesResult:
        # Only the cached form is used, and it reads what was stored.
        assert mode == BluetoothCacheMode.CACHED
        stored = self._peer.stored
        found = {s: a for s, a in stored.items() if s == service.uuid}
        return FakeServicesResult(BluetoothError.SUCCESS, self._services(found))


class FakeBluetoothSocket(socket.socket):
    """
    A stand-in for a Winsock ``AF_BLUETOOTH`` socket.

    It is one end of a real socket pair, so asyncio carries it once connected.
    The other end stands for the peer.
    """

    def __init__(self, world: "FakeWindows") -> None:
        ours, theirs = socket.socketpair()
        super().__init__(ours.family, ours.type, ours.proto, ours.detach())
        self.world = world
        self.far: socket.socket | None = theirs
        self.channel: int | None = None
        self.reached: tuple[str, int] | None = None
        self.backlog: queue.Queue[tuple[socket.socket, str] | None] = queue.Queue()

    @override
    def connect(self, address: Any, /) -> None:
        self.world.reach(self, *address)

    @override
    def bind(self, address: Any, /) -> None:
        self.world.take_channel(self, address[1])

    @override
    def listen(self, backlog: int = 0, /) -> None:
        pass

    @override
    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        arrived = self.backlog.get()

        if arrived is None:
            # What Windows raises here is not measured, and nothing reads it.
            raise OSError(errno.EBADF, "the listening socket was closed")

        conn, address = arrived
        assert self.channel is not None
        return conn, (address, self.channel)

    @override
    def close(self) -> None:
        self.world.release(self)
        self.backlog.put(None)

        if self.far is not None and self not in self.world.connected:
            self.far.close()

        super().close()


class _Winsock:
    """
    The ``socket`` module as the backend sees it, with Bluetooth sockets
    stood in for.
    """

    def __init__(self, world: "FakeWindows") -> None:
        self._world = world

    def __getattr__(self, name: str) -> object:
        return getattr(socket, name)

    def socket(self, family: int, kind: int, proto: int) -> FakeBluetoothSocket:
        assert (family, kind, proto) == (
            socket.AF_BLUETOOTH,
            socket.SOCK_STREAM,
            socket.BTPROTO_RFCOMM,
        )
        made = FakeBluetoothSocket(self._world)
        self._world.sockets.append(made)
        return made


class _WinsockLibrary:
    """
    A stand-in for ``ws2_32``'s name registration.

    It reads the query set the way Windows does, from raw memory, so a record
    laid out wrongly advertises the wrong channel here too.
    """

    def __init__(self, world: "FakeWindows") -> None:
        self._world = world
        self._last = 0

    def WSASetServiceW(self, query: Any, operation: int, flags: int) -> int:
        found = ctypes.cast(query, ctypes.POINTER(publish._WSAQUERYSET)).contents
        service = uuid.UUID(bytes_le=ctypes.string_at(found.lpServiceClassId, 16))
        where = found.lpcsaBuffer[0].LocalAddr
        raw = ctypes.string_at(where.lpSockaddr, where.iSockaddrLength)
        # SOCKADDR_BTH as Windows declares it: the port is at offset 26.
        channel = int.from_bytes(raw[26:30], "little")
        name = found.lpszServiceInstanceName

        if operation == publish._RNRSERVICE_REGISTER:
            self._world.published[service] = (name, channel)
            return 0

        # Refused with WSAEINVAL even for a query set that registered cleanly,
        # and the record goes all the same, on an unrecorded Windows version.
        self._world.published.pop(service, None)
        self._last = errno.WSAEINVAL
        return -1

    def WSAGetLastError(self) -> int:
        return self._last


class _AdapterStatics:
    def __init__(self, world: "FakeWindows") -> None:
        self._world = world

    def get_device_selector(self) -> str:
        return _ADAPTERS

    async def from_id_async(self, id: str) -> FakeAdapter:
        found = next((a for a in self._world.adapters if a.id == id), None)

        if found is None:
            # Measured on Windows 10.0.26200.9711: an id that no longer
            # resolves fails the operation rather than giving None.
            raise FileNotFoundError(2, "The system cannot find the file specified.")

        return found


class _DeviceStatics:
    def __init__(self, world: "FakeWindows") -> None:
        self._world = world

    def get_device_selector_from_pairing_state(self, paired: bool) -> str:
        assert paired
        return _PAIRED

    async def from_id_async(self, id: str) -> FakeDevice:
        peer = next((p for p in self._world.peers.values() if p.id == id), None)

        if peer is None:
            # Measured on Windows 10.0.26200.9711: never None. An unknown
            # address still gives a device, and a malformed id fails.
            prefix = "Bluetooth#Bluetooth"
            digits = id.removeprefix(prefix)

            if not id.startswith(prefix) or len(digits) != 12:
                raise OSError(
                    22, "The provided device ID is not a valid BluetoothDevice object."
                )

            return await self.from_bluetooth_address_async(int(digits, 16))

        return FakeDevice(peer)

    async def from_bluetooth_address_async(self, address: int) -> FakeDevice:
        text = address.to_bytes(6, "big").hex(":").upper()
        peer = self._world.peers.get(text)

        if peer is None:
            # Measured on Windows 10.0.26200.9457: an address Windows has
            # never seen still gives a device, which then does not answer.
            peer = FakePeer(text, "", paired=False)
            peer.on = False

        return FakeDevice(peer)


class _InformationStatics:
    def __init__(self, world: "FakeWindows") -> None:
        self._world = world

    async def find_all_async_aqs_filter(self, selector: str) -> list[FakeInformation]:
        assert selector == _ADAPTERS
        self._world.check_failure()
        listed = [FakeInformation(a.id, a.name, {}) for a in self._world.adapters]

        for adapter in [a for a in self._world.adapters if a.vanishing]:
            self._world.unplug(adapter)

        return listed

    async def find_all_async_aqs_filter_and_additional_properties(
        self, selector: str, properties: Iterable[str]
    ) -> list[FakeInformation]:
        assert selector == _PAIRED
        assert list(properties) == [ADDRESS_PROPERTY]
        return [
            FakeInformation(
                p.id,
                p.name,
                # Written without separators and in lower case.
                {ADDRESS_PROPERTY: box_string(p.address.replace(":", "").lower())}
                if p.address_listed
                else {},
            )
            for p in self._world.peers.values()
            if p.paired
        ]

    def create_watcher_aqs_filter(self, selector: str) -> FakeWatcher:
        assert selector == _ADAPTERS
        watcher = FakeWatcher()
        self._world.watchers.append(watcher)
        return watcher


class FakeWindows:
    """
    The machine's Bluetooth as WinRT and Winsock report it, and the devices
    around it.
    """

    def __init__(self) -> None:
        self.adapters: list[FakeAdapter] = []
        self.peers: dict[str, FakePeer] = {}
        self.watchers: list[FakeWatcher] = []
        self.sockets: list[FakeBluetoothSocket] = []
        self.connected: list[FakeBluetoothSocket] = []
        self.connecting: set[tuple[str, int]] = set()
        self.bound: dict[int, FakeBluetoothSocket] = {}
        self.published: dict[uuid.UUID, tuple[str, int]] = {}
        self.far_ends: list[socket.socket] = []
        self.failure: OSError | None = None

    # -- test controls -------------------------------------------------

    def plug_in(
        self, address: str = "C4:CB:76:AA:10:C6", state: RadioState = RadioState.ON
    ) -> FakeAdapter:
        """
        Add an adapter, and tell the watchers.
        """
        number = len(self.adapters)
        adapter = FakeAdapter(
            f"adapter{number}",
            int(address.replace(":", ""), 16),
            f"Adapter {number}",
            FakeRadio(state),
        )
        self.adapters.append(adapter)

        for watcher in self.watchers:
            watcher.added.fire(watcher, FakeInformation(adapter.id, adapter.name, {}))

        return adapter

    def unplug(self, adapter: FakeAdapter) -> None:
        """
        Remove an adapter, and tell the watchers.
        """
        self.adapters.remove(adapter)

        for watcher in self.watchers:
            watcher.removed.fire(watcher, None)

    def switch(self, state: RadioState, adapter: int = 0) -> None:
        """
        Switch an adapter's radio, and tell whoever listens to it.
        """
        radio = self.adapters[adapter].radio
        radio.state = state
        radio.state_changed.fire(radio, None)

    def add_peer(
        self,
        address: str = "00:1A:7D:DA:71:0C",
        name: str = "freyr",
        paired: bool = True,
    ) -> FakePeer:
        """
        Add a device in range.
        """
        peer = FakePeer(address, name, paired)
        self.peers[address] = peer
        return peer

    def check_failure(self) -> None:
        if self.failure is not None:
            raise self.failure

    def far_end(self, index: int = -1) -> socket.socket:
        """
        The peer's end of a connected socket.
        """
        far = self.connected[index].far
        assert far is not None
        return far

    def connect_in(self, peer: FakePeer, channel: int) -> socket.socket:
        """
        Have a peer connect to a channel listening here.

        Returns:
            The peer's end of the connection.
        """
        ours, theirs = socket.socketpair()
        self.far_ends.append(theirs)
        self.bound[channel].backlog.put((ours, peer.address))
        return theirs

    # -- what the stand-ins share --------------------------------------

    def reach(self, sock: FakeBluetoothSocket, address: str, channel: int) -> None:
        """
        Connect a socket to a peer's channel, or fail the way Winsock does.
        """
        # Measured on Windows 10.0.26200.9711 against BlueZ 5.85: a second
        # connect to a channel fails at once while the first is going.
        if (address, channel) in self.connecting:
            raise winsock_error(WSAEADDRINUSE)

        self.connecting.add((address, channel))

        try:
            self._carry_out(sock, address, channel)
        finally:
            self.connecting.discard((address, channel))

    def _carry_out(self, sock: FakeBluetoothSocket, address: str, channel: int) -> None:
        """
        Connect a socket, with no other connect to its channel going.
        """
        peer = self.peers.get(address)

        if peer is not None:
            peer.attempts += 1

            if peer.hold is not None:
                peer.hold.wait()

        if sock.fileno() == -1:
            # Closed while connecting. Not measured, and nothing reads it.
            raise OSError(errno.EBADF, "the socket was closed")

        # Measured on Windows 10.0.26200.9457 against macOS 26.6.2, and on
        # 10.0.26200.9711 against BlueZ 5.85: a channel nothing listens on is
        # refused with WSAEHOSTDOWN, and a device that is off times out.
        if peer is None or not peer.on:
            raise winsock_error(WSAETIMEDOUT)

        if peer.timeouts:
            peer.timeouts -= 1
            raise winsock_error(WSAETIMEDOUT)

        if channel not in peer.listening:
            raise winsock_error(WSAEHOSTDOWN)

        peer.connected = True
        sock.reached = (address, channel)
        self.connected.append(sock)

    def take_channel(self, sock: FakeBluetoothSocket, channel: int) -> None:
        """
        Bind a listening socket, or refuse a channel that is taken.
        """
        if channel in self.bound:
            raise OSError(errno.EADDRINUSE, "Only one usage of each socket address")

        self.bound[channel] = sock
        sock.channel = channel

    def release(self, sock: FakeBluetoothSocket) -> None:
        if sock.channel is not None and self.bound.get(sock.channel) is sock:
            del self.bound[sock.channel]

    def close(self) -> None:
        """
        Close the peers' ends of every connection.
        """
        for far in self.far_ends:
            far.close()

        for sock in self.sockets:
            if sock.far is not None:
                sock.far.close()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Put the stand-ins where the backend looks for WinRT and Winsock.
        """
        adapters = _AdapterStatics(self)
        found = _DeviceStatics(self)
        information = _InformationStatics(self)
        winsock = _Winsock(self)

        monkeypatch.setattr(windows, "BluetoothAdapter", adapters)
        monkeypatch.setattr(windows, "BluetoothDevice", found)
        monkeypatch.setattr(windows, "DeviceInformation", information)
        monkeypatch.setattr(windows, "socket", winsock)
        monkeypatch.setattr(availability, "BluetoothAdapter", adapters)
        monkeypatch.setattr(availability, "DeviceInformation", information)
        monkeypatch.setattr(devices, "BluetoothDevice", found)
        monkeypatch.setattr(connection, "socket", winsock)
        monkeypatch.setattr(publish, "_ws2", _WinsockLibrary(self))
