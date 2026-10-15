# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Serving, the way a listening socket does it.

A published service hands out each peer that connects as a request, which the
program accepts -- getting a channel, in a block of its own -- or declines.
The program runs its conversations in its own task group, so nothing here
starts a task.
"""

from __future__ import annotations

import copy
import enum
import logging
import warnings
import weakref
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Self
from uuid import UUID

from aio_rfcomm._scope import Scope
from aio_rfcomm.backend.provider import BackendRequest, BackendService
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import BluetoothUnavailableError

if TYPE_CHECKING:
    from aio_rfcomm.bluetooth import Bluetooth

__all__ = ["ConnectionRequests", "RfcommConnectionRequest", "RfcommService"]

logger = logging.getLogger(__name__)


class _Answer(enum.Enum):
    NONE = enum.auto()
    ACCEPTED = enum.auto()
    DECLINED = enum.auto()
    # Hung up on because Bluetooth went. Accepting it raises why.
    ENDED = enum.auto()


class RfcommConnectionRequest:
    """
    A device that has connected to a service.

    Accept it, or decline it. A request that is neither is hung up on when it is
    garbage collected, with a :class:`ResourceWarning`.
    """

    def __init__(self, backend: BackendRequest, service: RfcommService) -> None:
        """
        Not called directly. Requests come from
        :meth:`RfcommService.connection_requests`.

        Args:
            backend: The backend's request.
            service: The service it arrived through.
        """
        self._backend = backend
        self._service = service
        self._answer = _Answer.NONE

    @property
    def info(self) -> RfcommDeviceInfo:
        """
        The device that connected. Its name may be missing.
        """
        return self._backend.info

    def accept(self) -> AbstractAsyncContextManager[RfcommChannel]:
        """
        Accept the connection.

        Returns:
            An async context manager that gives the channel, and closes it when the
            block ends. The channel carries on even if the service stops first.
            Entering it raises :class:`~aio_rfcomm.errors.BluetoothUnavailableError`
            if Bluetooth stopped being usable since the request arrived.

        Raises:
            RuntimeError: The request was already accepted or declined.
            ScopeClosedError: The service's block has ended.
        """
        self._service._scope.check()
        match self._answer:
            case _Answer.ACCEPTED | _Answer.DECLINED:
                raise RuntimeError(
                    f"this request was already {self._answer.name.lower()}"
                )
            case _Answer.NONE | _Answer.ENDED:
                self._answer = _Answer.ACCEPTED
                return self._accepting()

    @asynccontextmanager
    async def _accepting(self) -> AsyncGenerator[RfcommChannel, None]:
        try:
            self._service._scope.check()
            self._service._check_ending()
            async with self._service._bluetooth._open(self._backend.open) as channel:
                yield channel
        finally:
            # Does nothing once a channel has the connection. Otherwise an
            # accept that failed would leave the peer connected.
            self._backend.close()

    async def decline(self) -> None:
        """
        Turn the device away by hanging up.

        The device sees its connection fail, or open and then close at once. This
        always takes effect, even if it is cancelled. It does not
        raise if hanging up fails. Declining twice does nothing.

        Raises:
            RuntimeError: The request was already accepted.
        """
        match self._answer:
            case _Answer.ACCEPTED:
                raise RuntimeError("this request was already accepted")
            case _Answer.DECLINED | _Answer.ENDED:
                return
            case _Answer.NONE:
                self._answer = _Answer.DECLINED
                try:
                    await self._backend.decline()
                except Exception:
                    # Nothing a caller could do about a hang-up that failed.
                    logger.debug(
                        "declining %s failed", self.info.address, exc_info=True
                    )

    def _close(self, answer: _Answer = _Answer.DECLINED) -> None:
        """
        Hang up, unless a channel already has the connection.

        For the service, which answers every request left unanswered when it
        ends, so none of them is warned about later.

        Args:
            answer: What the service's ending amounts to for this request.
        """
        if self._answer is _Answer.NONE:
            self._answer = answer
        self._backend.close()

    def __del__(self) -> None:
        # Hung up first: the warning can itself raise, under -W error or as
        # the interpreter shuts down.
        self._backend.close()

        if self._answer is _Answer.NONE:
            warnings.warn(
                f"a connection request from {self._backend.info.address} was "
                "neither accepted nor declined, and has been hung up on",
                ResourceWarning,
                source=self,
                stacklevel=2,
            )


class ConnectionRequests:
    """
    The devices connecting to a service, one request each.

    Made by :meth:`RfcommService.connection_requests`. When Bluetooth stops being
    usable, the ``async for`` loop raises
    :class:`~aio_rfcomm.errors.BluetoothUnavailableError`.
    """

    def __init__(self, service: RfcommService) -> None:
        """
        Not called directly. Use :meth:`RfcommService.connection_requests`.

        Args:
            service: The service being listened on.
        """
        self._service = service

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> RfcommConnectionRequest:
        """
        Wait for the next peer.

        Returns:
            Its request.

        Raises:
            BluetoothUnavailableError: Bluetooth went away, which ends the
                service. Every later call raises the same.
            ScopeClosedError: The service's block has ended.
        """
        return await self._service._next_request()


class RfcommService:
    """
    A service published on this computer, listening for devices.
    """

    def __init__(
        self,
        bluetooth: Bluetooth,
        backend: BackendService,
        service: UUID,
        name: str,
        scope: Scope,
    ) -> None:
        """
        Not called directly. Services come from
        :meth:`~aio_rfcomm.Bluetooth.serve`.

        Args:
            bluetooth: The Bluetooth object it was published through.
            backend: The backend's service.
            service: The service UUID that was published.
            name: The name that was published with it.
            scope: The service's scope.
        """
        self._bluetooth = bluetooth
        self._backend = backend
        self._service = service
        self._name = name
        self._scope = scope
        self._ending: BluetoothUnavailableError | None = None
        # Weak, so that one nobody kept can be collected and warned about.
        self._requests: weakref.WeakSet[RfcommConnectionRequest] = weakref.WeakSet()

    @property
    def service(self) -> UUID:
        """
        The service's UUID.
        """
        return self._service

    @property
    def name(self) -> str:
        """
        The service's name.
        """
        return self._name

    @property
    def channel(self) -> int:
        """
        The RFCOMM channel the service is on.

        A device that cannot look up the service by its UUID needs this number.
        """
        return self._backend.channel

    def connection_requests(self) -> ConnectionRequests:
        """
        Get each device that connects, as it arrives.

        ::

            async with asyncio.TaskGroup() as group:
                async for request in service.connection_requests():
                    group.create_task(talk(request))

        Returns:
            An async iterator of requests. It never ends by itself.
        """
        self._scope.check()
        return ConnectionRequests(self)

    async def _next_request(self) -> RfcommConnectionRequest:
        self._scope.check()
        self._check_ending()

        backend = None

        try:
            with self._bluetooth._interrupted():
                backend = await self._backend.accept()
        except BaseException:
            # Accepted, and then Bluetooth went before anyone could have it.
            if backend is not None:
                backend.close()
            raise

        request = RfcommConnectionRequest(backend, self)
        self._requests.add(request)
        return request

    def _check_ending(self) -> None:
        """
        Raises:
            BluetoothUnavailableError: Bluetooth went away, ending the service.
        """
        if self._ending is not None:
            raise copy.copy(self._ending)

    def _end(self, error: BluetoothUnavailableError) -> None:
        """
        End the service because Bluetooth has gone.

        The peers already waiting arrived before it went, over links that are
        gone too, so they are hung up on.

        Args:
            error: What every later call raises.
        """
        if self._ending is None:
            self._ending = error
        for request in list(self._requests):
            request._close(_Answer.ENDED)

    def _close(self) -> None:
        self._scope.close()
        for request in list(self._requests):
            request._close()
