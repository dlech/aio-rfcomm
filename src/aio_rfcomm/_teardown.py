# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Turning "the resource went away" into "this block stops".

Shared by channels and adapters. Written once as a function rather than
inherited from a base class, since the two have nothing else in common.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager, contextmanager

from aio_rfcomm.errors import RfcommError

__all__ = ["raise_when", "unwrap_lone_error"]


@contextmanager
def unwrap_lone_error() -> Generator[None, None, None]:
    """
    Report a task group's single failure as itself rather than as a group.

    A task group reports one failure as a group of one. Callers of this
    library should not have to write ``except*`` for what is a single error,
    so wherever a task group is an implementation detail its lone failure is
    unwrapped. A genuine group of several is passed through untouched, because
    flattening that would lose information.

    Yields:
        Nothing. The block runs as usual.
    """
    try:
        yield
    except BaseExceptionGroup as group:
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise


@asynccontextmanager
async def raise_when(
    error: Callable[[], Awaitable[RfcommError]],
) -> AsyncGenerator[None, None]:
    """
    Raise into the enclosed block once ``error`` produces one.

    The watcher is cancelled when the block finishes, so a resource that stays
    healthy does not leave the surrounding task group waiting on a watcher
    that will never fire.

    Args:
        error: Waits until the resource is gone, then builds the error to
            raise in the enclosed block.

    Yields:
        Nothing. The block runs normally until the resource is gone.
    """

    async def watch() -> None:
        raise await error()

    with unwrap_lone_error():
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch())
            try:
                yield
            finally:
                watcher.cancel()
