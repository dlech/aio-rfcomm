# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The parts of the macOS backend that need neither IOBluetooth nor a radio.

Framing a control message, passing a descriptor with it, routing a reply to
the request that asked for it and turning the helper's errors back into ours
are all ordinary logic, and where the bugs have been -- which is why they are
here rather than left to a Mac with Bluetooth.

None of it imports rubicon, so it runs on Linux as well as on macOS. It does
need Unix sockets -- the control protocol is a socket pair carrying
descriptors -- which is why the whole module steps aside on Windows.
"""

import asyncio
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest

if not hasattr(socket, "AF_UNIX"):
    pytest.skip(
        "the helper's control protocol is built on Unix sockets",
        allow_module_level=True,
    )

from aio_rfcomm.backend.iobluetooth import _reason, _wanted
from aio_rfcomm.backend.iobluetooth._bundle import _identifier, _key
from aio_rfcomm.backend.iobluetooth._helper import (
    Helper,
    HelperFailedError,
    _rebuild,
)
from aio_rfcomm.backend.iobluetooth._protocol import MAX_MESSAGE, Link, ProtocolError
from aio_rfcomm.errors import (
    CloseReason,
    ConnectionFailedError,
    DeviceNotFoundError,
    ServiceNotFoundError,
)


@asynccontextmanager
async def _pair() -> AsyncGenerator[tuple[Link, Link], None]:
    """
    Two ends of a control socket, both closed again afterwards.

    Yields:
        The near end and the far end.
    """
    near, far = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    here, there = Link(near), Link(far)
    try:
        yield here, there
    finally:
        here.close()
        there.close()


@asynccontextmanager
async def _helper() -> AsyncGenerator[tuple[Helper, Link], None]:
    """
    A helper wired to a socket the test plays the other end of.

    Yields:
        The helper, and the far end of its control socket.
    """
    async with _pair() as (here, there):
        helper = Helper(here)
        async with asyncio.TaskGroup() as group:
            reader = group.create_task(helper.deliver())
            try:
                yield helper, there
            finally:
                reader.cancel()


async def _next(link: Link) -> dict[str, object]:
    """
    Take the next message off a link, without its descriptors.

    Args:
        link: The link to read from.

    Returns:
        The message.
    """
    frame = await link.receive()
    assert frame is not None
    return frame[0]


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


async def test_a_message_arrives_as_it_was_sent() -> None:
    async with _pair() as (here, there):
        here.send({"op": "ready", "id": 1})
        assert await _next(there) == {"op": "ready", "id": 1}


async def test_messages_keep_their_boundaries() -> None:
    """
    Two messages written back to back arrive as two, not as one run of bytes.
    """
    async with _pair() as (here, there):
        here.send({"id": 1})
        here.send({"id": 2})
        assert await _next(there) == {"id": 1}
        assert await _next(there) == {"id": 2}


async def test_a_message_too_large_to_send_is_refused() -> None:
    async with _pair() as (here, _there):
        with pytest.raises(ProtocolError, match="too large"):
            here.send({"padding": "x" * MAX_MESSAGE})


async def test_the_far_end_going_away_ends_the_stream() -> None:
    async with _pair() as (here, there):
        there.close()
        assert await here.receive() is None


# --------------------------------------------------------------------------
# Passing descriptors
#
# This is what the whole protocol exists for: the helper opens the channel and
# the library ends up holding the socket.
# --------------------------------------------------------------------------


async def test_a_descriptor_travels_with_its_message() -> None:
    async with _pair() as (here, there):
        read_fd, write_fd = os.pipe()
        try:
            here.send({"id": 1, "ok": 7}, [read_fd])
            frame = await there.receive()
        finally:
            os.close(read_fd)
            os.close(write_fd)

        assert frame is not None
        message, fds = frame
        assert message == {"id": 1, "ok": 7}
        assert len(fds) == 1
        # A copy, not the same number, and ours to close.
        assert fds[0] != read_fd
        os.close(fds[0])


async def test_the_sender_may_close_its_own_descriptor_at_once() -> None:
    """
    A frame can sit in the queue after ``send`` returns, so the link takes a
    copy rather than relying on the caller to keep theirs open.
    """
    async with _pair() as (here, there):
        read_fd, write_fd = os.pipe()
        here.send({"id": 1}, [read_fd])
        os.close(read_fd)
        os.close(write_fd)

        frame = await there.receive()
        assert frame is not None
        _message, fds = frame
        assert len(fds) == 1
        os.stat(fds[0])  # still a live descriptor
        os.close(fds[0])


# --------------------------------------------------------------------------
# Routing replies and events
# --------------------------------------------------------------------------


async def test_a_reply_goes_to_the_request_that_asked() -> None:
    async with _helper() as (helper, there):
        asking = asyncio.create_task(helper.ask("adapters"))
        request = await _next(there)
        there.send({"id": request["id"], "ok": ["one"]})

        result, fds = await asking
        assert result == ["one"]
        assert fds == []


async def test_replies_find_their_own_request_whatever_the_order() -> None:
    async with _helper() as (helper, there):
        first = asyncio.create_task(helper.ask("devices"))
        second = asyncio.create_task(helper.ask("powered"))
        one = await _next(there)
        two = await _next(there)

        # Answered back to front on purpose.
        there.send({"id": two["id"], "ok": True})
        there.send({"id": one["id"], "ok": []})

        assert (await second)[0] is True
        assert (await first)[0] == []


async def test_an_error_reply_comes_back_as_that_error() -> None:
    async with _helper() as (helper, there):
        asking = asyncio.create_task(helper.ask("open", address="00:11"))
        request = await _next(there)
        there.send(
            {
                "id": request["id"],
                "error": "ServiceNotFoundError",
                "message": "00:11 does not offer it",
            }
        )

        with pytest.raises(ServiceNotFoundError, match="does not offer"):
            await asking


async def test_the_helper_going_away_fails_what_was_outstanding() -> None:
    """
    Otherwise a caller waits for an answer from a process that has gone.
    """
    async with _helper() as (helper, there):
        asking = asyncio.create_task(helper.ask("adapters"))
        await _next(there)
        there.close()

        with pytest.raises(HelperFailedError):
            await asking


async def test_asking_after_the_helper_has_gone_fails_at_once() -> None:
    async with _helper() as (helper, there):
        asking = asyncio.create_task(helper.ask("adapters"))
        await _next(there)
        there.close()
        with pytest.raises(HelperFailedError):
            await asking

        with pytest.raises(HelperFailedError):
            await helper.ask("powered")


async def test_the_end_of_a_channel_reaches_that_channel() -> None:
    async with _helper() as (_helper_, there):
        helper = _helper_
        ending = helper.expect(3)
        there.send({"event": "gone", "handle": 3, "reason": "PEER_CLOSED"})

        assert await ending == "PEER_CLOSED"


async def test_another_channels_ending_is_not_mistaken_for_this_one() -> None:
    async with _helper() as (helper, there):
        ending = helper.expect(3)
        there.send({"event": "gone", "handle": 4, "reason": "PEER_CLOSED"})
        await asyncio.sleep(0)

        assert not ending.done()


async def test_a_helper_that_dies_ends_every_channel() -> None:
    """
    From out here a helper that stopped and a link that dropped look the same,
    and either way a reader waiting on the channel has to be let go.
    """
    async with _helper() as (helper, there):
        ending = helper.expect(3)
        there.close()

        assert await ending == "LINK_LOST"


async def test_giving_up_on_a_request_tells_the_helper_to_stop() -> None:
    """
    Opening a channel can wait on a device that never answers, so a caller
    who gives up has to be able to make the helper give up too.
    """
    async with _helper() as (helper, there):
        asking = asyncio.create_task(helper.ask("open", address="00:11"))
        request = await _next(there)

        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        assert await _next(there) == {
            "id": pytest.approx(request["id"] + 1),
            "op": "abandon",
            "of": request["id"],
        }


# --------------------------------------------------------------------------
# Turning the helper's errors back into ours
# --------------------------------------------------------------------------


def test_a_known_error_comes_back_as_itself() -> None:
    assert isinstance(_rebuild("DeviceNotFoundError", "gone"), DeviceNotFoundError)


def test_an_error_we_do_not_know_is_still_a_failure() -> None:
    error = _rebuild("SomethingFromTheFuture", "who knows")
    assert isinstance(error, ConnectionFailedError)
    assert "who knows" in str(error)


def test_an_error_that_takes_a_reason_does_not_break_the_rebuild() -> None:
    """
    Some of our errors are built from a reason rather than a message, and a
    helper that names one must not take the library down with it.
    """
    assert isinstance(_rebuild("ChannelClosedError", "the peer left"), Exception)


def test_something_that_is_not_an_error_at_all_is_not_raised() -> None:
    assert isinstance(
        _rebuild("CloseReason", "not an exception"), ConnectionFailedError
    )


# --------------------------------------------------------------------------
# Odds and ends
# --------------------------------------------------------------------------


def test_a_close_reason_survives_the_trip() -> None:
    assert _reason("PEER_CLOSED") is CloseReason.PEER_CLOSED


def test_a_reason_we_do_not_know_is_treated_as_a_lost_link() -> None:
    assert _reason("SOMETHING_NEW") is CloseReason.LINK_LOST


def test_no_filter_matches_everything() -> None:
    assert _wanted(None) == []


def test_bundles_for_different_names_are_told_apart() -> None:
    """
    macOS remembers a Bluetooth decision against the bundle identifier, so two
    programs asking under different names must not share one.
    """
    assert _identifier("Robot Console") != _identifier("Track Editor")
    assert _identifier("Robot Console") == "dev.aiorfcomm.helper.robot-console"


def test_a_name_with_nothing_usable_in_it_still_makes_an_identifier() -> None:
    assert _identifier("...") == "dev.aiorfcomm.helper.unnamed"


def test_the_cache_key_follows_what_goes_into_the_bundle(tmp_path) -> None:
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"not really an interpreter")

    same = _key(interpreter, "Name", "Reason")
    assert _key(interpreter, "Name", "Reason") == same
    assert _key(interpreter, "Other", "Reason") != same
    assert _key(interpreter, "Name", "Other reason") != same


# --------------------------------------------------------------------------
# Telling "not allowed" from "switched off"
# --------------------------------------------------------------------------


def test_a_refused_permission_is_not_reported_as_a_dead_radio() -> None:
    """
    macOS reports Bluetooth as switched off to a process that may not use it,
    so the obvious message sends someone to turn on a radio that is already
    on -- which is exactly the wrong place to look. This cost an evening
    once.
    """
    from aio_rfcomm.backend.iobluetooth import Authorization, _no_radio
    from aio_rfcomm.errors import PermissionDeniedError

    error = _no_radio(Authorization.DENIED, "thor")
    assert isinstance(error, PermissionDeniedError)
    assert "System Settings" in str(error)


def test_an_unanswered_prompt_says_so() -> None:
    from aio_rfcomm.backend.iobluetooth import Authorization, _no_radio
    from aio_rfcomm.errors import PermissionDeniedError

    error = _no_radio(Authorization.NOT_DETERMINED, "thor")
    assert isinstance(error, PermissionDeniedError)
    assert "dialog" in str(error)


def test_a_restriction_says_it_is_not_this_program_s_to_undo() -> None:
    from aio_rfcomm.backend.iobluetooth import Authorization, _no_radio
    from aio_rfcomm.errors import PermissionDeniedError

    assert isinstance(
        _no_radio(Authorization.RESTRICTED, "thor"), PermissionDeniedError
    )


def test_an_allowed_program_with_a_dead_radio_is_told_to_turn_it_on() -> None:
    from aio_rfcomm.backend.iobluetooth import Authorization, _no_radio
    from aio_rfcomm.errors import AdapterOffError

    error = _no_radio(Authorization.ALLOWED_ALWAYS, "thor")
    assert isinstance(error, AdapterOffError)
    assert "thor" in str(error)
    assert "switched off" in str(error)


def test_the_renaming_trap_is_in_the_refusal_message() -> None:
    """
    The permission is remembered against the name given to
    prompt_under_own_name, so renaming an application asks again -- and the
    symptom points nowhere near the cause.
    """
    from aio_rfcomm.backend.iobluetooth import Authorization, _no_radio

    assert "prompt_under_own_name" in str(_no_radio(Authorization.DENIED, None))
