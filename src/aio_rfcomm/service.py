# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The service handle callers hold, and the machinery that feeds its handler.

A served channel's lifetime is the handler call: the channel is opened before
the handler starts and closed once it returns, so a handler never has to
remember to close anything and cannot leak a channel by returning early.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID

from aio_rfcomm._scope import Scope
from aio_rfcomm._teardown import raise_when, unwrap_lone_error
from aio_rfcomm.backend.provider import BackendChannel, BackendService
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import AdapterLostError, AdapterLostReason, RfcommError

__all__ = ["ConnectionHandler", "RfcommService"]

ConnectionHandler = Callable[[RfcommDeviceInfo, RfcommChannel], Awaitable[None]]
"""
What runs for each peer that connects.

Called with the peer and its channel. The channel is closed when the handler
returns, so the handler runs for exactly as long as it wants the connection to
last.
"""


class RfcommService:
    """
    A service published on an adapter, listening for peers.

    Handed to the caller so that it can say what was published -- most usefully
    the channel number, which the caller may not have chosen -- and so that it
    can stop work when the adapter underneath goes away.
    """

    def __init__(
        self,
        backend: BackendService,
        service: UUID,
        name: str,
        lost: Callable[[], Awaitable[AdapterLostReason]],
    ) -> None:
        """
        Not called directly. Services come from :meth:`~aio_rfcomm.RfcommAdapter.serve`.

        Args:
            backend: The backend-specific service object.
            service: The service UUID that was published.
            name: The name that was published with it.
            lost: Waits until the adapter underneath is gone.
        """
        self._backend = backend
        self._service = service
        self._name = name
        self._lost = lost

    @property
    def service(self) -> UUID:
        """
        The service UUID being published.
        """
        return self._service

    @property
    def name(self) -> str:
        """
        The human-readable name being published with it.
        """
        return self._name

    @property
    def channel(self) -> int:
        """
        The RFCOMM channel peers reach this service on.

        Worth printing: a peer that cannot do service discovery needs this
        number, and it may have been chosen by the library rather than by the
        caller.
        """
        return self._backend.channel

    async def _lost_error(self) -> RfcommError:
        return AdapterLostError(await self._lost())

    def fail_when_gone(self) -> AbstractAsyncContextManager[None]:
        """
        Stop the enclosed block once the adapter goes away.

        A server spends its life waiting, so without this it would wait just
        as patiently on an adapter that has been switched off::

            async with service.fail_when_gone():
                await asyncio.Event().wait()

        Returns:
            An async context manager that raises
            :class:`~aio_rfcomm.errors.AdapterLostError` in the enclosed block
            once the adapter is gone.
        """
        return raise_when(self._lost_error)


async def _handle(
    handler: ConnectionHandler,
    peer: RfcommDeviceInfo,
    opening: AbstractAsyncContextManager[BackendChannel],
    parent: Scope,
) -> None:
    """
    Run one handler, and report rather than propagate what it raises.

    A server outlives the connections it serves, so one handler failing must
    not take down the service or its other connections. There is nobody to
    raise to, which is what the loop's exception handler is for: it prints
    the traceback by default and the application can replace it.

    Args:
        handler: What the caller gave to :meth:`~aio_rfcomm.RfcommAdapter.serve`.
        peer: Who connected.
        opening: The channel, not yet open.
        parent: The service's scope, which the channel's nests inside.
    """
    scope = parent.child("channel")
    try:
        async with opening as backend_channel:
            try:
                await handler(peer, RfcommChannel(backend_channel, scope))
            finally:
                scope.close()
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001  (reported, not propagated)
        asyncio.get_running_loop().call_exception_handler(
            {
                "message": "unhandled exception in an RFCOMM connection handler",
                "exception": error,
                "peer": peer.address,
            }
        )


async def _accept_forever(
    backend: BackendService,
    handler: ConnectionHandler,
    group: asyncio.TaskGroup,
    running: set[asyncio.Task[None]],
    scope: Scope,
) -> None:
    """
    Take connections as they arrive and give each one to the handler.

    Args:
        backend: The listening service.
        handler: What to run for each peer.
        group: Where handler tasks are started.
        running: The handler tasks, so they can be stopped with the service.
        scope: The service's scope.
    """
    while True:
        peer, opening = await backend.accept()
        task = group.create_task(_handle(handler, peer, opening, scope))
        running.add(task)
        task.add_done_callback(running.discard)


@asynccontextmanager
async def serve_connections(
    opening: AbstractAsyncContextManager[BackendService],
    handler: ConnectionHandler,
    service: UUID,
    name: str,
    lost: Callable[[], Awaitable[AdapterLostReason]],
    parent: Scope,
) -> AsyncGenerator[RfcommService, None]:
    """
    Publish a service and run ``handler`` for every peer that connects.

    The accepting and the handlers live in a task group of the block's own, so
    that leaving the block stops all of them. Handlers are cancelled rather
    than waited for: the block ending is the caller saying the service is over,
    and a handler that is still talking to a peer would otherwise keep it open
    indefinitely.

    Args:
        opening: The backend's service, not yet published.
        handler: What to run for each peer.
        service: The service UUID being published.
        name: The name being published with it.
        lost: Waits until the adapter underneath is gone.
        parent: The adapter's scope, which the service's nests inside.

    Yields:
        The published service.
    """
    scope = parent.child("service")
    async with opening as backend:
        running: set[asyncio.Task[None]] = set()
        try:
            with unwrap_lone_error():
                async with asyncio.TaskGroup() as group:
                    accepting = group.create_task(
                        _accept_forever(backend, handler, group, running, scope)
                    )
                    try:
                        yield RfcommService(backend, service, name, lost)
                    finally:
                        accepting.cancel()
                        for task in running:
                            task.cancel()
        finally:
            scope.close()
