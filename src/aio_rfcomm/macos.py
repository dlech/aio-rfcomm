# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
What only macOS needs.

Import and call it on every platform. Elsewhere it does nothing.
"""

import sys

__all__ = ["prompt_under_name"]


def prompt_under_name(*, name: str, reason: str) -> None:
    """
    Give this program a name for macOS to ask Bluetooth permission under.

    macOS asks the user before a program can use Bluetooth. A bundled application
    is asked about under its own name, and does not need this. A script, a REPL
    or a command line tool has no name of its own. Without one, macOS goes by the
    permission of the program that started it, such as the terminal. It asks
    under that program's name, or refuses if that program cannot be asked, and
    then the availability is ``NOT_ALLOWED``. If that program already has
    permission, Bluetooth works without asking. Call this before opening
    Bluetooth, so that your program is asked about under its own name.

    The user sees ``name`` in the permission dialog and later in the system
    settings, and macOS remembers the answer under it. Name it after your program,
    not after this library, and do not change it: a new name asks the user again.

    Does nothing on Linux and Windows.

    Args:
        name: The program's name, as the user knows it.
        reason: Why the program needs Bluetooth, shown in the permission dialog.
    """
    if sys.platform == "darwin":
        from aio_rfcomm.backend.iobluetooth.process import prompt_under_name

        prompt_under_name(name=name, reason=reason)
