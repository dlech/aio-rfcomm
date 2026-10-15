# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Keeping the library's own task groups out of what callers see.
"""

import pytest

from aio_rfcomm._teardown import unwrap_lone_error


def test_a_lone_failure_comes_out_as_itself() -> None:
    with pytest.raises(ValueError, match="alone"), unwrap_lone_error():
        raise ExceptionGroup("one", [ValueError("alone")])


def test_several_failures_stay_a_group() -> None:
    with pytest.raises(ExceptionGroup), unwrap_lone_error():
        raise ExceptionGroup("two", [ValueError("one"), KeyError("two")])


def test_anything_else_passes_through() -> None:
    with pytest.raises(KeyError), unwrap_lone_error():
        raise KeyError("plain")
