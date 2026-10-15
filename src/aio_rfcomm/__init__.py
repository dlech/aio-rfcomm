# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Cross-platform asyncio RFCOMM.

Open Bluetooth with :func:`open_bluetooth`, then either reach out to a
device's service or publish one of your own and wait for peers.
"""

from aio_rfcomm.availability import Availability, UnavailableReason
from aio_rfcomm.bluetooth import AvailabilityWatcher, Bluetooth, open_bluetooth
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.service import (
    ConnectionRequests,
    RfcommConnectionRequest,
    RfcommService,
)

__all__ = [
    "Availability",
    "AvailabilityWatcher",
    "Bluetooth",
    "ConnectionRequests",
    "RfcommChannel",
    "RfcommConnectionRequest",
    "RfcommDeviceInfo",
    "RfcommService",
    "UnavailableReason",
    "open_bluetooth",
]
