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
import sys
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import pytest

if not hasattr(socket, "AF_UNIX"):
    pytest.skip(
        "the helper's control protocol is built on Unix sockets",
        allow_module_level=True,
    )

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth import (
    IOBluetoothBluetooth,
    IOBluetoothChannel,
    _availability,
    _not_allowed,
    _wanted,
)
from aio_rfcomm.backend.iobluetooth.bundle import _identifier, _key
from aio_rfcomm.backend.iobluetooth.codes import CBManagerAuthorization, CBManagerState
from aio_rfcomm.backend.iobluetooth.process import (
    Helper,
    HelperFailedError,
    _rebuild,
    run_helper,
)
from aio_rfcomm.backend.iobluetooth.protocol import MAX_MESSAGE, Link, ProtocolError
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
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
        there.send({"event": "gone", "handle": 3})

        async with asyncio.timeout(1):
            await ending.wait()


async def test_another_channels_ending_is_not_mistaken_for_this_one() -> None:
    async with _helper() as (helper, there):
        ending = helper.expect(3)
        there.send({"event": "gone", "handle": 4})
        await asyncio.sleep(0)

        assert not ending.said()


async def test_a_helper_that_dies_ends_every_channel() -> None:
    """
    A reader waiting on a channel of a helper that stopped has to be let go.
    """
    async with _helper() as (helper, there):
        ending = helper.expect(3)
        there.close()

        async with asyncio.timeout(1):
            await ending.wait()


DEVICE = RfcommDeviceInfo("00:11:22:33:44:55")


def _bluetooth(helper: Helper) -> IOBluetoothBluetooth:
    bluetooth = IOBluetoothBluetooth()
    bluetooth.helper = helper
    return bluetooth


@asynccontextmanager
async def _channel(
    helper: Helper, there: Link, handle: int
) -> AsyncGenerator[tuple[IOBluetoothChannel, socket.socket], None]:
    """
    A channel opened through the library, with the test as the helper.

    Args:
        helper: The library's end of the control socket.
        there: The test's end, standing in for the helper.
        handle: The handle the test hands back for the channel.

    Yields:
        The open channel, and the far end of its socket.
    """
    bluetooth = _bluetooth(helper)
    async with AsyncExitStack() as stack:
        opening = asyncio.ensure_future(
            stack.enter_async_context(bluetooth.open_channel(DEVICE, 4))
        )
        asked = await _next(there)
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        stack.callback(ours.close)
        with theirs:
            there.send({"id": asked["id"], "ok": handle}, [theirs.fileno()])
        async with asyncio.timeout(1):
            channel = await opening
        assert isinstance(channel, IOBluetoothChannel)
        try:
            yield channel, ours
        finally:
            # Let the close finish, as the helper would once it had let go.
            there.send({"event": "finished", "handle": handle})


async def test_a_reader_giving_up_leaves_the_others_their_answer() -> None:
    """
    The stream and the report of its end come over different sockets, so a
    reader can see the end first and wait for the report. Canceling a task
    that awaits a future cancels the future, so a report shared that way was
    lost to everyone else the moment that reader gave up.
    """
    async with (
        _helper() as (helper, there),
        _channel(helper, there, 7) as (channel, far),
    ):
        far.close()
        reading = asyncio.ensure_future(channel.receive())
        await asyncio.sleep(0.01)
        assert not reading.done(), "the reader should wait for the helper's report"
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)

        there.send({"event": "gone", "handle": 7})
        async with asyncio.timeout(1):
            with pytest.raises(ChannelClosedError):
                await channel.receive()


async def test_giving_up_on_a_request_tells_the_helper_to_stop() -> None:
    """
    Opening a channel can wait on a device that never answers, so a caller
    who gives up has to be able to make the helper give up too.
    """
    async with _helper() as (helper, there):
        asking = asyncio.create_task(helper.ask("open", address="00:11"))
        request = await _next(there)
        number = request["id"]
        assert isinstance(number, int)

        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        assert await _next(there) == {"id": number + 1, "op": "abandon", "of": number}


# --------------------------------------------------------------------------
# Turning the helper's errors back into ours
# --------------------------------------------------------------------------


def test_a_known_error_comes_back_as_itself() -> None:
    assert isinstance(_rebuild("DeviceNotFoundError", "gone"), DeviceNotFoundError)


