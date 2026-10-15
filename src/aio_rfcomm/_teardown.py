# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Keeping the library's own task groups out of what callers see.
"""

from collections.abc import Generator
from contextlib import contextmanager

__all__ = ["unwrap_lone_error"]


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
