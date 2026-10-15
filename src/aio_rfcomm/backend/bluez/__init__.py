# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The Linux backend, talking to BlueZ over D-Bus.

No Bluetooth socket is created to reach a service, which is deliberate. CPython
only compiles ``AF_BLUETOOTH`` support when BlueZ headers are present at build
time, and because those are GPL the widely used ``python-build-standalone``
builds -- the ones ``uv`` installs -- ship without it. BlueZ hands back an
already-connected file descriptor instead, so connecting by service UUID works
on a Python that has never heard of Bluetooth.

Two things are better with a socket, and both degrade rather than fail without
one: connecting to a bare channel number, which cannot be done over D-Bus at
all (:meth:`BlueZBluetooth.open_channel`), and choosing the channel a service
listens on, which falls back to a guess (:meth:`BlueZBluetooth._reserve_channel`).
"""

# Do not add ``from __future__ import annotations`` to this module. dbus-fast
# reads the annotations on the Profile1 methods below at runtime to work out
# the D-Bus signature, and under PEP 563 an annotation is stored as its own
# source text rather than the object, so the lookup would fail.

import asyncio
import errno
import itertools
import logging
import os
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import NamedTuple, TypeVar, cast
from uuid import UUID

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface
from dbus_fast.annotations import DBusDict, DBusObjectPath, DBusUnixFd
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method
from typing_extensions import override

from aio_rfcomm._teardown import unwrap_lone_error
from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    BackendRequest,
    BackendService,
    NoBluetooth,
)
from aio_rfcomm.backend.stream import StreamChannel, StreamProtocol, open_stream
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    ChannelInUseError,
    CloseReason,
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
    UnsupportedOperationError,
)

__all__ = ["BlueZBackend"]

logger = logging.getLogger(__name__)

_SERVICE = "org.bluez"
_ADAPTER = "org.bluez.Adapter1"
_DEVICE = "org.bluez.Device1"
_PROFILE_MANAGER = "org.bluez.ProfileManager1"
_OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"

# Object paths are scoped to a bus connection, so this only has to be unique
# within one process; two programs may export the same path without clashing.
_profile_paths = itertools.count()

_Interface = TypeVar("_Interface", bound=ProxyInterface)

_ManagedObjects = dict[str, dict[str, dict[str, Variant]]]


# --------------------------------------------------------------------------
# Typed views of the BlueZ interfaces we use
#
# dbus-fast builds proxy methods at runtime from introspection, so a plain
# proxy has nothing a type checker can see. Declaring the handful we call
# keeps the call sites checked rather than littered with ignores.
# --------------------------------------------------------------------------


class _ObjectManagerProxy(ProxyInterface):
    async def call_get_managed_objects(self) -> _ManagedObjects: ...


class _ProfileManagerProxy(ProxyInterface):
    async def call_register_profile(
        self, profile: str, uuid: str, options: dict[str, Variant]
    ) -> None: ...

    async def call_unregister_profile(self, profile: str) -> None: ...


class _DeviceProxy(ProxyInterface):
    async def call_connect_profile(self, uuid: str) -> None: ...


class _Profile(ServiceInterface):
    """
    The object BlueZ calls back with a connected file descriptor.

    One of these is shared by every connection attempt for a service UUID,
    because BlueZ allows only one registration per UUID; see :class:`_Profiles`.
    Callbacks are routed to whoever is waiting on that device.
    """

    def __init__(self) -> None:
        super().__init__("org.bluez.Profile1")
        self._waiting: dict[str, list[asyncio.Future[int]]] = {}

    def expect(self, device_path: str) -> asyncio.Future[int]:
        """
        Register interest in the next connection from a device.

        Args:
            device_path: The device's object path.

        Returns:
            A future resolved with the connected file descriptor.
        """
        waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(device_path, []).append(waiter)
        return waiter

    def forget(self, device_path: str, waiter: asyncio.Future[int]) -> None:
        """
        Drop a waiter that is no longer interested.

        Args:
            device_path: The device's object path.
            waiter: The future returned by :meth:`expect`.
        """
        waiters = self._waiting.get(device_path)
        if waiters and waiter in waiters:
            waiters.remove(waiter)
            if not waiters:
                del self._waiting[device_path]

    @method()
    def Release(self) -> None:
        # BlueZ drops the registration, which for us only happens because we
        # asked it to. A channel already handed over stays open and is closed
        # by whoever owns it, so there is nothing to undo here.
        logger.debug("BlueZ released the profile")

    @method()
    def NewConnection(
        self, device: DBusObjectPath, fd: DBusUnixFd, properties: DBusDict
    ) -> None:
        waiters = self._waiting.get(device)
        if not waiters:
            # Nobody is waiting: the attempt was cancelled just before BlueZ
            # answered, or a peer connected unprompted. Either way the
            # descriptor is ours to close, and dropping it silently would leak
            # it.
            logger.debug("unexpected connection from %s; closing fd %d", device, fd)
            os.close(fd)
            return
        waiters.pop(0).set_result(fd)

    @method()
    def RequestDisconnection(self, device: DBusObjectPath) -> None:
        # BlueZ asking us to hang up. The channel's own scope is what closes
        # it, and closing the descriptor here would pull it out from under
        # whoever is still using it, so this only records the request.
        logger.debug("BlueZ asked us to disconnect %s", device)


class _Profiles:
    """
    One Profile1 registration per service UUID, shared and reference counted.

    BlueZ rejects a second registration of a UUID with "UUID already
    registered", so a profile cannot be registered per connection attempt.
    Concurrent connects to one service share a registration, which is dropped
    when the last of them has finished with it.
    """

    def __init__(
        self,
        bus: MessageBus,
        manager: Callable[[], Awaitable[_ProfileManagerProxy]],
    ) -> None:
        """
        Args:
            bus: Where profiles are exported.
            manager: Gets BlueZ's profile manager. Asked for each time rather
                than kept, so a restarted BlueZ is found.
        """
        self._bus = bus
        self._manager = manager
        self._lock = asyncio.Lock()
        self._registered: dict[str, tuple[str, _Profile, int]] = {}

    @asynccontextmanager
    async def acquire(self, uuid: str) -> AsyncGenerator[_Profile, None]:
        """
        Borrow the profile for a UUID, registering it if nobody has yet.

        Args:
            uuid: The service UUID.

        Yields:
            The shared profile object.

        Raises:
            ConnectionFailedError: Another program already holds this UUID.
        """
        manager = await self._manager()

        async with self._lock:
            existing = self._registered.get(uuid)
            if existing is None:
                path = f"/org/aio_rfcomm/profile{next(_profile_paths)}"
                profile = _Profile()
                self._bus.export(path, profile)
                try:
                    await manager.call_register_profile(
                        path,
                        uuid,
                        {
                            "Role": Variant("s", "client"),
                            "AutoConnect": Variant("b", False),
                        },
                    )
                except Exception as error:
                    self._bus.unexport(path)
                    raise _translate_registration(error, uuid) from error
                self._registered[uuid] = (path, profile, 1)
            else:
                path, profile, count = existing
                self._registered[uuid] = (path, profile, count + 1)

        try:
            yield profile
        finally:
            async with self._lock:
                path, profile, count = self._registered[uuid]
                if count > 1:
                    self._registered[uuid] = (path, profile, count - 1)
                else:
                    del self._registered[uuid]
                    try:
                        await manager.call_unregister_profile(path)
                    finally:
                        self._bus.unexport(path)


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
    async def explain_end(self) -> CloseReason:
        """
        Make sure a radio going off is known before the end is put down to
        the peer.

        Measured on BlueZ 5.72: switching the radio off ends the channel about
        50 ms before the adapter is reported off, while its power state
        already reads as going off. So the adapter is read again here, and if
        Bluetooth is no longer usable that is reported first, and becomes why
        the channel ended.

        Returns:
            Why the channel went away, as far as the socket can tell.
        """
        try:
            await self._refresh()
        except Exception:
            # The end still has to be explained; the reason is only less sure.
            logger.debug("could not read whether Bluetooth is usable", exc_info=True)
        return await super().explain_end()


class _ServerProfile(ServiceInterface):
    """
    The object BlueZ calls back with each peer that connects to a service.

    Unlike the client profile, nothing is waiting for a particular device, so
    connections go into a queue for whoever asks next. The queue is unbounded
    because refusing a peer that BlueZ has already accepted would mean closing
    a connection the peer believes in.
    """

    def __init__(self) -> None:
        super().__init__("org.bluez.Profile1")
        self._arrived: asyncio.Queue[tuple[str, int]] = asyncio.Queue()

    async def next_connection(self) -> tuple[str, int]:
        """
        Wait for the next peer.

        Returns:
            The peer's object path and the connected file descriptor.
        """
        return await self._arrived.get()

    def discard_queued(self) -> None:
        """
        Close any connection nobody took.

        Called when the service is withdrawn. These descriptors belong to
        peers that connected and were never handed to a handler, so closing
        them is the only correct end.
        """
        while True:
            try:
                _, fd = self._arrived.get_nowait()
            except asyncio.QueueEmpty:
                return
            logger.debug("closing fd %d, queued but never taken", fd)
            os.close(fd)

    @method()
    def Release(self) -> None:
        # BlueZ dropping a server registration, which for us only happens
        # because we asked it to when the service was withdrawn.
        logger.debug("BlueZ released the server profile")

    @method()
    def NewConnection(
        self, device: DBusObjectPath, fd: DBusUnixFd, properties: DBusDict
    ) -> None:
        self._arrived.put_nowait((device, fd))

    @method()
    def RequestDisconnection(self, device: DBusObjectPath) -> None:
        # The handler's own block is what closes a served channel, and closing
        # the descriptor here would pull it out from under a handler that is
        # still using it.
        logger.debug("BlueZ asked us to disconnect %s", device)


class BlueZService(BackendService):
    """
    A service published through BlueZ, listening for peers.
    """

    def __init__(
        self,
        profile: _ServerProfile,
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
            os.close(fd)


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


class BlueZBluetooth(BackendBluetooth):
    """
    Bluetooth through BlueZ, on one adapter, with a bus connection of its own.
    """

    def __init__(self, bus: MessageBus, profiles: _Profiles) -> None:
        self._bus = bus
        self._profiles = profiles
        self._claimed: set[int] = set()

        self.path = ""
        """
        The adapter's object path, kept up to date as adapters come and go.
        """

        self.address: str | None = None
        """
        The adapter's own address, if BlueZ reports one.
        """

        self.refresh: Callable[[], Awaitable[None]] = _nothing
        """
        Reads whether Bluetooth is usable and reports it; set once there is
        somewhere to report it.
        """

    async def _interface(
        self, path: str, name: str, kind: type[_Interface]
    ) -> _Interface:
        """
        Get one interface of one object, typed.

        Args:
            path: The object path.
            name: The interface name.
            kind: The declared view of that interface.

        Returns:
            The interface.
        """
        introspection = await self._bus.introspect(_SERVICE, path)
        proxy = self._bus.get_proxy_object(_SERVICE, path, introspection)
        return cast(_Interface, proxy.get_interface(name))

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        wanted = _wanted(service)
        # REVISIT: GetManagedObjects walks every object BlueZ knows and is
        # expensive to call repeatedly. bleak keeps one manager running and
        # follows change signals instead; worth doing here if listing turns
        # out to be called often.
        manager = await self._interface("/", _OBJECT_MANAGER, _ObjectManagerProxy)
        objects = await manager.call_get_managed_objects()

        found: list[RfcommDeviceInfo] = []
        for path, interfaces in sorted(objects.items()):
            properties = interfaces.get(_DEVICE)
            if properties is None:
                continue
            # Ask the device which adapter it belongs to rather than inferring
            # it from the shape of the object path. There is no sensible
            # default to compare against -- an empty string is not a valid
            # object path -- so an absent property just means no match.
            belongs_to = properties.get("Adapter")
            if belongs_to is None or belongs_to.value != self.path:
                continue
            uuids = {
                u.lower() for u in properties.get("UUIDs", Variant("as", [])).value
            }
            if wanted and not wanted & uuids:
                continue
            found.append(
                RfcommDeviceInfo(
                    properties["Address"].value,
                    properties.get("Alias", Variant("s", "")).value or None,
                    path,
                )
            )
        return found

    @override
    def open_service(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, service)

    @override
    def open_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect_by_channel(device, channel)

    @override
    def serve(
        self, service: UUID, *, name: str, channel: int | None = None
    ) -> AbstractAsyncContextManager[BackendService]:
        return self._serve(service, name, channel)

    def _reserve_channel(self, channel: int | None) -> int:
        """
        Settle which channel to listen on before registering the profile.

        BlueZ neither picks a channel for a server nor complains when the one
        it is given is already taken, so the choice has to be made and checked
        here. An RFCOMM socket bound to the adapter's own address is refused
        with ``EADDRINUSE`` when a profile already holds that channel, and
        binding channel 0 has the kernel pick a free one -- so a socket, used
        only as a probe and closed again at once, answers both questions.

        Binding ``BDADDR_ANY`` instead would not: it succeeds on a channel a
        profile holds, and so would report every channel free.

        Without a socket there is no probe and no safe guess, so the caller
        has to name a channel; see :meth:`_unprobed_channel`.

        The socket is closed before the profile is registered, because BlueZ
        wants to listen on that channel itself and cannot while we hold it.
        That leaves a moment in which another program could take the channel,
        which is not worth avoiding: closing the window entirely would mean
        never letting BlueZ listen at all.

        Args:
            channel: The channel the caller asked for, or ``None`` for any.

        Returns:
            The channel to register on.

        Raises:
            ChannelInUseError: The requested channel is taken, or there is
                none free.
            UnsupportedOperationError: No channel was named and this Python
                cannot probe for one.
        """
        if not hasattr(socket, "AF_BLUETOOTH") or self.address is None:
            return self._unprobed_channel(channel)

        probe = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
        )
        try:
            try:
                probe.bind((self.address, channel or 0))
                # The kernel does not settle on a channel until the socket is
                # listening, so binding channel 0 and reading the name back
                # without this returns 0 rather than the channel it picked.
                probe.listen(1)
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    raise ChannelInUseError(
                        f"channel {channel} is already in use on {self.address}"
                        if channel is not None
                        else "every RFCOMM channel on this adapter is in use"
                    ) from error
                raise ConnectionFailedError(
                    f"could not reserve a channel on {self.address}: {error}"
                ) from error
            return probe.getsockname()[1]
        finally:
            probe.close()

    def _unprobed_channel(self, channel: int | None) -> int:
        """
        Settle on a channel with no socket to check it against.

        The ``python-build-standalone`` builds -- the ones ``uv`` installs --
        have no ``AF_BLUETOOTH``, so on those nothing can be probed. A channel
        the caller named is taken at their word and used unchecked. Choosing
        one here is refused instead of guessed: BlueZ says nothing when a
        registration lands on a channel another profile holds, so a wrong
        guess would publish a service that simply never appears, which is
        worse than saying up front that the choice cannot be made.

        Args:
            channel: The channel the caller asked for, or ``None`` for any.

        Returns:
            The channel to register on.

        Raises:
            ChannelInUseError: This process is already serving on it.
            UnsupportedOperationError: No channel was named and none can be
                chosen.
        """
        if channel is None:
            raise UnsupportedOperationError(
                "choosing a channel to serve on needs a Python built with "
                "Bluetooth socket support, and this one has none. BlueZ neither "
                "picks a channel for a server nor reports which are free, and a "
                "registration that collides is published as nothing at all. "
                "Pass an explicit channel, or use a Python built against "
                "libbluetooth."
            )
        if channel in self._claimed:
            raise ChannelInUseError(
                f"this process is already serving on channel {channel}"
            )
        return channel

    @asynccontextmanager
    async def _serve(
        self, service: UUID, name: str, channel: int | None
    ) -> AsyncGenerator[BackendService, None]:
        """
        Publish a service and listen for peers.

        BlueZ will not publish a record unless it is told which channel to
        listen on: registering without one succeeds and quietly advertises
        nothing, which was measured rather than assumed. So a channel is
        always settled here -- see :meth:`_reserve_channel` -- and always
        reported back.
        """
        channel = self._reserve_channel(channel)

        path = f"/org/aio_rfcomm/server{next(_profile_paths)}"
        profile = _ServerProfile()
        manager = await self._interface(
            "/org/bluez", _PROFILE_MANAGER, _ProfileManagerProxy
        )

        self._bus.export(path, profile)
        self._claimed.add(channel)
        try:
            try:
                await manager.call_register_profile(
                    path,
                    str(service),
                    {
                        "Role": Variant("s", "server"),
                        "Name": Variant("s", name),
                        "Channel": Variant("q", channel),
                        "RequireAuthentication": Variant("b", False),
                        "RequireAuthorization": Variant("b", False),
                    },
                )
            except Exception as error:
                raise _translate_registration(error, str(service)) from error

            try:
                yield BlueZService(profile, channel, self.refresh)
            finally:
                await manager.call_unregister_profile(path)
        finally:
            self._claimed.discard(channel)
            self._bus.unexport(path)
            profile.discard_queued()

    def _device_path(self, device: RfcommDeviceInfo) -> str:
        """
        Find the object path for a device.

        Args:
            device: The device.

        Returns:
            The device's object path, taken from the description where it came
            from this backend and rebuilt from the address otherwise.
        """
        if device.id is not None:
            return device.id
        return f"{self.path}/dev_{device.address.replace(':', '_')}"

    @asynccontextmanager
    async def _connect(
        self, device: RfcommDeviceInfo, service: UUID
    ) -> AsyncGenerator[BackendChannel, None]:
        address = device.address
        device_path = self._device_path(device)
        uuid = str(service)

        async with AsyncExitStack() as stack:
            profile = await stack.enter_async_context(self._profiles.acquire(uuid))

            waiter = profile.expect(device_path)
            stack.callback(profile.forget, device_path, waiter)

            try:
                remote = await self._interface(device_path, _DEVICE, _DeviceProxy)
            except Exception as error:
                raise DeviceNotFoundError(address) from error

            try:
                await remote.call_connect_profile(uuid)
            except Exception as error:
                raise _translate_connect(error, address, uuid) from error

            fd = await waiter
            sock = socket.socket(fileno=fd)
            transport, protocol = await open_stream(sock)
            opened = BlueZChannel(transport, protocol, self.refresh)
            stack.push_async_callback(opened.close)

            yield opened

    @asynccontextmanager
    async def _connect_by_channel(
        self, device: RfcommDeviceInfo, channel: int
    ) -> AsyncGenerator[BackendChannel, None]:
        """
        Connect straight to a channel number, over a Bluetooth socket.

        BlueZ offers no way to do this over D-Bus. ``ConnectProfile`` is its
        only route, and that looks the service UUID up in what the device
        advertises before it considers anything else; pinning ``Channel`` on
        the profile registration does not change that, which was measured
        rather than assumed. So this needs a real socket, and a Python built
        without Bluetooth support cannot do it at all.
        """
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise UnsupportedOperationError(
                "connecting to a channel number needs a Python built with "
                "Bluetooth socket support, and this one has none. BlueZ offers "
                "no way to do it over D-Bus. Connect by service UUID instead, "
                "or use a Python built against libbluetooth."
            )

        address = device.address
        async with AsyncExitStack() as stack:
            sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            stack.callback(sock.close)

            # Which adapter a socket goes out of is chosen by binding to that
            # adapter's own address; left unbound the kernel picks for itself,
            # which would quietly ignore the adapter in use.
            if self.address is not None:
                try:
                    sock.bind((self.address, 0))
                except OSError as error:
                    raise ConnectionFailedError(
                        f"could not use adapter {self.address}: {error}"
                    ) from error

            try:
                await asyncio.get_running_loop().sock_connect(sock, (address, channel))
            except OSError as error:
                raise _translate_socket_connect(error, address, channel) from error

            transport, protocol = await open_stream(sock)
            opened = BlueZChannel(transport, protocol, self.refresh)
            stack.push_async_callback(opened.close)

            yield opened


class BlueZBackend(BackendProvider):
    """
    The Linux implementation.
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
        Connect to the system bus, and follow BlueZ and its adapters on it.

        Args:
            state: Where to report availability.

        Yields:
            Bluetooth through BlueZ.
        """
        async with AsyncExitStack() as stack:
            try:
                # A connected channel arrives as a Unix file descriptor, so
                # passing them has to be negotiated when the connection is made.
                bus = await stack.enter_async_context(_bus(negotiate_unix_fd=True))
            except (OSError, DBusError, ValueError) as error:
                # No system bus at all: a container, say, with nothing to reach.
                state.set(
                    Availability.UNSUPPORTED, f"could not reach the system bus: {error}"
                )
                bus = None

            if bus is None:
                # Outside the except, so that what the caller's block raises is
                # not reported as happening while handling this.
                yield NoBluetooth()
                return

            bluetooth = BlueZBluetooth(
                bus,
                _Profiles(
                    bus,
                    lambda: _get(
                        bus, "/org/bluez", _PROFILE_MANAGER, _ProfileManagerProxy
                    ),
                ),
            )
            changed = asyncio.Event()

            def notice(message: Message) -> None:
                if message.message_type is MessageType.SIGNAL:
                    changed.set()

            bluetooth.refresh = lambda: _read(bus, bluetooth, state)
            bus.add_message_handler(notice)
            stack.callback(bus.remove_message_handler, notice)
            for rule in _WATCHED:
                await _add_match(bus, rule)

            with unwrap_lone_error():
                async with asyncio.TaskGroup() as group:
                    following = group.create_task(
                        _follow(bus, bluetooth, state, changed)
                    )
                    try:
                        yield bluetooth
                    finally:
                        following.cancel()


async def _nothing() -> None:
    pass


# What the bus answers when nothing owns the name asked for: BlueZ is not
# running, and nothing has started it.
_NOBODY_THERE = (
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
)


# What can change whether Bluetooth is usable: an adapter's properties, which
# include whether it is powered; adapters arriving and leaving; and BlueZ
# itself starting and stopping. The first was measured against a radio being
# switched off and on (BlueZ 5.72); the others are only documented.
_WATCHED = (
    (
        "type='signal',sender='org.bluez',"
        "interface='org.freedesktop.DBus.Properties',"
        "member='PropertiesChanged',arg0='org.bluez.Adapter1'"
    ),
    "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager'",
    (
        "type='signal',sender='org.freedesktop.DBus',"
        "interface='org.freedesktop.DBus',"
        "member='NameOwnerChanged',arg0='org.bluez'"
    ),
)


async def _add_match(bus: MessageBus, rule: str) -> None:
    """
    Ask the bus to deliver the signals a rule matches.

    Args:
        bus: A connected system bus.
        rule: The match rule.
    """
    reply = await bus.call(
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch",
            signature="s",
            body=[rule],
        )
    )
    if reply is None or reply.message_type is MessageType.ERROR:
        # Not fatal: Bluetooth still works, but changes to it go unnoticed.
        logger.warning("the system bus refused %s: %s", rule, reply and reply.body)


async def _follow(
    bus: MessageBus,
    bluetooth: BlueZBluetooth,
    state: AvailabilityState,
    changed: asyncio.Event,
) -> None:
    """
    Read whether Bluetooth is usable, and again after anything that may have
    changed it.

    Everything is read afresh each time rather than worked out from what the
    signal said, so that a signal missed or misread costs nothing beyond its
    own moment.

    Args:
        bus: A connected system bus.
        bluetooth: Where the adapter in use is kept.
        state: Where to report availability.
        changed: Set by every signal that may matter.
    """
    while True:
        changed.clear()
        try:
            await _read(bus, bluetooth, state)
        except Exception as error:
            # A failed read says nothing about Bluetooth, so what was last
            # known stands until the next signal. With nothing known yet,
            # opening Bluetooth would wait for ever.
            logger.exception("could not read whether Bluetooth is usable")

            if not state.is_known:
                state.set(
                    Availability.SERVICE_UNAVAILABLE,
                    f"could not read whether Bluetooth is usable: {error}",
                )
        await changed.wait()


class _Adapter(NamedTuple):
    path: str
    address: str | None
    name: str | None
    powered: bool
    power_state: str | None


async def _list_adapters(bus: MessageBus) -> list[_Adapter]:
    """
    Read every adapter BlueZ knows about.

    Args:
        bus: A connected system bus.

    Returns:
        The adapters, in object path order.
    """
    manager = await _get(bus, "/", _OBJECT_MANAGER, _ObjectManagerProxy)
    objects = await manager.call_get_managed_objects()

    return [
        _Adapter(
            path,
            interfaces[_ADAPTER].get("Address", Variant("s", "")).value or None,
            interfaces[_ADAPTER].get("Alias", Variant("s", "")).value or None,
            bool(interfaces[_ADAPTER].get("Powered", Variant("b", False)).value),
            # Newer than Powered, so not always there.
            _optional_text(interfaces[_ADAPTER].get("PowerState")),
        )
        for path, interfaces in sorted(objects.items())
        if _ADAPTER in interfaces
    ]


def _powered(adapter: _Adapter) -> tuple[Availability, str | None]:
    """
    Say whether an adapter's radio is usable.

    Measured on BlueZ 5.72: switching off goes through ``on-disabling`` for
    about 50 ms while ``Powered`` still reads true, and the radio's links drop
    during it. So only ``on`` counts, where BlueZ reports a power state.

    Args:
        adapter: The adapter, as last read.

    Returns:
        Its availability, and what to tell the user about it.
    """
    name = adapter.name or adapter.path
    match adapter.power_state:
        case None if adapter.powered:
            return Availability.USABLE, None
        case "on":
            return Availability.USABLE, None
        case "off-blocked":
            return (
                Availability.SWITCHED_OFF,
                f"{name} is blocked, by rfkill or airplane mode; unblock it",
            )
        case _:
            return (
                Availability.SWITCHED_OFF,
                f"{name} is switched off; turn Bluetooth on",
            )


async def _read(
    bus: MessageBus, bluetooth: BlueZBluetooth, state: AvailabilityState
) -> None:
    """
    Read whether Bluetooth is usable, and which adapter to use.

    Args:
        bus: A connected system bus.
        bluetooth: Where the adapter in use is kept.
        state: Where to report availability.
    """
    try:
        adapters = await _list_adapters(bus)
    except DBusError as error:
        if error.type not in _NOBODY_THERE:
            raise
        state.set(Availability.SERVICE_UNAVAILABLE, f"BlueZ is not running: {error}")
        return

    if not adapters:
        state.set(Availability.NO_ADAPTER)
        return

    # The one in use is kept while it is there; otherwise the first.
    # REVISIT: a machine with several radios should be able to name one.
    chosen = next((a for a in adapters if a.path == bluetooth.path), adapters[0])
    bluetooth.path = chosen.path
    bluetooth.address = chosen.address

    state.set(*_powered(chosen))


@asynccontextmanager
async def _bus(*, negotiate_unix_fd: bool) -> AsyncGenerator[MessageBus, None]:
    """
    A system bus connection, always closed again.

    dbus-fast can leave a connection's resources behind if ``connect()`` fails
    part way through, so the disconnect runs on that path too.

    Args:
        negotiate_unix_fd: Whether file descriptor passing is needed.

    Yields:
        The connected bus.
    """
    bus = MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=negotiate_unix_fd)
    try:
        await bus.connect()
    except BaseException:
        bus.disconnect()
        raise

    try:
        yield bus
    finally:
        bus.disconnect()
        await bus.wait_for_disconnect()


async def _get(
    bus: MessageBus, path: str, name: str, kind: type[_Interface]
) -> _Interface:
    """
    Get one interface of one object, typed.

    Args:
        bus: A connected system bus.
        path: The object path.
        name: The interface name.
        kind: The declared view of that interface.

    Returns:
        The interface.
    """
    introspection = await bus.introspect(_SERVICE, path)
    proxy = bus.get_proxy_object(_SERVICE, path, introspection)
    return cast(_Interface, proxy.get_interface(name))


def _wanted(service: UUID | Collection[UUID] | None) -> set[str]:
    """
    Normalise the service filter to a set of lower-case UUID strings.

    Args:
        service: One service, several, or ``None`` for no filter.

    Returns:
        The UUIDs to match, empty for no filter.
    """
    if service is None:
        return set()
    if isinstance(service, UUID):
        return {str(service).lower()}
    return {str(u).lower() for u in service}


def _optional_text(value: Variant | None) -> str | None:
    """
    Args:
        value: A property that may be missing.

    Returns:
        Its text, or ``None`` if it is missing.
    """
    return None if value is None else str(value.value)


def _translate_registration(error: Exception, uuid: str) -> RfcommError:
    """
    Turn a failure to register a profile into one of ours.

    Args:
        error: What BlueZ raised.
        uuid: The service UUID being registered.

    Returns:
        The error to raise instead.
    """
    if "already registered" in str(error):
        return ConnectionFailedError(
            f"another program on this machine has already claimed {uuid}. "
            "BlueZ allows only one profile registration per service UUID."
        )
    return ConnectionFailedError(f"could not register a profile for {uuid}: {error}")


def _translate_connect(error: Exception, address: str, uuid: str) -> RfcommError:
    """
    Turn a BlueZ connection error into one of ours.

    Args:
        error: What BlueZ raised.
        address: The device being connected to.
        uuid: The service being connected to.

    Returns:
        The error to raise instead.
    """
    text = str(error)
    if "profile-unavailable" in text or "NotAvailable" in text:
        return ServiceNotFoundError(f"{address} does not offer {uuid}")
    if "page-timeout" in text or "NotReady" in text or "Host is down" in text:
        return DeviceNotFoundError(f"{address} did not answer")
    return ConnectionFailedError(f"could not open a channel to {address}: {text}")


def _translate_socket_connect(
    error: OSError, address: str, channel: int
) -> RfcommError:
    """
    Turn a failed RFCOMM socket connect into one of ours.

    Args:
        error: What ``connect`` raised.
        address: The device being connected to.
        channel: The channel number.

    Returns:
        The error to raise instead.
    """
    match error.errno:
        case errno.ECONNREFUSED:
            # Any refusal reads the same; nothing listening is the one measured
            # (5.72).
            return ServiceNotFoundError(
                f"{address} refused channel {channel}; usually nothing there is "
                "listening on it"
            )
        case _:
            return ConnectionFailedError(
                f"could not reach {address} on channel {channel}: {error}"
            )