@pytest.mark.parametrize(
    "reason",
    [
        Availability.SWITCHED_OFF,
        Availability.NOT_ALLOWED,
        Availability.NO_ADAPTER,
        Availability.SERVICE_UNAVAILABLE,
        Availability.UNSUPPORTED,
    ],
)
def test_bluetooth_being_unusable_survives_the_socket(
    reason: Availability,
) -> None:
    """
    A channel open can fail because the radio went away, and the helper is
    where that is noticed, so the reason has to cross the socket too.
    """
    rebuilt = _rebuild("BluetoothUnavailableError", "the radio went away", reason.name)
    assert isinstance(rebuilt, BluetoothUnavailableError)
    assert rebuilt.reason is reason
    assert "the radio went away" in str(rebuilt)


@pytest.mark.parametrize("reason", [None, "USABLE", "SOMETHING_NEW"])
def test_an_unavailability_without_a_reason_is_still_a_failure(
    reason: str | None,
) -> None:
    rebuilt = _rebuild("BluetoothUnavailableError", "the radio went away", reason)
    assert isinstance(rebuilt, ConnectionFailedError)
    assert "the radio went away" in str(rebuilt)


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
        _rebuild("Availability", "not an exception"), ConnectionFailedError
    )


# --------------------------------------------------------------------------
# Odds and ends
# --------------------------------------------------------------------------


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
    denied = CBManagerAuthorization.DENIED
    assert _availability(CBManagerState.POWERED_OFF, denied) is Availability.NOT_ALLOWED
    assert "System Settings" in _not_allowed(denied)


def test_an_unanswered_prompt_says_so() -> None:
    unasked = CBManagerAuthorization.NOT_DETERMINED
    assert (
        _availability(CBManagerState.POWERED_OFF, unasked) is Availability.NOT_ALLOWED
    )
    assert "dialog" in _not_allowed(unasked)


def test_a_restriction_says_it_is_not_this_program_s_to_undo() -> None:
    assert "parental controls" in _not_allowed(CBManagerAuthorization.RESTRICTED)


def test_an_allowed_program_with_a_dead_radio_is_told_to_turn_it_on() -> None:
    allowed = CBManagerAuthorization.ALLOWED_ALWAYS
    assert (
        _availability(CBManagerState.POWERED_OFF, allowed) is Availability.SWITCHED_OFF
    )


def test_the_renaming_trap_is_in_the_refusal_message() -> None:
    """
    The permission is remembered against the name given to
    prompt_under_name, so renaming an application asks again -- and the
    symptom points nowhere near the cause.
    """
    assert "prompt_under_name" in _not_allowed(CBManagerAuthorization.DENIED)


@pytest.mark.parametrize(
    ("state", "availability"),
    [
        (CBManagerState.POWERED_ON, Availability.USABLE),
        (CBManagerState.UNAUTHORIZED, Availability.NOT_ALLOWED),
        (CBManagerState.RESETTING, Availability.SERVICE_UNAVAILABLE),
        (CBManagerState.UNSUPPORTED, Availability.UNSUPPORTED),
        (CBManagerState.UNKNOWN, None),
        (99, None),
    ],
)
def test_each_manager_state_means_one_thing(
    state: int, availability: Availability | None
) -> None:
    allowed = CBManagerAuthorization.ALLOWED_ALWAYS
    assert _availability(state, allowed) is availability


# --------------------------------------------------------------------------
# The manager's state
# --------------------------------------------------------------------------


async def test_the_helper_s_states_arrive_in_order() -> None:
    async with _helper() as (helper, there):
        for state in (CBManagerState.RESETTING, CBManagerState.POWERED_ON):
            there.send({"event": "state", "state": state, "authorization": 3})
        async with asyncio.timeout(1):
            assert await helper.next_state() == (CBManagerState.RESETTING, 3)
            assert await helper.next_state() == (CBManagerState.POWERED_ON, 3)


async def test_a_reader_giving_up_loses_no_state() -> None:
    async with _helper() as (helper, there):
        impatient = asyncio.ensure_future(helper.next_state())
        await asyncio.sleep(0.01)
        impatient.cancel()
        await asyncio.gather(impatient, return_exceptions=True)

        there.send(
            {"event": "state", "state": CBManagerState.POWERED_OFF, "authorization": 3}
        )
        async with asyncio.timeout(1):
            assert await helper.next_state() == (CBManagerState.POWERED_OFF, 3)


async def test_states_from_a_helper_that_has_gone_are_not_handed_out() -> None:
    """
    They described a helper that is no longer there, and acting on one --
    powered on, say -- would report Bluetooth usable with nothing behind it.
    """
    async with _helper() as (helper, there):
        there.send(
            {"event": "state", "state": CBManagerState.POWERED_ON, "authorization": 3}
        )
        there.close()
        async with asyncio.timeout(1):
            while not helper.stopped:
                await asyncio.sleep(0)
            with pytest.raises(HelperFailedError):
                await helper.next_state()


