# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The PyInstaller hook for aio-rfcomm.
"""

import sys

from PyInstaller.utils.hooks import collect_submodules

# The macOS helper runs as a module nothing imports, and it needs rubicon,
# which the program itself never imports.
hiddenimports = (
    collect_submodules("aio_rfcomm.backend.iobluetooth")
    + collect_submodules("rubicon.objc")
    if sys.platform == "darwin"
    else []
)
