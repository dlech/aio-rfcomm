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
all (``BlueZBluetooth.open_channel``), and choosing the channel a service
listens on, which falls back to a guess (``BlueZBluetooth._reserve_channel``).
"""

# Measurements cited in this module name the BlueZ they were made on, one of:
# - BlueZ 5.72: Ubuntu 24.04, package 5.72-0ubuntu5.5, before 2026-10-05. The
#   kernel was not recorded.
# - BlueZ 5.85: Ubuntu 26.04.1, package 5.85-4ubuntu0.3 or later, on kernel
#   7.0.0-38-generic.
# The peer was a Mac on macOS 26.6.2 (25G83).

import asyncio
import errno
import logging
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import cast
from uuid import UUID

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from typing_extensions import override

from aio_rfcomm._teardown import unwrap_lone_error
from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.bluez.availability import (
    WATCHED,
    follow_availability,
    read_availability,
)
from aio_rfcomm.backend.bluez.connection import (
    BlueZChannel,
    BlueZService,
)
from aio_rfcomm.backend.bluez.dbus import (
    DEVICE,
    OBJECT_MANAGER,
    PROFILE_MANAGER,
    SERVICE,
    DeviceProxy,
    Interface,
    ObjectManagerProxy,
    ProfileManagerProxy,
    add_match,
    get_interface,
    system_bus,
)
from aio_rfcomm.backend.bluez.errors import (
    translate_connect,
    translate_registration,
    translate_socket_connect,
)
from aio_rfcomm.backend.bluez.profile import Profiles, ServerProfile, profile_paths
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendChannel,
    BackendProvider,
    BackendService,
    NoBluetooth,
)
from aio_rfcomm.backend.stream import (
    open_stream,
)
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    ChannelInUseError,
    ConnectionFailedError,
    DeviceNotFoundError,
    UnsupportedOperationError,
)

__all__ = ["BlueZBackend"]

logger = logging.getLogger(__name__)


async def _nothing() -> None:
    pass


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


class BlueZBluetooth(BackendBluetooth):
    """
    Bluetooth through BlueZ, on one adapter, with a bus connection of its own.
    """

    def __init__(self, bus: MessageBus, profiles: Profiles) -> None:
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
        Reads whether Bluetooth is usable and reports it. Set once there is
        somewhere to report it.
        """

    async def _interface(
        self, path: str, name: str, kind: type[Interface]
    ) -> Interface:
        """
        Get one interface of one object, typed.

        Args:
            path: The object path.
            name: The interface name.
            kind: The declared view of that interface.

        Returns:
            The interface.
        """
        introspection = await self._bus.introspect(SERVICE, path)
        proxy = self._bus.get_proxy_object(SERVICE, path, introspection)
        return cast(Interface, proxy.get_interface(name))

    @override
    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        wanted = _wanted(service)
        # REVISIT: GetManagedObjects walks every object BlueZ knows and is
        # expensive to call repeatedly. bleak keeps one manager running and
        # follows change signals instead. Worth doing here if listing turns
        # out to be called often.
        manager = await self._interface("/", OBJECT_MANAGER, ObjectManagerProxy)
        objects = await manager.call_get_managed_objects()

        found: list[RfcommDeviceInfo] = []
        for path, interfaces in sorted(objects.items()):
            properties = interfaces.get(DEVICE)

            if properties is None:
                continue

            # Ask the device which adapter it belongs to rather than inferring
            # it from the shape of the object path. Address, Alias and Adapter
            # are always there. UUIDs is documented as optional (BlueZ 5.85,
            # doc/org.bluez.Device.rst).
            if properties["Adapter"].value != self.path:
                continue

            uuids = {
                u.lower() for u in properties.get("UUIDs", Variant("as", [])).value
            }

            if wanted and not wanted & uuids:
                continue

            found.append(
                RfcommDeviceInfo(
                    properties["Address"].value, properties["Alias"].value, path
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
        has to name a channel. See ``_unprobed_channel``.

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
        nothing, measured on BlueZ 5.72. So a channel is always settled here
        -- see ``_reserve_channel`` -- and always reported back.
        """
        channel = self._reserve_channel(channel)

        path = f"/org/aio_rfcomm/server{next(profile_paths)}"
        profile = ServerProfile()
        manager = await self._interface(
            "/org/bluez", PROFILE_MANAGER, ProfileManagerProxy
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
                raise translate_registration(error, str(service)) from error

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
                remote = await self._interface(device_path, DEVICE, DeviceProxy)
            except Exception as error:
                raise DeviceNotFoundError(address) from error

            try:
                await remote.call_connect_profile(uuid)
            except Exception as error:
                raise translate_connect(error, address, uuid) from error

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
        advertises before it considers anything else. Pinning ``Channel`` on
        the profile registration does not change that, measured on BlueZ 5.72.
        So this needs a real socket, and a Python built without Bluetooth
        support cannot do it at all.
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
            # adapter's own address. Left unbound the kernel picks for itself,
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
                raise translate_socket_connect(error, address, channel) from error

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
                bus = await stack.enter_async_context(
                    system_bus(negotiate_unix_fd=True)
                )
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
                Profiles(
                    bus,
                    lambda: get_interface(
                        bus, "/org/bluez", PROFILE_MANAGER, ProfileManagerProxy
                    ),
                ),
            )
            changed = asyncio.Event()

            def notice(message: Message) -> None:
                if message.message_type is MessageType.SIGNAL:
                    changed.set()

            bluetooth.refresh = lambda: read_availability(bus, bluetooth, state)
            bus.add_message_handler(notice)
            stack.callback(bus.remove_message_handler, notice)
            for rule in WATCHED:
                await add_match(bus, rule)

            with unwrap_lone_error():
                async with asyncio.TaskGroup() as group:
                    following = group.create_task(
                        follow_availability(bus, bluetooth, state, changed)
                    )
                    try:
                        yield bluetooth
                    finally:
                        following.cancel()
