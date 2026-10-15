# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
What the channel handle adds over the backend's: arguments checked, and a
cancelled send not allowed to pass unnoticed.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from aio_rfcomm.availability import Availability
from aio_rfcomm.channel import RfcommChannel
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelBrokenError,
    ChannelBusyError,
    ChannelClosedError,
    CloseReason,
    DelimiterNotFoundError,
)
from tests.fake import FakeChannel, FakeProvider


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


async def test_bluetooth_going_matters_more_than_a_cancelled_send(
    opened: tuple[RfcommChannel, FakeChannel], provider: FakeProvider
) -> None:
    channel, fake = opened
    await _cancel_a_send(channel, fake)
    provider.report(Availability.SWITCHED_OFF)

    with pytest.raises(BluetoothUnavailableError):
        await channel.send(b"again")


# --------------------------------------------------------------------------
# Taking exact amounts and delimited messages
# --------------------------------------------------------------------------


async def test_exactly_gathers_across_arrivals(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    receiving = asyncio.ensure_future(channel.receive_exactly(5))
    fake.deliver(b"ab")
    await asyncio.sleep(0)
    assert not receiving.done()
    fake.deliver(b"cdefg")

    assert await receiving == b"abcde"
    assert await channel.receive() == b"fg"


async def test_a_cancelled_exactly_leaves_what_it_gathered(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    receiving = asyncio.ensure_future(channel.receive_exactly(5))
    fake.deliver(b"abc")
    await asyncio.sleep(0)
    assert fake.undelivered == 0, "the call should have taken what arrived"
    receiving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await receiving

    fake.deliver(b"de")
    async with asyncio.timeout(1):
        assert await channel.receive_exactly(5) == b"abcde"


async def test_an_end_part_way_leaves_what_arrived_for_receive(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"abc")
    fake.go_away(CloseReason.PEER_CLOSED)

    with pytest.raises(ChannelClosedError):
        await channel.receive_exactly(5)
    assert await channel.receive() == b"abc"
    with pytest.raises(ChannelClosedError):
        await channel.receive()


async def test_until_finds_a_delimiter_split_across_arrivals(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    receiving = asyncio.ensure_future(channel.receive_until(b"\r\n", max_bytes=100))
    fake.deliver(b"hello\r")
    await asyncio.sleep(0)
    assert not receiving.done()
    fake.deliver(b"\nworld\r\n")

    assert await receiving == b"hello"
    assert await channel.receive_until(b"\r\n", max_bytes=100) == b"world"


async def test_until_takes_one_message_of_several(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"one\ntwo\n\nthree")

    assert await channel.receive_until(b"\n", max_bytes=10) == b"one"
    assert await channel.receive_until(b"\n", max_bytes=10) == b"two"
    assert await channel.receive_until(b"\n", max_bytes=10) == b""
    assert await channel.receive() == b"three"


async def test_until_gives_up_at_its_limit_and_takes_nothing(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"far too long a line\n")

    with pytest.raises(DelimiterNotFoundError):
        await channel.receive_until(b"\n", max_bytes=5)
    assert await channel.receive() == b"far too long a line\n"


async def test_until_counts_the_delimiter_in_its_limit(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"abcd\n")
    assert await channel.receive_until(b"\n", max_bytes=5) == b"abcd"

    fake.deliver(b"abcde\n")
    with pytest.raises(DelimiterNotFoundError):
        await channel.receive_until(b"\n", max_bytes=5)


@pytest.mark.parametrize(
    "second",
    [
        lambda channel: channel.receive(),
        lambda channel: channel.receive_exactly(1),
        lambda channel: channel.receive_until(b"\n", max_bytes=10),
    ],
)
async def test_every_way_of_receiving_shares_one_reader(
    opened: tuple[RfcommChannel, FakeChannel],
    second: Callable[[RfcommChannel], Awaitable[bytes]],
) -> None:
    channel, _fake = opened
    waiting = asyncio.ensure_future(channel.receive_until(b"\n", max_bytes=10))
    await asyncio.sleep(0)
    with pytest.raises(ChannelBusyError):
        await second(channel)
    # A mistake in the arguments is still reported as that.
    with pytest.raises(ValueError):
        await channel.receive_exactly(0)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.parametrize(
    "call",
    [
        lambda channel: channel.receive_exactly(0),
        lambda channel: channel.receive_until(b"", max_bytes=5),
        lambda channel: channel.receive_until(b"\n", max_bytes=0),
        lambda channel: channel.receive_until(b"\r\n", max_bytes=1),
    ],
)
async def test_nonsense_arguments_are_refused(
    opened: tuple[RfcommChannel, FakeChannel],
    call: Callable[[RfcommChannel], Awaitable[bytes]],
) -> None:
    channel, _fake = opened
    with pytest.raises(ValueError):
        await call(channel)


async def test_until_gives_up_on_a_peer_that_never_sends_a_delimiter(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"abcde")
    with pytest.raises(DelimiterNotFoundError):
        await channel.receive_until(b"\n", max_bytes=5)


async def test_until_gives_up_as_soon_as_its_limit_arrives(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"abc")
    receiving = asyncio.ensure_future(channel.receive_until(b"\n", max_bytes=5))
    await asyncio.sleep(0)
    fake.deliver(b"de")

    with pytest.raises(DelimiterNotFoundError):
        async with asyncio.timeout(1):
            await receiving
    assert await channel.receive() == b"abcde"


async def test_receive_takes_from_what_is_already_here_by_its_limit(
    opened: tuple[RfcommChannel, FakeChannel],
) -> None:
    channel, fake = opened
    fake.deliver(b"abcdef")
    assert await channel.receive_exactly(2) == b"ab"
    assert await channel.receive(2) == b"cd"
    assert await channel.receive() == b"ef"


async def test_bluetooth_going_part_way_leaves_what_arrived_for_receive(
    opened: tuple[RfcommChannel, FakeChannel], provider: FakeProvider
) -> None:
    channel, fake = opened
    fake.deliver(b"abc")
    provider.report(Availability.SWITCHED_OFF)

    with pytest.raises(BluetoothUnavailableError):
        await channel.receive_exactly(5)
    assert await channel.receive() == b"abc"
    with pytest.raises(BluetoothUnavailableError):
        await channel.receive()
