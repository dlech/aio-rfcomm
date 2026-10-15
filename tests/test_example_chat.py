# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
How the chat example shows what arrives.

Splitting the stream into lines is the library's ``receive_until``, tested
with the channel. What is left here is turning a line into something fit to
print, whatever the peer sent.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.errors import ChannelClosedError
from tests.fake import FakeChannel

pytest.importorskip("prompt_toolkit", reason="the chat example needs prompt_toolkit")

_source = Path(__file__).parent.parent / "examples" / "chat.py"
_spec = importlib.util.spec_from_file_location("example_chat", _source)
assert _spec is not None and _spec.loader is not None
chat: Any = importlib.util.module_from_spec(_spec)
sys.modules["example_chat"] = chat
_spec.loader.exec_module(chat)


async def _shown(
    opened: tuple[RfcommChannel, FakeChannel],
    capsys: pytest.CaptureFixture[str],
    arrived: bytes,
) -> list[str]:
    channel, fake = opened
    fake.deliver(arrived)
    fake.go_away()
    with pytest.raises(ChannelClosedError):
        await chat.show(channel, "peer")
    return capsys.readouterr().out.splitlines()


async def test_each_line_is_shown_as_it_completes(
    opened: tuple[RfcommChannel, FakeChannel], capsys: pytest.CaptureFixture[str]
) -> None:
    shown = await _shown(opened, capsys, b"one\ntwo\n\n")
    assert shown == ["peer: one", "peer: two", "peer: "]


async def test_carriage_returns_are_not_shown(
    opened: tuple[RfcommChannel, FakeChannel], capsys: pytest.CaptureFixture[str]
) -> None:
    """
    A peer on a platform that ends lines with CRLF should not leave a stray
    carriage return in the middle of the display.
    """
    assert await _shown(opened, capsys, b"windows\r\n") == ["peer: windows"]


async def test_bytes_that_are_not_utf8_do_not_raise(
    opened: tuple[RfcommChannel, FakeChannel], capsys: pytest.CaptureFixture[str]
) -> None:
    """
    The peer is not obliged to send valid text, and a chat window falling over
    because of one bad byte would be worse than showing a replacement.
    """
    assert await _shown(opened, capsys, b"caf\xff\n") == ["peer: caf\ufffd"]


async def test_an_overlong_line_is_shown_in_pieces(
    opened: tuple[RfcommChannel, FakeChannel], capsys: pytest.CaptureFixture[str]
) -> None:
    line = b"x" * (chat.MAX_LINE + 10)
    shown = await _shown(opened, capsys, line + b"\n")
    assert shown == ["peer: " + "x" * chat.MAX_LINE, "peer: " + "x" * 10]
