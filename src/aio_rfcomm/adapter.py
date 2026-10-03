# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The adapter handle callers hold.
"""

from collections.abc import Collection
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from aio_rfcomm._scope import Scope
from aio_rfcomm._teardown import raise_when
from aio_rfcomm.backend.provider import (
    FIRST_CHANNEL,
    LAST_CHANNEL,
    BackendAdapter,
)
from aio_rfcomm.device import RfcommDevice
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import AdapterLostError, AdapterLostReason, RfcommError
from aio_rfcomm.service import ConnectionHandler, RfcommService, serve_connections

__all__ = ["RfcommAdapter"]


class RfcommAdapter:
    """
    An open Bluetooth adapter.

    Everything is reached through an adapter rather than through module-level
    state, because a machine may have more than one, and because the
    adapter's scope is what bounds the lifetime of whatever the platform
    needs underneath -- on macOS, a helper process.
    """

    def __init__(self, backend: BackendAdapter) -> None:
        """
        Not called directly. Adapters come from :func:`~aio_rfcomm.open_adapter`.

        Args:
            backend: The backend-specific adapter object.
        """
        self._backend = backend
        self._scope = Scope("adapter")

    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[RfcommDeviceInfo]:
        """
        List the devices this adapter already knows about.

        A snapshot of what the OS knows, which in practice means paired
        devices. Pairing is not something this library does yet, so for now
        it happens in the OS's own settings.

        Descriptions come back rather than usable devices, because the caller
        is going to look through them and pick one. Hand the one you want to
        :meth:`use_device`.

        Args:
            service: Return only devices offering this service, or any of
                these services. ``None`` returns every known device.

        Returns:
            What the OS knows about each matching device.
        """
        self._scope.check()
        return await self._backend.list_known_devices(service=service)

    def use_device(self, device: RfcommDeviceInfo | str) -> RfcommDevice:
        """
        Take a device to work with.

        Nothing is contacted here, so this cannot fail on an absent device;
        the address is only resolved when a channel is opened.

        Args:
            device: One of the descriptions from :meth:`list_known_devices`,
                or a bare Bluetooth address. An address need not be paired, or
                known to the OS at all.

        Returns:
            The device, ready to open a channel to.
        """
        self._scope.check()
        info = RfcommDeviceInfo(device, None) if isinstance(device, str) else device
        return RfcommDevice(self._backend, info, self._scope)

    def serve(
        self,
        service: UUID,
        handler: ConnectionHandler,
        *,
        name: str,
        channel: int | None = None,
    ) -> AbstractAsyncContextManager[RfcommService]:
        """
        Publish a service and run ``handler`` for every peer that connects.

        The handler is called with the peer and its channel, and the channel
        is closed when it returns::

            async def talk(peer, channel):
                await channel.send(b"hello\n")

            async with adapter.serve(CHAT, talk, name="chat") as service:
                print(f"listening on channel {service.channel}")
                async with service.fail_when_gone():
                    await asyncio.Event().wait()

        Handlers run concurrently, one per peer, and stop when this block
        does. An exception escaping a handler cannot be raised to anybody, so
        it goes to the event loop's exception handler instead of ending the
        service.

        Waiting for the block to end is the caller's business. Waiting on an
        :class:`asyncio.Event` that nothing sets, as above, costs nothing;
        a loop that sleeps and checks does not.

        Args:
            service: The service UUID to publish. Peers look this up to find
                the channel number, so it has to be one they already know.
            handler: What to run for each peer that connects.
            name: The human-readable service name, which goes into the
                published record. Required, because it is what a person sees
                when they look at what this machine offers.
            channel: The RFCOMM channel to listen on, 1 to 30. Leave it unset
                to let the library choose, which it can do everywhere except
                on a Linux Python built without Bluetooth socket support --
                there the choice cannot be made safely and has to be yours.

        Returns:
            An async context manager yielding the published service, withdrawn
            on exit.

        Raises:
            ValueError: ``channel`` is not an RFCOMM channel number.
            ChannelInUseError: Something already holds that channel.
            UnsupportedOperationError: This platform cannot serve yet, or no
                channel was given and this one cannot be chosen for you.
        """
        self._scope.check()
        if channel is not None and not FIRST_CHANNEL <= channel <= LAST_CHANNEL:
            raise ValueError(
                f"channel {channel} is not an RFCOMM channel; they run from "
                f"{FIRST_CHANNEL} to {LAST_CHANNEL}"
            )
        return serve_connections(
            self._backend.serve(service, name=name, channel=channel),
            handler,
            service,
            name,
            self.wait_until_gone,
            self._scope,
        )

    def _close(self) -> None:
        """
        End the adapter's scope, invalidating everything found through it.
        """
        self._scope.close()

    async def wait_until_gone(self) -> AdapterLostReason:
        """
        Wait until the adapter is gone.

        This does not return while the adapter is healthy, and leaving the
        ``async with`` block is not a close it can observe -- so a task
        awaiting it inside a task group in the adapter's own scope will
        deadlock, the group waiting on the task and the task waiting on a
        close that only happens once the group has finished. Use
        :meth:`fail_when_gone` for that; it cancels its watcher on the way
        out.

        Returns:
            Why the adapter went away.
        """
        return await self._backend.wait_until_gone()

    async def _lost_error(self) -> RfcommError:
        return AdapterLostError(await self.wait_until_gone())

    def fail_when_gone(self) -> AbstractAsyncContextManager[None]:
        """
        Stop the enclosed block once the adapter goes away.

        An adapter can be switched off part way through a session, which would
        otherwise leave idle work waiting on a radio that is no longer there.

        Returns:
            An async context manager that raises
            :class:`~aio_rfcomm.errors.AdapterLostError` in the enclosed block
            once the adapter is gone.
        """
        return raise_when(self._lost_error)
