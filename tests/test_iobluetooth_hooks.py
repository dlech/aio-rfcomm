# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Tests for the hooks that keep what CoreBluetooth and IOBluetooth would
throw away.
"""

import ctypes
import os
import socket
import sys
import threading
from ctypes import c_int
from typing import TYPE_CHECKING, Any, cast

assert not TYPE_CHECKING or sys.platform == "darwin"

import pytest

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)

from rubicon.objc import NSObject, ObjCInstance, ns_from_py, objc_method, objc_property

from aio_rfcomm.backend.iobluetooth.helper.native import hooks
from aio_rfcomm.backend.iobluetooth.helper.native.channel import (
    ChannelDelegate,
    unread_by_channel,
)
from tests.fake_iobluetooth import (
    ADDRESS,
    # An autouse fixture, used by being imported.
    clean_state,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)


@pytest.fixture
def results(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    An empty record of bluetoothd's results, for this test alone.
    """
    monkeypatch.setattr(hooks, "open_results", {})


class _Peer(NSObject):
    """
    A ``CBClassicPeer``, which the hook only asks for its address.
    """

    addressString = objc_property()


def _peer() -> NSObject:
    peer = _Peer.alloc().init()
    # As measured on macOS 26.6.2: CBClassicPeer writes the address upper case
    # with colons.
    cast(Any, peer).addressString = "00:1A:7D:DA:71:0C"
    return peer


def _pointer(thing: object) -> int:
    return cast(int, cast(Any, thing).ptr.value)


def _read(info: dict[str, int]) -> hooks._OpenResult:
    ns = ns_from_py(cast(Any, info))
    return hooks._read_open_result(_pointer(_peer()), _pointer(ns))


async def test_the_result_the_daemon_sent_is_noted_by_address_and_channel(
    results: None,
) -> None:
    hooks._note_open_result(
        _read({"kCBMsgArgResult": 913, "kCBMsgArgRFCOMMChannelID": 12})
    )
    assert hooks.open_results == {(ADDRESS, 12): 913}


async def test_a_result_without_a_channel_is_ignored(results: None) -> None:
    hooks._note_open_result(_read({"kCBMsgArgResult": 913}))
    assert hooks.open_results == {}


async def test_a_value_of_another_type_is_left_unread(results: None) -> None:
    """
    Sending it intValue would raise an Objective-C exception on the
    framework's thread, which nothing could catch.
    """
    said = _read({"kCBMsgArgResult": 913, "kCBMsgArgSocket": cast(Any, "eight")})
    assert said.result == 913
    assert said.socket is None


def test_the_daemon_s_report_is_read_off_the_main_thread() -> None:
    """
    Measured on macOS 26.6.2: the framework calls the hook on a thread of its
    own, where rubicon racing the main thread to load a class's methods ended a
    helper.
    """
    peer = _peer()
    info = cast(
        ObjCInstance,
        ns_from_py({"kCBMsgArgResult": 913, "kCBMsgArgRFCOMMChannelID": 12}),
    )
    read: list[hooks._OpenResult] = []
    framework = threading.Thread(
        target=lambda: read.append(
            hooks._read_open_result(_pointer(peer), _pointer(info))
        )
    )
    framework.start()
    framework.join()
    assert read == [hooks._OpenResult(ADDRESS, 12, 913, None)]


def _failed_open(result: int, descriptor: int) -> hooks._OpenResult:
    return hooks._OpenResult(ADDRESS, 4, result, descriptor)


def _is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return False

    return True


# Never set: the stream's own read waits, as it did on macOS 26.6.2 on a socket
# that had already ended.
_never = threading.Event()


class _Stream(NSObject):
    """
    The input stream of a channel, over a real socket.
    """

    @objc_method
    def hasBytesAvailable(self) -> bool:
        # Yes even with nothing there, as measured on macOS 26.6.2.
        return True

    @objc_method
    def propertyForKey_(self, key: ObjCInstance) -> ObjCInstance:
        descriptor: int | None = cast(Any, self).descriptor
        said = b""

        if str(key) == "kCFStreamPropertySocketNativeHandle" and descriptor is not None:
            said = bytes(c_int(descriptor))

        return cast(ObjCInstance, ns_from_py(said))

    @objc_method
    def read_maxLength_(self, buffer: ctypes.c_void_p, length: ctypes.c_ulong) -> int:
        _never.wait()
        return 0


