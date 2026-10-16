# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Runs before the program, and runs the macOS helper instead when asked to.

A frozen program has no interpreter to start the helper with, so the library
starts the program itself again, with ``AIO_RFCOMM_CONTROL_FD`` set.
"""


def _run_helper_if_asked() -> None:
    import os
    import sys

    if sys.platform != "darwin" or "AIO_RFCOMM_CONTROL_FD" not in os.environ:
        return

    import runpy

    runpy.run_module("aio_rfcomm.backend.iobluetooth.helper", run_name="__main__")
    sys.exit(0)


_run_helper_if_asked()
