# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Building the application bundle the helper process runs from.

macOS refuses Bluetooth to a process whose responsible process has no
``NSBluetoothAlwaysUsageDescription`` in its ``Info.plist``, and it refuses by
sending ``SIGABRT`` rather than by returning an error. A wheel unpacked into
site-packages has no ``Info.plist`` to put the key in, so one is built here:
a minimal ``.app`` around a copy of the running interpreter, cached under the
user's cache directory and rebuilt only when the interpreter changes.

Two things about this are worth knowing before changing it.

The interpreter is copied rather than symlinked or launched through a wrapper,
because the bundle's identity comes from the main executable's own path. A
process that execs its way out of the bundle loses the ``Info.plist`` along
with it.

The bundle is not code signed. That was measured rather than assumed: a
disclaimed spawn from an unsigned bundle is granted Bluetooth exactly as a
signed one is. Signing would mean depending on ``codesign``, which needs the
Xcode command line tools, for no benefit.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import sys
import tempfile
from pathlib import Path

__all__ = ["build_helper"]

# Bumped when the layout below changes, so a cached bundle built by an older
# version of this package is rebuilt rather than reused.
_LAYOUT = 1

_DEFAULT_NAME = "aio-rfcomm"
_DEFAULT_REASON = "This program uses Bluetooth to talk to a paired device."


def build_helper(name: str = _DEFAULT_NAME, reason: str = _DEFAULT_REASON) -> Path:
    """
    Get the helper bundle, building it if it is not already cached.

    Args:
        name: What macOS should call the helper, in the permission dialog and
            in Privacy & Security.
        reason: The usage description shown in that dialog.

    Returns:
        The path of the executable inside the bundle.
    """
    interpreter = Path(sys.executable).resolve()
    app = _cache() / f"helper-{_key(interpreter, name, reason)}" / "AioRfcommHelper.app"
    executable = app / "Contents" / "MacOS" / "helper"
    if executable.exists():
        return executable

    app.parent.mkdir(parents=True, exist_ok=True)
    # Built somewhere else and moved into place, so that a bundle at the
    # expected path is always a finished one, even if two processes race or
    # this one is killed part way through.
    staging = Path(tempfile.mkdtemp(dir=app.parent))
    try:
        _assemble(staging / app.name, interpreter, name, reason)
        try:
            os.replace(staging / app.name, app)
        except OSError:
            # Another process finished first, or left a partial bundle. Either
            # way the one already there is no worse than ours.
            if not executable.exists():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return executable


def _assemble(app: Path, interpreter: Path, name: str, reason: str) -> None:
    """
    Lay out one bundle.

    Args:
        app: Where to build it. Must not exist.
        interpreter: The real interpreter to copy in.
        name: The bundle's display name.
        reason: The Bluetooth usage description.
    """
    executable = app / "Contents" / "MacOS" / "helper"
    executable.parent.mkdir(parents=True)
    shutil.copy2(interpreter, executable)
    executable.chmod(0o755)

    # An interpreter that loads libpython from @executable_path/../lib -- as
    # the python-build-standalone builds do -- looks for it relative to the
    # copy, which is now somewhere else entirely. Putting the original
    # directories back in the same relative places is enough; the dynamic
    # linker follows symbolic links.
    beside = interpreter.parent.parent
    for directory in ("lib", "Frameworks"):
        if (beside / directory).is_dir():
            (app / "Contents" / directory).symlink_to(beside / directory)

    with (app / "Contents" / "Info.plist").open("wb") as plist:
        plistlib.dump(
            {
                "CFBundleIdentifier": _identifier(name),
                "CFBundleName": name,
                "CFBundleExecutable": "helper",
                "CFBundlePackageType": "APPL",
                # Keeps the helper out of the Dock and the application
                # switcher. It has no user interface and should not look like
                # a program the user started.
                "LSUIElement": True,
                "NSBluetoothAlwaysUsageDescription": reason,
            },
            plist,
        )


def _identifier(name: str) -> str:
    """
    Build a bundle identifier from a display name.

    macOS remembers a Bluetooth decision against the identifier, so two
    programs that ask under different names get their own answers, and one
    program keeps its answer across runs.

    Args:
        name: The display name.

    Returns:
        A reverse-DNS bundle identifier.
    """
    slug = "".join(c if c.isalnum() else "-" for c in name).strip("-").lower()
    return f"dev.aiorfcomm.helper.{slug or 'unnamed'}"


def _key(interpreter: Path, name: str, reason: str) -> str:
    """
    Build the cache key for one bundle.

    Everything that ends up inside the bundle is part of the key, so a changed
    interpreter or a changed usage description produces a new bundle rather
    than a stale one.

    Args:
        interpreter: The interpreter that will be copied in.
        name: The bundle's display name.
        reason: The Bluetooth usage description.

    Returns:
        A short hexadecimal digest.
    """
    stat = interpreter.stat()
    parts = (
        str(_LAYOUT),
        str(interpreter),
        str(stat.st_size),
        str(stat.st_mtime_ns),
        name,
        reason,
    )
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def _cache() -> Path:
    """
    Find the directory cached bundles belong in.

    Returns:
        The directory. Not created here.
    """
    return Path.home() / "Library" / "Caches" / "aio-rfcomm"
