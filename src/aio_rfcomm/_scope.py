# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Keeping handles from outliving the block that produced them.

An adapter, a device and a channel each come out of an ``async with``, and
none of them means anything once that block has ended. Holding one afterwards
is a bug in the caller, and it should say so at once rather than half-working
or hanging.

Scopes nest the way the blocks do: a channel's scope has the adapter's as its
parent, so closing the adapter invalidates everything found through it without
having to track them.
"""

from dataclasses import dataclass, field

from aio_rfcomm.errors import ScopeClosedError

__all__ = ["Scope"]


@dataclass(eq=False, slots=True)
class Scope:
    """
    The lifetime of one handle.
    """

    what: str
    """
    What this scope belongs to, for the error message.
    """

    parent: "Scope | None" = None
    """
    The enclosing scope, if any. Closing it closes this one too.
    """

    closed: bool = field(default=False)
    """
    Whether this scope's own block has ended.
    """

    def child(self, what: str) -> "Scope":
        """
        Make a scope nested inside this one.

        Args:
            what: What the new scope belongs to.

        Returns:
            The nested scope.
        """
        return Scope(what, self)

    def check(self) -> None:
        """
        Confirm this scope and every enclosing one is still open.

        Raises:
            ScopeClosedError: This scope, or one enclosing it, has ended.
        """
        scope: Scope | None = self
        while scope is not None:
            if scope.closed:
                raise ScopeClosedError(
                    f"the {scope.what} is closed; it cannot be used outside the "
                    "'async with' block that created it"
                )
            scope = scope.parent

    def close(self) -> None:
        """
        End this scope.
        """
        self.closed = True
