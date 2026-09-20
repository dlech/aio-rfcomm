# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The channel handle callers hold.
"""

from contextlib import AbstractAsyncContextManager

from aio_rfcomm._scope import Scope
from aio_rfcomm._teardown import raise_when
from aio_rfcomm.backend.provider import BackendChannel
from aio_rfcomm.errors import (
    ChannelBusyError,
    ChannelClosedError,
    CloseReason,
    RfcommError,
)

__all__ = ["RfcommChannel"]


class RfcommChannel:
    """
    An open RFCOMM channel.

    A byte stream, not a message stream: nothing preserves the boundaries
    between sends. macOS can look as though it does, because data arrives in
    fixed-size pieces, but that must not be relied on.

    One task sends and one task receives. Two tasks using the same direction
    raises :class:`~aio_rfcomm.errors.ChannelBusyError`.
    """

    def __init__(self, backend: BackendChannel, scope: Scope) -> None:
        """
        Not called directly. Channels come from :meth:`~aio_rfcomm.RfcommDevice.open_service` or
        :meth:`~aio_rfcomm.RfcommDevice.open_channel`.

        Args:
            backend: The backend-specific channel object.
            scope: The channel's scope. Using the channel outside it raises.
        """
        self._backend = backend
        self._scope = scope
        self._sending = False
        self._receiving = False

    async def send(self, data: bytes) -> None:
        """
        Send all of ``data``.

        If this is cancelled, an unknown number of bytes reached the peer, so
        the channel is broken and the only safe thing left is to close it.

        Args:
            data: The bytes to send.
        """
        self._scope.check()
        if self._sending:
            raise ChannelBusyError("another task is already sending on this channel")
        self._sending = True
        try:
            await self._backend.send(data)
        finally:
            self._sending = False

    async def receive(self, max_bytes: int | None = None) -> bytes:
        """
        Take the next bytes to arrive.

        Args:
            max_bytes: At most this many bytes. ``None`` for any amount.

        Returns:
            Between 1 and ``max_bytes`` bytes, or ``b""`` once the peer has
            hung up (:attr:`~aio_rfcomm.errors.CloseReason.PEER_CLOSED`).
        """
        self._scope.check()
        if self._receiving:
            raise ChannelBusyError("another task is already receiving on this channel")
        self._receiving = True
        try:
            return await self._backend.receive(max_bytes)
        finally:
            self._receiving = False

    async def wait_until_gone(self) -> CloseReason:
        """
        Wait until the channel is gone.

        Use this to notice a close and carry on::

            reason = await channel.wait_until_gone()
            if reason is CloseReason.PEER_CLOSED:
                print("the other end hung up")
            else:
                print(f"lost the channel: {reason.value}")

        To stop a block of work instead, use :meth:`fail_when_gone`.

        This does not return while the channel is healthy, and leaving the
        ``async with`` block is not a close it can observe -- so a task
        awaiting it inside a task group in the channel's own scope will
        deadlock, the group waiting on the task and the task waiting on a
        close that only happens once the group has finished. Use
        :meth:`fail_when_gone` for that; it cancels its watcher on the way
        out.

        Returns:
            Why the channel went away.
        """
        self._scope.check()
        return await self._backend.wait_until_gone()

    async def _closed_error(self) -> RfcommError:
        return ChannelClosedError(await self.wait_until_gone())

    def fail_when_gone(self) -> AbstractAsyncContextManager[None]:
        """
        Stop the enclosed block once the channel goes away.

        Pending sends and receives fail on their own, so this is for work that
        would otherwise sit idle and never notice::

            async with chan.fail_when_gone():
                async with asyncio.TaskGroup() as group:
                    group.create_task(pump(chan))
                    group.create_task(refresh_ui())

        The cancellation is scoped to exactly this block, and the library
        never reaches beyond it.

        Returns:
            An async context manager that raises
            :class:`~aio_rfcomm.errors.ChannelClosedError` in the enclosed
            block once the channel is gone, whatever the reason.
        """
        return raise_when(self._closed_error)
