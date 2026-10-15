# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
What PyInstaller needs to bundle this package.

PyInstaller finds this through the ``pyinstaller40`` entry point. Nothing here
is used at run time.
"""

from pathlib import Path

__all__ = ["get_hook_dirs"]


def get_hook_dirs() -> list[str]:
    """
    Say where this package's PyInstaller hooks are.

    Returns:
        The directories holding them.
    """
    return [str(Path(__file__).parent)]
