# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The Linux backend, talking to BlueZ over D-Bus.

No Bluetooth socket is created to reach a service, which is deliberate. CPython
only compiles ``AF_BLUETOOTH`` support when BlueZ headers are present at build
time, and because those are GPL the widely used ``python-build-standalone``
builds -- the ones ``uv`` installs -- ship without it. BlueZ hands back an
already-connected file descriptor instead, so connecting by service UUID works
on a Python that has never heard of Bluetooth. Connecting to a bare channel
number is the one thing that still needs a real socket; see
:meth:`BlueZAdapter.open_channel`.
"""

# Do not add ``from __future__ import annotations`` to this module. dbus-fast
# reads the annotations on the Profile1 methods below at runtime to work out
# the D-Bus signature, and under PEP 563 an annotation is stored as its own
# source text rather than the object, so the lookup would fail.

import asyncio
import itertools
import logging
import os
import socket
from collections.abc import AsyncGenerator, Collection
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import TypeVar, cast
from uuid import UUID

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface
from dbus_fast.annotations import DBusDict, DBusObjectPath, DBusUnixFd
from dbus_fast.service import ServiceInterface, method
from typing_extensions import override

from aio_rfcomm.backend.provider import BackendAdapter, BackendChannel, BackendProvider
from aio_rfcomm.backend.stream import StreamChannel
from aio_rfcomm.discovery import RfcommAdapterInfo, RfcommDeviceInfo
from aio_rfcomm.errors import (
    AdapterNotFoundError,
    AdapterOffError,
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


class _AdapterProxy(ProxyInterface):
    async def get_powered(self) -> bool: ...


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

    def __init__(self, bus: MessageBus, manager: _ProfileManagerProxy) -> None:
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
        async with self._lock:
            existing = self._registered.get(uuid)
            if existing is None:
                path = f"/org/aio_rfcomm/profile{next(_profile_paths)}"
                profile = _Profile()
                self._bus.export(path, profile)
                try:
                    await self._manager.call_register_profile(
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
                    await self._manager.call_unregister_profile(path)
                    self._bus.unexport(path)


class BlueZChannel(StreamChannel):
    """
    An RFCOMM channel over the file descriptor BlueZ supplied.

    Nothing to add: BlueZ gives us a connected socket and the shared stream
    channel does the rest. It stays a class of its own so that a backtrace
    names the platform.
    """


class BlueZAdapter(BackendAdapter):
    """
    One BlueZ adapter, with a bus connection of its own.
    """

    def __init__(
        self, bus: MessageBus, info: RfcommAdapterInfo, profiles: _Profiles
    ) -> None:
        super().__init__()
        self._bus = bus
        self._path = info.id
        self._address = info.address
        self._profiles = profiles

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
            if belongs_to is None or belongs_to.value != self._path:
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
        self, device: RfcommDeviceInfo | str, service: UUID
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect(device, service)

    @override
    def open_channel(
        self, device: RfcommDeviceInfo | str, channel: int
    ) -> AbstractAsyncContextManager[BackendChannel]:
        return self._connect_by_channel(device, channel)

    def _device_path(self, device: RfcommDeviceInfo | str) -> str:
        """
        Find the object path for a device.

        Args:
            device: A description from :meth:`list_known_devices`, or an
                address.

        Returns:
            The device's object path, taken from the description where it came
            from this backend and rebuilt from the address otherwise.
        """
        if isinstance(device, RfcommDeviceInfo) and device.id is not None:
            return device.id
        address = device if isinstance(device, str) else device.address
        return f"{self._path}/dev_{address.upper().replace(':', '_')}"

    @asynccontextmanager
    async def _connect(
        self, device: RfcommDeviceInfo | str, service: UUID
    ) -> AsyncGenerator[BackendChannel, None]:
        address = device if isinstance(device, str) else device.address
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
            stack.callback(sock.close)
            reader, writer = await asyncio.open_connection(sock=sock)
            stack.callback(writer.close)

            yield BlueZChannel(reader, writer)

    @asynccontextmanager
    async def _connect_by_channel(
        self, device: RfcommDeviceInfo | str, channel: int
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

        address = device if isinstance(device, str) else device.address
        async with AsyncExitStack() as stack:
            sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            stack.callback(sock.close)

            # Which adapter a socket goes out of is chosen by binding to that
            # adapter's own address; left unbound the kernel picks for itself,
            # which would quietly ignore the adapter the caller opened.
            if self._address is not None:
                try:
                    sock.bind((self._address, 0))
                except OSError as error:
                    raise ConnectionFailedError(
                        f"could not use adapter {self._address}: {error}"
                    ) from error

            try:
                await asyncio.get_running_loop().sock_connect(sock, (address, channel))
            except OSError as error:
                raise ConnectionFailedError(
                    f"could not reach {address} on channel {channel}: {error}"
                ) from error

            reader, writer = await asyncio.open_connection(sock=sock)
            stack.callback(writer.close)

            yield BlueZChannel(reader, writer)


class BlueZBackend(BackendProvider):
    """
    The Linux implementation.
    """

    @override
    async def list_adapters(self) -> list[RfcommAdapterInfo]:
        async with _bus(negotiate_unix_fd=False) as bus:
            return await _list_adapters(bus)

    @override
    def open_adapter(
        self, adapter: RfcommAdapterInfo | None = None
    ) -> AbstractAsyncContextManager[BackendAdapter]:
        return self._open(adapter)

    @asynccontextmanager
    async def _open(
        self, adapter: RfcommAdapterInfo | None
    ) -> AsyncGenerator[BackendAdapter, None]:
        # A connected channel arrives as a Unix file descriptor, so passing
        # them has to be negotiated when the connection is made.
        async with _bus(negotiate_unix_fd=True) as bus:
            available = await _list_adapters(bus)
            if not available:
                raise AdapterNotFoundError("this machine has no Bluetooth adapter")

            # REVISIT: taking the first is a poor default on a machine with
            # more than one radio. It should prefer a powered adapter over an
            # unpowered one, and honour an environment variable naming a
            # preferred adapter so a user can settle it without touching code.
            # bleak has a worked-out algorithm for this worth following.
            chosen = available[0] if adapter is None else adapter
            if not any(a.id == chosen.id for a in available):
                raise AdapterNotFoundError(chosen.id)

            radio = await _get(bus, chosen.id, _ADAPTER, _AdapterProxy)
            if not await radio.get_powered():
                raise AdapterOffError(
                    f"{chosen.name or chosen.id} is switched off; turn Bluetooth on"
                )

            manager = await _get(
                bus, "/org/bluez", _PROFILE_MANAGER, _ProfileManagerProxy
            )
            yield BlueZAdapter(bus, chosen, _Profiles(bus, manager))


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


async def _list_adapters(bus: MessageBus) -> list[RfcommAdapterInfo]:
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
        RfcommAdapterInfo(
            path,
            interfaces[_ADAPTER].get("Address", Variant("s", "")).value or None,
            interfaces[_ADAPTER].get("Alias", Variant("s", "")).value or None,
        )
        for path, interfaces in sorted(objects.items())
        if _ADAPTER in interfaces
    ]


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
