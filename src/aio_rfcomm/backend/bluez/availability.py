# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Reading whether Bluetooth is usable, and following it as it changes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple

from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.bluez.dbus import (
    ADAPTER,
    OBJECT_MANAGER,
    ObjectManagerProxy,
    get_interface,
    optional_string,
)
from aio_rfcomm.backend.provider import (
    AvailabilityState,
)

if TYPE_CHECKING:
    from aio_rfcomm.backend.bluez import BlueZBluetooth


__all__ = ["WATCHED", "follow_availability", "read_availability"]

logger = logging.getLogger(__name__)


# What the bus answers when nothing owns the name asked for: BlueZ is not
# running, and nothing has started it.
_NOBODY_THERE = (
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
)

# What can change whether Bluetooth is usable: an adapter's properties (which
# include whether it is powered), adapters arriving and leaving, and BlueZ
# itself starting and stopping. The first was measured against a radio being
# switched off and on (BlueZ 5.85). The others are only documented.
WATCHED = (
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


class _Adapter(NamedTuple):
    path: str
    address: str
    name: str
    powered: bool
    power_state: str | None


def _powered(adapter: _Adapter) -> tuple[Availability, str | None]:
    """
    Say whether an adapter's radio is usable.

    Measured on BlueZ 5.85: switching off goes through ``on-disabling`` for
    about 50 ms while ``Powered`` still reads true, and the radio's links drop
    during it. So only ``on`` counts, where BlueZ reports a power state.

    Args:
        adapter: The adapter, as last read.

    Returns:
        Its availability, and what to tell the user about it.
    """
    name = adapter.name

    match adapter.power_state:
        case None if adapter.powered:
            return Availability.USABLE, None
        case "on":
            return Availability.USABLE, None
        case "off-blocked":
            return (
                Availability.SWITCHED_OFF,
                f"{name} is blocked, by rfkill or airplane mode. Unblock it",
            )
        case _:
            return (
                Availability.SWITCHED_OFF,
                f"{name} is switched off. Turn Bluetooth on",
            )


async def _list_adapters(bus: MessageBus) -> list[_Adapter]:
    """
    Read every adapter BlueZ knows about.

    Args:
        bus: A connected system bus.

    Returns:
        The adapters, in object path order.
    """
    manager = await get_interface(bus, "/", OBJECT_MANAGER, ObjectManagerProxy)
    objects = await manager.call_get_managed_objects()

    # Address, Alias and Powered are always there. PowerState is documented
    # as experimental, so it may not be (BlueZ 5.85, doc/org.bluez.Adapter.rst).
    return [
        _Adapter(
            path,
            interfaces[ADAPTER]["Address"].value,
            interfaces[ADAPTER]["Alias"].value,
            interfaces[ADAPTER]["Powered"].value,
            optional_string(interfaces[ADAPTER].get("PowerState")),
        )
        for path, interfaces in sorted(objects.items())
        if ADAPTER in interfaces
    ]


async def read_availability(
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

    # The one in use is kept while it is there. Otherwise the first.
    # REVISIT: a machine with several radios should be able to name one.
    chosen = next((a for a in adapters if a.path == bluetooth.path), adapters[0])
    bluetooth.path = chosen.path
    bluetooth.address = chosen.address

    state.set(*_powered(chosen))


async def follow_availability(
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
            await read_availability(bus, bluetooth, state)
        except Exception as error:
            # A failed read says nothing about Bluetooth, so what was last
            # known stands until the next signal. With nothing known yet,
            # opening Bluetooth would wait forever.
            logger.exception("could not read whether Bluetooth is usable")

            if not state.is_known:
                state.set(
                    Availability.SERVICE_UNAVAILABLE,
                    f"could not read whether Bluetooth is usable: {error}",
                )
        await changed.wait()
