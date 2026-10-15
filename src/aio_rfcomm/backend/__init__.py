# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

import sys

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.provider import BackendProvider
from aio_rfcomm.errors import BluetoothUnavailableError

__all__ = ["get_backend"]


def get_backend() -> BackendProvider:
    """
    Get the backend for this platform.

    Returns:
        The platform's backend.

    Raises:
        NotImplementedError: This platform will be supported, but its backend
            is not written yet.
        BluetoothUnavailableError: This platform has no RFCOMM support.
    """
    if sys.platform == "linux":
        from aio_rfcomm.backend.bluez import BlueZBackend

        return BlueZBackend()

    if sys.platform == "darwin":
        from aio_rfcomm.backend.iobluetooth import IOBluetoothBackend

        return IOBluetoothBackend()

    if sys.platform == "win32":
        from aio_rfcomm.backend.windows import WindowsBackend

        return WindowsBackend()

    raise BluetoothUnavailableError(
        Availability.UNSUPPORTED, f"no Bluetooth backend for {sys.platform}"
    )
