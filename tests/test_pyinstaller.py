# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The files PyInstaller reads to bundle this package.

Building a frozen program needs PyInstaller and takes minutes, so this only
checks that what PyInstaller looks for is where it looks.
"""

import ast
import runpy
import sys
from pathlib import Path

import pytest

import aio_rfcomm
from aio_rfcomm._pyinstaller import get_hook_dirs


def _hooks() -> Path:
    (directory,) = get_hook_dirs()
    return Path(directory)


def test_the_hook_is_named_for_the_package() -> None:
    assert (_hooks() / "hook-aio_rfcomm.py").is_file()


def test_the_hook_needs_nothing_from_pyinstaller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # py2app, told to include this package, fails on PyInstaller's own hooks.
    monkeypatch.setitem(sys.modules, "PyInstaller", None)
    monkeypatch.setattr(sys, "platform", "darwin")

    hook = runpy.run_path(str(_hooks() / "hook-aio_rfcomm.py"))

    assert hook["hiddenimports"]


def test_the_hook_names_modules_that_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    root = Path(aio_rfcomm.__file__).parent.parent

    hook = runpy.run_path(str(_hooks() / "hook-aio_rfcomm.py"))

    for name in hook["hiddenimports"]:
        assert root.joinpath(*name.split(".")).with_suffix(".py").is_file(), name


def test_the_run_time_hook_is_listed_where_pyinstaller_reads_it() -> None:
    listed = ast.literal_eval((_hooks() / "rthooks.dat").read_text())

    assert listed == {"aio_rfcomm": ["pyi_rth_aio_rfcomm.py"]}
    assert (_hooks() / "rthooks" / "pyi_rth_aio_rfcomm.py").is_file()


def test_the_run_time_hook_leaves_an_ordinary_start_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIO_RFCOMM_CONTROL_FD", raising=False)

    runpy.run_path(str(_hooks() / "rthooks" / "pyi_rth_aio_rfcomm.py"))
