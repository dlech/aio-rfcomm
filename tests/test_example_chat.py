# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The framing in the chat example.

Everything else in that example is wiring, but splitting a byte stream into
lines is the part a reader is most likely to copy and the part most likely to
be subtly wrong: RFCOMM keeps no message boundaries, so a line can arrive in
pieces and several can arrive at once. Getting it wrong looks fine against a
peer that happens to send one tidy line per write.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("prompt_toolkit", reason="the chat example needs prompt_toolkit")

_source = Path(__file__).parent.parent / "examples" / "chat.py"
_spec = importlib.util.spec_from_file_location("example_chat", _source)
assert _spec is not None and _spec.loader is not None
chat: Any = importlib.util.module_from_spec(_spec)
sys.modules["example_chat"] = chat
_spec.loader.exec_module(chat)


def test_a_whole_line_comes_out_whole() -> None:
    buffer = bytearray()
    assert list(chat.split_lines(buffer, b"hello\n")) == ["hello"]
    assert buffer == b""


def test_a_line_split_across_arrivals_is_rejoined() -> None:
    buffer = bytearray()
    assert list(chat.split_lines(buffer, b"hel")) == []
    assert list(chat.split_lines(buffer, b"lo\n")) == ["hello"]


def test_several_lines_in_one_arrival_all_come_out() -> None:
    buffer = bytearray()
    assert list(chat.split_lines(buffer, b"one\ntwo\nthree\n")) == [
        "one",
        "two",
        "three",
    ]


def test_a_trailing_partial_line_is_kept_for_next_time() -> None:
    buffer = bytearray()
    assert list(chat.split_lines(buffer, b"done\nnot yet")) == ["done"]
    assert buffer == b"not yet"
    assert list(chat.split_lines(buffer, b" finished\n")) == ["not yet finished"]


def test_carriage_returns_are_not_shown() -> None:
    """
    A peer on a platform that ends lines with CRLF should not leave a stray
    carriage return in the middle of the display.
    """
    buffer = bytearray()
    assert list(chat.split_lines(buffer, b"windows\r\n")) == ["windows"]


def test_an_empty_line_is_still_a_line() -> None:
    buffer = bytearray()
    assert list(chat.split_lines(buffer, b"\n")) == [""]


def test_bytes_that_are_not_utf8_do_not_raise() -> None:
    """
    The peer is not obliged to send valid text, and a chat window falling over
    because of one bad byte would be worse than showing a replacement.
    """
    buffer = bytearray()
    assert list(chat.split_lines(buffer, b"caf\xff\n")) == ["caf�"]
