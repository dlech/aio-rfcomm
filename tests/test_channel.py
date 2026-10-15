# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
What the channel handle adds over the backend's: arguments checked, and a
cancelled send not allowed to pass unnoticed.
"""

import asyncio

import pytest

from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.errors import ChannelBrokenError, ChannelClosedError, CloseReason
from tests.fake import FakeChannel


@pytest.mark.parametrize("max_bytes", [0, -1])
async def test_asking_for_nothing_is_refused(
    opened: tuple[RfcommChannel, FakeChannel], max_bytes: int
) -> None:
    channel, _fake = opened
    with pytest.raises(ValueError, match="at least 1"):
        await channel.receive(max_bytes)


async def test_the_end_is_raised_rather_than_returned(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"bye")
    fake.go_away(CloseReason.PEER_CLOSED)

    assert await channel.receive() == b"bye"
    with pytest.raises(ChannelClosedError) as caught:
        await channel.receive()
    assert caught.value.reason is CloseReason.PEER_CLOSED


async def _cancel_a_send(channel: RfcommChannel, fake: FakeChannel) -> None:
    fake.send_blocks = True
    sending = asyncio.ensure_future(channel.send(b"half a frame"))
    await asyncio.sleep(0)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    fake.send_blocks = False


async def test_a_send_after_a_cancelled_one_is_refused(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    """
    The usual way to get here: a deadline around a send fires, and the send
    is tried again. Whether the peer got the first is unknown, so the retry
    is refused where it happens rather than corrupting the stream unseen.
    """
    channel, fake = opened
    await _cancel_a_send(channel, fake)

    with pytest.raises(ChannelBrokenError):
        await channel.send(b"again")
    assert not fake.sent


async def test_receiving_is_unaffected_by_a_cancelled_send(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    await _cancel_a_send(channel, fake)

    fake.deliver(b"still listening")
    assert await channel.receive() == b"still listening"


async def test_the_end_matters_more_than_a_cancelled_send(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    await _cancel_a_send(channel, fake)
    fake.go_away(CloseReason.LINK_LOST)

    with pytest.raises(ChannelClosedError) as caught:
        await channel.send(b"again")
    assert caught.value.reason is CloseReason.LINK_LOST