class _RFCOMMChannel(NSObject):
    """
    CoreBluetooth's ``CBRFCOMMChannel``, which only gives its stream here.
    """

    inputStream = objc_property()


class _ClosingChannel(NSObject):
    """
    An ``IOBluetoothRFCOMMChannel`` as its removal finds it.
    """

    rfcommchannel = objc_property()


def _closing_channel(descriptor: int | None) -> NSObject:
    stream = _Stream.alloc().init()
    cast(Any, stream).descriptor = descriptor
    rfcomm = _RFCOMMChannel.alloc().init()
    cast(Any, rfcomm).inputStream = stream
    channel = _ClosingChannel.alloc().init()
    cast(Any, channel).rfcommchannel = rfcomm
    return channel


def _read_unread_on_a_thread(channel: NSObject) -> bytes:
    # A thread with a bound, so that a read that waits fails the test rather
    # than hanging the suite.
    read: list[bytes] = []
    framework = threading.Thread(
        target=lambda: read.append(hooks._read_unread(_pointer(channel))),
        daemon=True,
    )
    framework.start()
    framework.join(5)
    assert read, "the read waited"
    return read[0]


def test_what_a_closing_channel_had_not_read_is_read_out() -> None:
    left = bytes(range(256)) * 20 + b"tail"
    ours, theirs = socket.socketpair()

    with ours, theirs:
        theirs.sendall(left)
        theirs.shutdown(socket.SHUT_WR)

        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == left


def test_a_closing_channel_with_nothing_unread_does_not_wait() -> None:
    ours, theirs = socket.socketpair()

    with ours, theirs:
        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == b""


def test_a_socket_that_has_ended_with_nothing_left_does_not_wait() -> None:
    ours, theirs = socket.socketpair()

    with ours:
        theirs.close()

        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == b""


def test_what_arrived_is_read_out_without_waiting_for_the_end() -> None:
    ours, theirs = socket.socketpair()

    with ours, theirs:
        theirs.sendall(b"tail")

        assert _read_unread_on_a_thread(_closing_channel(ours.fileno())) == b"tail"


def test_a_stream_that_does_not_say_its_socket_has_nothing_unread() -> None:
    assert _read_unread_on_a_thread(_closing_channel(None)) == b""


def test_a_channel_with_no_stream_left_has_nothing_unread() -> None:
    channel = _ClosingChannel.alloc().init()

    assert hooks._read_unread(_pointer(channel)) == b""


async def test_bytes_read_out_at_close_arrive_before_the_end() -> None:
    delegate = cast(Any, ChannelDelegate.alloc().init())
    channel = _ClosingChannel.alloc().init()
    unread_by_channel[_pointer(channel)] = b"tail"

    delegate.rfcommChannelClosed_(channel)

    assert delegate.arrived.get_nowait() == b"tail"
    assert delegate.ended.is_set()
    assert not unread_by_channel


def test_the_null_device_a_failed_open_leaves_is_closed() -> None:
    """
    Measured on macOS 26.6.2: one /dev/null descriptor left open per refused
    channel.
    """
    descriptor = os.open(os.devnull, os.O_RDONLY)
    hooks._close_unclaimed_socket(_failed_open(913, descriptor))
    assert not _is_open(descriptor)


def test_a_real_socket_is_never_closed_whatever_the_result() -> None:
    ours, theirs = socket.socketpair()
    with ours, theirs:
        hooks._close_unclaimed_socket(_failed_open(913, ours.fileno()))
        assert _is_open(ours.fileno())


def test_nothing_is_closed_after_a_success() -> None:
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        hooks._close_unclaimed_socket(_failed_open(0, descriptor))
        assert _is_open(descriptor)
    finally:
        os.close(descriptor)
