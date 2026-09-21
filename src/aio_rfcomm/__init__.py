# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Cross-platform asyncio RFCOMM.

Open an adapter, then either reach out to a device's service or publish one of
your own and wait for peers. Serving is implemented on Linux so far; see the
design notes linked from README.md.
"""

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from aio_rfcomm.adapter import RfcommAdapter
from aio_rfcomm.backend import get_backend
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.device import RfcommDevice
from aio_rfcomm.discovery import RfcommAdapterInfo, RfcommDeviceInfo
from aio_rfcomm.service import ConnectionHandler, RfcommService

__all__ = [
    "ConnectionHandler",
    "RfcommAdapter",
    "RfcommAdapterInfo",
    "RfcommChannel",
    "RfcommDevice",
    "RfcommDeviceInfo",
    "RfcommService",
    "list_adapters",
    "open_adapter",
]


async def list_adapters() -> list[RfcommAdapterInfo]:
    """
    List the Bluetooth adapters on this machine.

    Most programs never need this. :func:`open_adapter` with no argument picks
    the default one, which is the right answer on macOS and Windows and on the
    great majority of Linux machines. This exists for the case of a Linux box
    with more than one radio, where it matters which one is used.

    A way to name a preferred adapter through the environment would serve that
    case better, and is worth adding later.

    Returns:
        The adapters. macOS reports at most one.
    """
    return await get_backend().list_adapters()


@asynccontextmanager
async def _open_adapter(
    adapter: RfcommAdapterInfo | None,
) -> AsyncGenerator[RfcommAdapter, None]:
    async with get_backend().open_adapter(adapter) as backend:
        handle = RfcommAdapter(backend)
        try:
            yield handle
        finally:
            handle._close()


def open_adapter(
    adapter: RfcommAdapterInfo | None = None,
) -> AbstractAsyncContextManager[RfcommAdapter]:
    """
    Open an adapter for use.

    Args:
        adapter: Which adapter. ``None`` for the default.

    Returns:
        An async context manager yielding the open adapter, closed on exit.
    """
    return _open_adapter(adapter)
