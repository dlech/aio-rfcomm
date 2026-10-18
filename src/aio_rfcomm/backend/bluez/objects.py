# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
BlueZ's adapters and devices, read once and then followed through its signals.
"""

import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

assert not TYPE_CHECKING or sys.platform == "linux"

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from aio_rfcomm.backend.bluez.dbus import (
    ADAPTER,
    DEVICE,
    OBJECT_MANAGER,
    PROPERTIES,
    SERVICE,
    ObjectManagerProxy,
    PropertiesProxy,
    get_interface,
)

__all__ = ["BlueZObjects"]


# What the bus answers when nothing owns the name asked for: BlueZ is not
# running, and nothing has started it.
_NOBODY_THERE = (
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
)

# The adapter properties that say whether it is powered.
_POWER = frozenset({"Powered", "PowerState"})

_Properties = dict[str, Variant]


class BlueZObjects:
    """
    The adapters and devices BlueZ has, as last heard.

    Everything is read when BlueZ starts. After that each change arrives as a
    signal carrying the new values, so nothing is read again unless BlueZ
    restarts or a read fails.
    """

    def __init__(self) -> None:
        self.adapters: dict[str, _Properties] = {}
        """
        Each adapter's ``org.bluez.Adapter1`` properties, by object path.
        """

        self.devices: dict[str, _Properties] = {}
        """
        Each device's ``org.bluez.Device1`` properties, by object path.
        """

        self.running = False
        """
        Whether BlueZ is there.
        """

        self.stale = True
        """
        Whether everything has to be read again before it can be trusted.
        """

        self._kinds = {ADAPTER: self.adapters, DEVICE: self.devices}
        self._heard: list[list[Message]] = []

    def hear(self, message: Message) -> bool:
        """
        Take in a message the bus delivered.

        Args:
            message: Any message. Only BlueZ's signals change anything.

        Returns:
            Whether it may change whether Bluetooth is usable.
        """
        if message.message_type is not MessageType.SIGNAL:
            return False

        for heard in self._heard:
            heard.append(message)

        return self._apply(message)

    async def read(self, bus: MessageBus) -> None:
        """
        Read everything again.

        Args:
            bus: A connected system bus.

        Raises:
            DBusError: BlueZ is there but could not be read.
        """
        try:
            with self._meanwhile() as heard:
                manager = await get_interface(
                    bus, "/", OBJECT_MANAGER, ObjectManagerProxy
                )
                objects = await manager.call_get_managed_objects()
        except DBusError as error:
            if error.type not in _NOBODY_THERE:
                raise

            # Known, so no read is needed until BlueZ starts.
            self._forget(stale=False)
            return

        for table in self._kinds.values():
            table.clear()

        for path, interfaces in objects.items():
            for name, properties in interfaces.items():
                table = self._kinds.get(name)

                if table is not None:
                    table[path] = properties

        self.running = True
        self.stale = False
        self._replay(heard)

    async def read_adapter(self, bus: MessageBus, path: str) -> None:
        """
        Read one adapter's properties again.

        Args:
            bus: A connected system bus.
            path: The adapter's object path.

        Raises:
            DBusError: The adapter, or BlueZ, has gone.
        """
        with self._meanwhile() as heard:
            properties = await get_interface(bus, path, PROPERTIES, PropertiesProxy)
            values = await properties.call_get_all(ADAPTER)

        self.adapters[path] = values
        self._replay(heard)

    @contextmanager
    def _meanwhile(self) -> Generator[list[Message], None, None]:
        """
        Keep the signals that arrive while a read is in progress, to apply
        again on top of what was read.

        Yields:
            Where they are kept.
        """
        # The reply comes after every signal BlueZ sent before it, but the task
        # that asked may run only after later ones were taken in. Applying them
        # all again, in order, leaves the latest of each.
        heard: list[Message] = []
        self._heard.append(heard)
        try:
            yield heard
        finally:
            self._heard.remove(heard)

    def _replay(self, heard: list[Message]) -> None:
        for message in heard:
            self._apply(message)

    def _forget(self, *, stale: bool) -> None:
        for table in self._kinds.values():
            table.clear()

        self.running = False
        self.stale = stale

    def _apply(self, message: Message) -> bool:
        match (message.interface, message.member):
            case ("org.freedesktop.DBus.Properties", "PropertiesChanged"):
                interface, changed, invalidated = message.body
                known = self._kinds.get(interface, {}).get(message.path or "")

                if known is not None:
                    known.update(changed)

                    for name in invalidated:
                        known.pop(name, None)

                # Any adapter's, not only the one in use: one that powers up
                # may be the one to use.
                return interface == ADAPTER and not _POWER.isdisjoint(
                    [*changed, *invalidated]
                )
            case ("org.freedesktop.DBus.ObjectManager", "InterfacesAdded"):
                path, interfaces = message.body

                for name, properties in interfaces.items():
                    table = self._kinds.get(name)

                    if table is not None:
                        table.setdefault(path, {}).update(properties)

                return ADAPTER in interfaces
            case ("org.freedesktop.DBus.ObjectManager", "InterfacesRemoved"):
                path, names = message.body

                for name in names:
                    self._kinds.get(name, {}).pop(path, None)

                return ADAPTER in names
            case ("org.freedesktop.DBus", "NameOwnerChanged"):
                # The handler sees every message on the bus, not only BlueZ's.
                name, _, owner = message.body

                if name != SERVICE:
                    return False

                # A new owner is a BlueZ that has started afresh.
                self._forget(stale=bool(owner))
                return True
            case _:
                return False