async def test_waiting_for_a_state_hears_the_helper_go() -> None:
    async with _helper() as (helper, there):
        waiting = asyncio.ensure_future(helper.next_state())
        await asyncio.sleep(0.01)
        there.close()
        async with asyncio.timeout(1):
            with pytest.raises(HelperFailedError):
                await waiting
            # And again, for whoever asks next.
            with pytest.raises(HelperFailedError):
                await helper.next_state()


# --------------------------------------------------------------------------
# Letting go of a device
# --------------------------------------------------------------------------


async def test_a_close_waits_for_the_helper_to_let_the_device_go() -> None:
    """
    Closing this end of the socket is what tells the helper to stop, but it
    holds the device for as long as its own teardown takes and refuses another
    channel to that device meanwhile. So the close is not over until the
    helper says it is.
    """
    async with _helper() as (helper, there):
        helper.expect(7)
        waiting = asyncio.ensure_future(helper.wait_until_finished(7))
        await asyncio.sleep(0.01)
        assert not waiting.done()

        there.send({"event": "finished", "handle": 7})
        async with asyncio.timeout(1):
            await waiting


async def test_a_helper_that_has_gone_is_holding_nothing() -> None:
    """
    Waiting for a confirmation that can no longer come would wedge every later
    open to that device.
    """
    async with _helper() as (helper, there):
        helper.expect(7)
        waiting = asyncio.ensure_future(helper.wait_until_finished(7))
        await asyncio.sleep(0.01)
        assert not waiting.done()

        there.close()
        async with asyncio.timeout(1):
            await waiting


async def test_a_channel_nobody_registered_is_not_waited_for() -> None:
    async with _helper() as (helper, _there):
        async with asyncio.timeout(1):
            await helper.wait_until_finished(999)


async def test_closing_a_channel_does_not_return_before_the_helper_is_done() -> None:
    """
    The whole point of the confirmation: a caller whose close has returned can
    open another channel to that device, so the close has to outlast the
    helper's own teardown.
    """
    async with _helper() as (helper, there):
        bluetooth = _bluetooth(helper)
        stack = AsyncExitStack()
        # Entered as a task, because the request only goes out once it is.
        opening = asyncio.ensure_future(
            stack.enter_async_context(bluetooth.open_channel(DEVICE, 4))
        )

        asked = await _next(there)
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with theirs:
            there.send({"id": asked["id"], "ok": 7}, [theirs.fileno()])
        async with asyncio.timeout(1):
            await opening

        with ours:
            closing = asyncio.ensure_future(stack.aclose())
            await asyncio.sleep(0.05)
            assert not closing.done(), "closed without waiting for the helper"

            there.send({"event": "finished", "handle": 7})
            async with asyncio.timeout(1):
                await closing


@pytest.mark.skipif(sys.platform != "darwin", reason="IOBluetooth is macOS only")
async def test_the_helper_says_so_when_it_has_let_the_device_go() -> None:
    """
    The other half of the confirmation, which the library side cannot show:
    that the helper says it has let go, and says it even when the open failed
    and there was never a channel at all.

    Everything from the helper's request handling down through the native
    layer runs. Only the framework is stood in for.
    """
    from aio_rfcomm.backend.iobluetooth.helper import __main__ as helper_main
    from tests.fake_iobluetooth import fake_framework

    async with _pair() as (here, there):
        with pytest.MonkeyPatch.context() as patch:
            fake_framework(patch, answers=False, connected=False)
            with pytest.raises(DeviceNotFoundError):
                await helper_main._open(
                    here, 1, {"address": "00:11:22:33:44:55", "channel": 4}
                )

        said, _fds = await there.receive() or ({}, [])
        assert said["event"] == "finished"


@pytest.mark.skipif(sys.platform != "darwin", reason="IOBluetooth is macOS only")
async def test_a_helper_that_ran_the_program_does_not_start_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured with PyInstaller 6.22.3 on macOS 26.6.2: without the run-time
    hook, the helper of a frozen program runs the whole program again, which
    opens Bluetooth and starts a helper of its own.
    """
    monkeypatch.setenv("AIO_RFCOMM_CONTROL_FD", "9")
    with pytest.raises(HelperFailedError, match="ran the program instead"):
        async with run_helper():
            pass
