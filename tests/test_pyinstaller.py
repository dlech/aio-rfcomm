# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The files PyInstaller reads to bundle this package.

Building a frozen program needs PyInstaller and takes minutes, so this only
checks that what PyInstaller looks for is where it looks.
"""

import ast
import runpy
from pathlib import Path

import pytest

from aio_rfcomm._pyinstaller import get_hook_dirs


def _hooks() -> Path:
    (directory,) = get_hook_dirs()
    return Path(directory)


def test_the_hook_is_named_for_the_package() -> None:
    assert (_hooks() / "hook-aio_rfcomm.py").is_file()


def test_the_run_time_hook_is_listed_where_pyinstaller_reads_it() -> None:
    listed = ast.literal_eval((_hooks() / "rthooks.dat").read_text())

    assert listed == {"aio_rfcomm": ["pyi_rth_aio_rfcomm.py"]}
    assert (_hooks() / "rthooks" / "pyi_rth_aio_rfcomm.py").is_file()


def test_the_run_time_hook_leaves_an_ordinary_start_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIO_RFCOMM_CONTROL_FD", raising=False)

    runpy.run_path(str(_hooks() / "rthooks" / "pyi_rth_aio_rfcomm.py"))
