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
from aio_rfcomm.backend.bluez.dbus import optional_string
from aio_rfcomm.backend.bluez.objects import BlueZObjects
from aio_rfcomm.backend.provider import (
    AvailabilityState,
)

if TYPE_CHECKING:
    from aio_rfcomm.backend.bluez import BlueZBluetooth


__all__ = [
    "WATCHED",
    "follow_availability",
    "refresh_availability",
    "report_availability",
]

logger = logging.getLogger(__name__)


# What can change whether Bluetooth is usable: an adapter's properties (which
# include whether it is powered), adapters arriving and leaving, and BlueZ
# itself starting and stopping. The first was measured against a radio being
# switched off and on (BlueZ 5.85). The others are only documented. Devices'
# properties are followed for listing them.
WATCHED = (
    (
        "type='signal',sender='org.bluez',"
        "interface='org.freedesktop.DBus.Properties',"
        "member='PropertiesChanged',arg0='org.bluez.Adapter1'"
    ),
    (
        "type='signal',sender='org.bluez',"
        "interface='org.freedesktop.DBus.Properties',"
        "member='PropertiesChanged',arg0='org.bluez.Device1'"
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


def _list_adapters(objects: BlueZObjects) -> list[_Adapter]:
    """
    List the adapters BlueZ has.

    Args:
        objects: What BlueZ has, as last heard.

    Returns:
        The adapters, in object path order.
    """
    # Address, Alias and Powered are always there. PowerState is documented
    # as experimental, so it may not be (BlueZ 5.85, doc/org.bluez.Adapter.rst).
    return [
        _Adapter(
            path,
            properties["Address"].value,
            properties["Alias"].value,
            properties["Powered"].value,
            optional_string(properties.get("PowerState")),
        )
        for path, properties in sorted(objects.adapters.items())
    ]


def report_availability(bluetooth: BlueZBluetooth, state: AvailabilityState) -> None:
    """
    Report whether Bluetooth is usable, from what BlueZ last said, and settle
    which adapter to use.

    Args:
        bluetooth: Where the adapter in use is kept.
        state: Where to report availability.
    """
    objects = bluetooth.objects

    if not objects.running:
        state.set(Availability.SERVICE_UNAVAILABLE, "BlueZ is not running")
        return

    adapters = _list_adapters(objects)

    if not adapters:
        state.set(Availability.NO_ADAPTER)
        return

    # The one in use is kept while it is there. Otherwise the first.
    # REVISIT: a machine with several radios should be able to name one.
    chosen = next((a for a in adapters if a.path == bluetooth.path), adapters[0])
    bluetooth.path = chosen.path
    bluetooth.address = chosen.address

    state.set(*_powered(chosen))


async def refresh_availability(
    bus: MessageBus, bluetooth: BlueZBluetooth, state: AvailabilityState
) -> None:
    """
    Read whether the adapter in use is on, for when the signal saying it is
    not may still be on its way, and report it.

    Args:
        bus: A connected system bus.
        bluetooth: Where the adapter in use is kept.
        state: Where to report availability.
    """
    objects = bluetooth.objects

    if objects.stale or bluetooth.path not in objects.adapters:
        await objects.read(bus)
    else:
        try:
            await objects.read_adapter(bus, bluetooth.path)
        except DBusError:
            # The adapter or BlueZ has gone, which reading everything finds out.
            await objects.read(bus)

    report_availability(bluetooth, state)


async def follow_availability(
    bus: MessageBus,
    bluetooth: BlueZBluetooth,
    state: AvailabilityState,
    changed: asyncio.Event,
) -> None:
    """
    Report whether Bluetooth is usable, and again after anything that may have
    changed it.

    Everything is read when BlueZ starts, and again only after a read failed.
    Otherwise the signals say what changed.

    Args:
        bus: A connected system bus.
        bluetooth: Where the adapter in use is kept.
        state: Where to report availability.
        changed: Set by every signal that may change it.
    """
    while True:
        changed.clear()
        try:
            if bluetooth.objects.stale:
                await bluetooth.objects.read(bus)

            report_availability(bluetooth, state)
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
