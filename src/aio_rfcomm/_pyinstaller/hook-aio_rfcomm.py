# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The PyInstaller hook for aio-rfcomm.
"""

import sys

# The macOS helper runs as a module nothing imports. PyInstaller follows its
# imports from here, rubicon among them.
#
# Nothing is imported from PyInstaller. py2app bundles whatever the modules of
# an included package import, and it fails on PyInstaller's own hooks. Measured
# with py2app 0.28.10 and PyInstaller 6.22.3.
hiddenimports = (
    ["aio_rfcomm.backend.iobluetooth.helper.__main__"]
    if sys.platform == "darwin"
    else []
)
