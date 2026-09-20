# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The device handle callers hold.
"""

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID

from aio_rfcomm._scope import Scope
from aio_rfcomm.backend.provider import BackendAdapter, BackendChannel
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.discovery import RfcommDeviceInfo

__all__ = ["RfcommDevice"]


class RfcommDevice:
    """
    A device, reached through the adapter it was found on.

    Holding the adapter means a channel can be opened straight from the
    device, and it ties the device to the adapter's scope, where it belongs:
    a device outliving the adapter it came from would have nothing to talk
    through.
    """

    def __init__(
        self, backend: BackendAdapter, info: RfcommDeviceInfo, scope: Scope
    ) -> None:
        """
        Not called directly. Devices come from :meth:`~aio_rfcomm.RfcommAdapter.list_known_devices` or
        :meth:`~aio_rfcomm.RfcommAdapter.use_device`.

        Args:
            backend: The adapter this device was found on.
            info: What the OS knows about the device.
            scope: The adapter's scope. The device dies with it.
        """
        self._backend = backend
        self._info = info
        self._scope = scope

    @property
    def address(self) -> str:
        """
        The device's Bluetooth address.
        """
        return self._info.address

    @property
    def name(self) -> str | None:
        """
        The device's name, if the OS knows one.

        Not reliable for identification: the OS may be reporting a stale
        cached name. Match on :attr:`address`.
        """
        return self._info.name

    def open_service(self, service: UUID) -> AbstractAsyncContextManager[RfcommChannel]:
        """
        Open a channel to a service, looking up its channel number.

        Args:
            service: The service to look up and connect to.

        Returns:
            An async context manager yielding the open channel, closed on
            exit.
        """
        self._scope.check()
        return self._open(self._backend.open_service(self._info, service), self._scope)

    def open_channel(self, channel: int) -> AbstractAsyncContextManager[RfcommChannel]:
        """
        Open a channel by number, skipping service discovery.

        Worth using when the number is fixed and known, since service
        discovery is a slow round trip.

        Args:
            channel: The RFCOMM channel number.

        Returns:
            An async context manager yielding the open channel, closed on
            exit.
        """
        self._scope.check()
        return self._open(self._backend.open_channel(self._info, channel), self._scope)

    @staticmethod
    @asynccontextmanager
    async def _open(
        opening: AbstractAsyncContextManager[BackendChannel],
        parent: Scope,
    ) -> AsyncGenerator[RfcommChannel, None]:
        scope = parent.child("channel")
        async with opening as backend_channel:
            channel = RfcommChannel(backend_channel, scope)
            try:
                yield channel
            finally:
                scope.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.address} {self.name!r}>"
