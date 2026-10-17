# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Check that a built sdist and wheel hold only files that git tracks.

uv_build packs whatever is under src/, ignored files included, so a stray
cache there would otherwise ship.
"""

import argparse
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

PACKAGE = "src/aio_rfcomm/"


def _tracked() -> set[str]:
    """
    List the files git tracks.

    Returns:
        Their paths, relative to the top of the repository.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return {path for path in listed.split("\0") if path}


def _compare(
    name: str, packed: set[str], allowed: set[str], required: set[str]
) -> list[str]:
    """
    Say how one archive's files differ from the ones it should hold.

    Args:
        name: The archive's file name.
        packed: The files in the archive.
        allowed: The files it may hold.
        required: The files it must hold.

    Returns:
        One line per difference.
    """
    problems = [f"{name}: unexpected {path}" for path in sorted(packed - allowed)]
    problems += [f"{name}: missing {path}" for path in sorted(required - packed)]
    return problems


def _check_wheel(wheel: Path, tracked: set[str]) -> list[str]:
    """
    Check that a wheel holds exactly the package's tracked sources.

    Args:
        wheel: The wheel.
        tracked: The files git tracks.

    Returns:
        One line per problem.
    """
    with zipfile.ZipFile(wheel) as archive:
        names = {name for name in archive.namelist() if not name.endswith("/")}

    packed = {name for name in names if not name.split("/")[0].endswith(".dist-info")}
    sources = {
        path.removeprefix("src/") for path in tracked if path.startswith(PACKAGE)
    }
    return _compare(wheel.name, packed, sources, sources)


def _check_sdist(sdist: Path, tracked: set[str]) -> list[str]:
    """
    Check that an sdist holds the package's tracked sources and nothing untracked.

    Args:
        sdist: The sdist.
        tracked: The files git tracks.

    Returns:
        One line per problem.
    """
    with tarfile.open(sdist) as archive:
        # Every member sits under one top directory, named after the sdist.
        names = {
            member.name.split("/", 1)[1]
            for member in archive.getmembers()
            if member.isfile()
        }

    # The build makes PKG-INFO, and rewrites pyproject.toml, keeping the
    # original beside it.
    packed = names - {"PKG-INFO", "pyproject.toml.orig"}
    sources = {path for path in tracked if path.startswith(PACKAGE)}
    return _compare(sdist.name, packed, tracked, sources)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Check that the sdist and wheel in a directory hold only files that"
            " git tracks, and every tracked file of the package."
        )
    )
    parser.add_argument("dist", type=Path, help="the directory uv build wrote to")
    args = parser.parse_args()

    tracked = _tracked()
    wheels = list(args.dist.glob("*.whl"))
    sdists = list(args.dist.glob("*.tar.gz"))

    if len(wheels) != 1 or len(sdists) != 1:
        sys.exit(f"expected one wheel and one sdist in {args.dist}")

    problems = _check_wheel(wheels[0], tracked) + _check_sdist(sdists[0], tracked)

    if problems:
        sys.exit("\n".join(problems))

    print(f"{wheels[0].name} and {sdists[0].name} look right")


if __name__ == "__main__":
    main()
