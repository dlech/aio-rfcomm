# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The Profile1 objects BlueZ calls back with connected file descriptors.
"""

# Do not add ``from __future__ import annotations`` to this module. dbus-fast
# reads the annotations on its Profile1 methods at runtime to work out the
# D-Bus signature, and under PEP 563 an annotation is stored as its own source
# text rather than the object, so the lookup would fail.

import asyncio
import itertools
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

from dbus_fast import Variant
from dbus_fast.annotations import DBusDict, DBusObjectPath, DBusUnixFd
from dbus_fast.service import ServiceInterface, method

from aio_rfcomm.backend.bluez.errors import translate_registration
from aio_rfcomm.backend.stream import hang_up_descriptor

__all__ = ["Profiles", "ServerProfile", "profile_paths"]

logger = logging.getLogger(__name__)


# Object paths are scoped to a bus connection, so this only has to be unique
# within one process. Two programs may export the same path without clashing.
profile_paths = itertools.count()


class _Profile(ServiceInterface):
    """
    The object BlueZ calls back with a connected file descriptor.

    One of these is shared by every connection attempt for a service UUID,
    because BlueZ allows only one registration per UUID. See ``Profiles``.
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
            waiter: The future returned by ``expect``.
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
            # Nobody is waiting: the attempt was canceled just before BlueZ
            # answered, or a peer connected unprompted. Either way the
            # descriptor is ours to hang up, and dropping it would leak it.
            logger.debug("unexpected connection from %s. Closing fd %d", device, fd)
            hang_up_descriptor(fd)
            return
        waiters.pop(0).set_result(fd)

    @method()
    def RequestDisconnection(self, device: DBusObjectPath) -> None:
        # BlueZ asking us to hang up. The channel's own scope is what closes
        # it, and closing the descriptor here would pull it out from under
        # whoever is still using it, so this only records the request.
        logger.debug("BlueZ asked us to disconnect %s", device)


# Only what Profiles uses, so a test can pass stand-ins. MessageBus and
# ProfileManagerProxy are both these.
class Exporter(Protocol):
    """
    Where profiles are exported.
    """

    def export(self, path: str, interface: ServiceInterface, /) -> None: ...
    def unexport(self, path: str, /) -> None: ...


class ProfileManager(Protocol):
    """
    BlueZ's ProfileManager1.
    """

    async def call_register_profile(
        self, profile: str, uuid: str, options: dict[str, Variant], /
    ) -> None: ...
    async def call_unregister_profile(self, profile: str, /) -> None: ...


class Profiles:
    """
    One Profile1 registration per service UUID, shared and reference counted.

    BlueZ rejects a second registration of a UUID with "UUID already
    registered", so a profile cannot be registered per connection attempt.
    Concurrent connects to one service share a registration, which is dropped
    when the last of them has finished with it.
    """

    def __init__(
        self,
        bus: Exporter,
        manager: Callable[[], Awaitable[ProfileManager]],
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
                path = f"/org/aio_rfcomm/profile{next(profile_paths)}"
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
                    raise translate_registration(error, uuid) from error
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


class ServerProfile(ServiceInterface):
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
        Hang up on any peer nobody took.

        Called when the service is withdrawn. These descriptors belong to
        peers that connected and were never handed to a handler, so hanging
        up is the only correct end.
        """
        while True:
            try:
                _, fd = self._arrived.get_nowait()
            except asyncio.QueueEmpty:
                return
            logger.debug("closing fd %d, queued but never taken", fd)
            hang_up_descriptor(fd)

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
