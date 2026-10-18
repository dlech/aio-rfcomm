# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Talking to BlueZ over the system bus: the interfaces we use and the calls
every part of the backend shares.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TypeVar, cast

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface
from dbus_fast.introspection import Node

__all__ = [
    "ADAPTER",
    "DEVICE",
    "OBJECT_MANAGER",
    "PROFILE_MANAGER",
    "PROPERTIES",
    "SERVICE",
    "DeviceProxy",
    "Interface",
    "ObjectManagerProxy",
    "ProfileManagerProxy",
    "PropertiesProxy",
    "add_match",
    "get_interface",
    "optional_string",
    "system_bus",
]

logger = logging.getLogger(__name__)


SERVICE = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
PROFILE_MANAGER = "org.bluez.ProfileManager1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"

# Interfaces dbus-fast already knows, so introspecting for them would cost a
# round trip for nothing.
_STANDARD = frozenset({OBJECT_MANAGER, PROPERTIES})

Interface = TypeVar("Interface", bound=ProxyInterface)

_ManagedObjects = dict[str, dict[str, dict[str, Variant]]]

# --------------------------------------------------------------------------
# Typed views of the BlueZ interfaces we use
#
# dbus-fast builds proxy methods at runtime from introspection, so a plain
# proxy has nothing a type checker can see. Declaring the handful we call
# keeps the call sites checked rather than littered with ignores.
# --------------------------------------------------------------------------


class ObjectManagerProxy(ProxyInterface):
    async def call_get_managed_objects(self) -> _ManagedObjects: ...


class PropertiesProxy(ProxyInterface):
    async def call_get_all(self, interface: str) -> dict[str, Variant]: ...


class ProfileManagerProxy(ProxyInterface):
    async def call_register_profile(
        self, profile: str, uuid: str, options: dict[str, Variant]
    ) -> None: ...

    async def call_unregister_profile(self, profile: str) -> None: ...


class DeviceProxy(ProxyInterface):
    async def call_connect_profile(self, uuid: str) -> None: ...


@asynccontextmanager
async def system_bus(*, negotiate_unix_fd: bool) -> AsyncGenerator[MessageBus, None]:
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


async def get_interface(
    bus: MessageBus, path: str, name: str, kind: type[Interface]
) -> Interface:
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
    introspection = (
        Node.default(path) if name in _STANDARD else await bus.introspect(SERVICE, path)
    )
    proxy = bus.get_proxy_object(SERVICE, path, introspection)
    return cast(Interface, proxy.get_interface(name))


async def add_match(bus: MessageBus, rule: str) -> None:
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
    if reply.message_type is MessageType.ERROR:
        # Not fatal: Bluetooth still works, but changes to it go unnoticed.
        logger.warning("the system bus refused %s: %s", rule, reply.body)


def optional_string(value: Variant | None) -> str | None:
    """
    Args:
        value: A property that may be missing.

    Returns:
        Its string, or ``None`` if it is missing.
    """
    return None if value is None else value.value
