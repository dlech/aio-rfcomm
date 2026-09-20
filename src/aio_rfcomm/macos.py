# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS-specific corner of the API.

Some of what macOS needs cannot be decided for the program that imports this
library -- who it should identify itself to the user as, and in time, whether
it wants to lend this library its own main runloop rather than have a helper
process started for it. Those choices live here rather than in the
cross-platform API, where they would be arguments that mean nothing anywhere
else.

Importable anywhere, so a cross-platform program can guard the call with
``sys.platform`` rather than with a try/except around the import. On every
other platform these do nothing.
"""

import sys

__all__ = ["prompt_under_own_name"]

if sys.platform == "darwin":
    from aio_rfcomm.backend.iobluetooth._helper import prompt_under_own_name
else:

    def prompt_under_own_name(*, name: str, reason: str) -> None:
        """
        Do nothing. Only macOS asks a program to identify itself this way.

        Args:
            name: Ignored.
            reason: Ignored.
        """
