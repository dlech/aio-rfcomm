# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

import sys
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from typing_extensions import override

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.provider import (
    AvailabilityState,
    BackendBluetooth,
    BackendProvider,
    NoBluetooth,
)

__all__ = ["get_backend"]


def get_backend() -> BackendProvider:
    """
    Get the backend for this platform.

    Returns:
        The platform's backend, or one that reports Bluetooth unsupported on a
        platform with none.
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

    return _Unsupported()


class _Unsupported(BackendProvider):
    """
    A platform with no backend, which is to say no Bluetooth as far as this
    library can tell.
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
        state.set(Availability.UNSUPPORTED, f"no Bluetooth backend for {sys.platform}")
        yield NoBluetooth()
