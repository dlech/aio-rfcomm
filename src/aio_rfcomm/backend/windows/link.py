# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Hearing when the link to a device goes down, to explain a channel's end.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from winrt.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothDevice
from winrt.windows.foundation import EventRegistrationToken

from aio_rfcomm.backend.windows.addresses import parse_address

__all__ = ["LinkWatch", "watch_link"]

logger = logging.getLogger(__name__)

# How long to wait, once a channel's stream has ended, for the link to its
# device to be reported down. Measured on Windows 10.0.26200.9457 against
# BlueZ 5.85: when the peer switches its radio off, the stream ends cleanly
# and the link is reported down about 4 ms later. A peer hanging up leaves the
# link up. So twenty-five times that, chosen rather than measured.
_LINK_SETTLE_SECONDS = 0.1


class LinkWatch:
    """
    Hears when the link to one device goes down.
    """

    def __init__(self, device: BluetoothDevice) -> None:
        """
        Args:
            device: The device at the other end of a channel.
        """
        self._device = device
        self._gone = asyncio.Event()
        loop = asyncio.get_running_loop()

        def changed(sender: BluetoothDevice | None, _args: object) -> None:
            # WinRT calls back on threads of its own, possibly after the loop
            # has closed.
            if (
                sender is not None
                and sender.connection_status == BluetoothConnectionStatus.DISCONNECTED
            ):
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._gone.set)

        self._token: EventRegistrationToken | None = (
            device.add_connection_status_changed(changed)
        )

    async def went_down(self) -> bool:
        """
        Say whether the link went down with the channel.

        Returns:
            Whether it is down now, or is reported down shortly.
        """
        if self._device.connection_status == BluetoothConnectionStatus.DISCONNECTED:
            return True

        try:
            async with asyncio.timeout(_LINK_SETTLE_SECONDS):
                await self._gone.wait()
        except TimeoutError:
            return False

        return True

    def close(self) -> None:
        """
        Stop listening. Calling this twice is harmless.
        """
        if self._token is not None:
            self._device.remove_connection_status_changed(self._token)
            self._token = None


async def watch_link(address: str) -> LinkWatch | None:
    """
    Start hearing when the link to a device goes down.

    Args:
        address: The device's address.

    Returns:
        The watch, or ``None`` if the device could not be found, in which
        case every end is put down to the peer.
    """
    try:
        device = await BluetoothDevice.from_bluetooth_address_async(
            parse_address(address)
        )
    except OSError:
        logger.debug("could not find %s to watch its link", address, exc_info=True)
        return None

    if device is None:
        return None

    return LinkWatch(device)
