# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

import sys

from aio_rfcomm.backend.provider import BackendProvider
from aio_rfcomm.errors import UnsupportedPlatformError

__all__ = ["get_backend"]


def get_backend() -> BackendProvider:
    """
    Get the backend for this platform.

    Returns:
        The platform's backend.

    Raises:
        NotImplementedError: This platform will be supported, but its backend
            is not written yet.
        UnsupportedPlatformError: This platform has no RFCOMM support.
    """
    if sys.platform == "linux":
        from aio_rfcomm.backend.bluez import BlueZBackend

        return BlueZBackend()

    if sys.platform == "darwin":
        raise NotImplementedError("the macOS backend is not written yet")

    if sys.platform == "win32":
        raise NotImplementedError("the Windows backend is not written yet")

    raise UnsupportedPlatformError(f"no Bluetooth backend for {sys.platform}")
