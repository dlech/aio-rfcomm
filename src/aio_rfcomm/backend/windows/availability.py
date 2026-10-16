# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Reading whether Bluetooth is usable, and following it as it changes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, assert_never

from winrt.windows.devices.bluetooth import (
    BluetoothAdapter,
)
from winrt.windows.devices.enumeration import (
    DeviceInformation,
    DeviceWatcher,
    DeviceWatcherStatus,
)
from winrt.windows.devices.radios import RadioState

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.provider import (
    AvailabilityState,
)
from aio_rfcomm.backend.windows.addresses import format_address

if TYPE_CHECKING:
    from aio_rfcomm.backend.windows import WindowsBluetooth

__all__ = ["follow_availability", "list_adapters", "read_availability", "stop_watcher"]

logger = logging.getLogger(__name__)


async def list_adapters() -> list[tuple[str, str | None, str | None]]:
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
        # for the address instead was tried and measured on an unrecorded
        # Windows version: on an adapter interface that property comes back
        # empty, while other requested properties come back populated and the
        # same property on a device interface gives the address -- so it is
        # the adapter that does not carry it, not the request that is wrong.
        # Whether the radio does Classic is only on the object either way. In
        # practice there is one adapter, so this is one extra call.
        adapter = await BluetoothAdapter.from_id_async(info.id)
        if adapter is None or not adapter.is_classic_supported:
            continue
        found.append(
            (info.id, format_address(adapter.bluetooth_address), info.name or None)
        )
    return found


def stop_watcher(watcher: DeviceWatcher) -> None:
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


async def read_availability(
    bluetooth: WindowsBluetooth, state: AvailabilityState, notice: Callable[..., None]
) -> None:
    """
    Read whether Bluetooth is usable, and which radio to use.

    Args:
        bluetooth: Where the radio in use is kept.
        state: Where to report availability.
        notice: What the radio's events call.
    """
    available = await list_adapters()
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


async def follow_availability(
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
            await read_availability(bluetooth, state, notice)
        except Exception as error:
            # A failed read says nothing about Bluetooth, so what was last
            # known stands until the next event. With nothing known yet,
            # opening Bluetooth would wait forever.
            logger.exception("could not read whether Bluetooth is usable")

            if not state.is_known:
                state.set(
                    Availability.SERVICE_UNAVAILABLE,
                    f"could not read whether Bluetooth is usable: {error}",
                )

        await changed.wait()
