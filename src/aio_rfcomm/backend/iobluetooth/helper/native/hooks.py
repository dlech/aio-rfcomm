# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Hooks into CoreBluetooth and IOBluetooth for what they would otherwise
throw away.
"""

import ctypes
import logging
import os
import socket
import stat
from typing import NamedTuple, cast

from rubicon.objc import (
    ObjCInstance,
    ns_from_py,
)

from aio_rfcomm.backend.iobluetooth.helper.native.bindings import (
    get_class,
    normalize,
)
from aio_rfcomm.backend.iobluetooth.helper.native.channel import unread_by_channel

logger = logging.getLogger(__name__)

# The runtime calls the hook in hear_open_results needs, declared on a handle
# of its own so as not to disturb rubicon's declarations of the same functions.
_objc = ctypes.CDLL("/usr/lib/libobjc.dylib")
_objc.sel_registerName.restype = ctypes.c_void_p
_objc.sel_registerName.argtypes = [ctypes.c_char_p]
_objc.class_getInstanceMethod.restype = ctypes.c_void_p
_objc.class_getInstanceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_objc.method_getImplementation.restype = ctypes.c_void_p
_objc.method_getImplementation.argtypes = [ctypes.c_void_p]
_objc.method_setImplementation.restype = ctypes.c_void_p
_objc.method_setImplementation.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


# bluetoothd's own result for each channel open, before CoreBluetooth reduces
# it to an error code, by device address and channel. Written on a framework
# thread by the hook hear_open_results installs.
open_results: dict[tuple[str, int], int] = {}


_HandleOpened = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)

# The hook runs on a framework thread, where rubicon is not safe to use: it
# loads a class's methods on first use, and two threads doing so at once raise
# -- measured on macOS 26.6.2, as a helper that failed to make a channel's
# delegate while the hook read an incoming channel's result. So the hook reads
# with plain message sends, and its keys are made here, on the main thread.
_send_with_object = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)(("objc_msgSend", _objc))
_send_for_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc)
)
_send_for_int = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc)
)
_send_for_text = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(
    ("objc_msgSend", _objc)
)
_send_is_kind = ctypes.CFUNCTYPE(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)(("objc_msgSend", _objc))
_objc.objc_getClass.restype = ctypes.c_void_p
_objc.objc_getClass.argtypes = [ctypes.c_char_p]
_IS_KIND_OF_CLASS = _objc.sel_registerName(b"isKindOfClass:")
_NSNUMBER = _objc.objc_getClass(b"NSNumber")
_NSSTRING = _objc.objc_getClass(b"NSString")
_OBJECT_FOR_KEY = _objc.sel_registerName(b"objectForKey:")
_INT_VALUE = _objc.sel_registerName(b"intValue")
_ADDRESS_STRING = _objc.sel_registerName(b"addressString")
_UTF8_STRING = _objc.sel_registerName(b"UTF8String")
_RESULT_KEY = cast(ObjCInstance, ns_from_py("kCBMsgArgResult"))
_CHANNEL_KEY = cast(ObjCInstance, ns_from_py("kCBMsgArgRFCOMMChannelID"))
_SOCKET_KEY = cast(ObjCInstance, ns_from_py("kCBMsgArgSocket"))


class _OpenResult(NamedTuple):
    """
    What bluetoothd said about one channel open.
    """

    address: str | None
    channel: int | None
    result: int | None
    socket: int | None


def _read_open_result(peer: int, info: int) -> _OpenResult:
    """
    Read bluetoothd's report of a channel open, on whatever thread.

    Args:
        peer: The ``CBClassicPeer`` the channel was opened to.
        info: What bluetoothd sent.

    Returns:
        What it said, with ``None`` for anything it left out.
    """

    # Checked before anything is sent to them: an Objective-C exception on
    # this thread is beyond any Python handler and would end the process.
    def number(key: ObjCInstance) -> int | None:
        found = _send_with_object(info, _OBJECT_FOR_KEY, key.ptr)
        if found and _send_is_kind(found, _IS_KIND_OF_CLASS, _NSNUMBER):
            return int(_send_for_int(found, _INT_VALUE))

        return None

    address = None
    text = _send_for_object(peer, _ADDRESS_STRING)

    if text and _send_is_kind(text, _IS_KIND_OF_CLASS, _NSSTRING):
        utf8 = _send_for_text(text, _UTF8_STRING)
        address = utf8.decode() if utf8 else None

    return _OpenResult(
        address, number(_CHANNEL_KEY), number(_RESULT_KEY), number(_SOCKET_KEY)
    )


def record_open_result(address: str, channel: int, result: int) -> None:
    """
    Record bluetoothd's result for one channel open.

    Args:
        address: The device's address, in any of the forms IOBluetooth writes.
        channel: The RFCOMM channel number.
        result: The daemon's ``kCBMsgArgResult``, 0 for success.
    """
    open_results[(normalize(address), channel)] = result


def _note_open_result(opened: _OpenResult) -> None:
    """
    Note one channel open's result as bluetoothd reported it.

    Args:
        opened: What bluetoothd said.
    """
    if opened.address is None or opened.channel is None or opened.result is None:
        return
    record_open_result(opened.address, opened.channel, opened.result)


def describe_result(result: int) -> str:
    """
    Say what CoreBluetooth makes of one of bluetoothd's results.

    Args:
        result: A ``kCBMsgArgResult`` other than 0.

    Returns:
        CoreBluetooth's own description, which for most results on macOS
        26.6.2 is only "Unknown error."
    """
    # A private category, so a macOS without it costs only the description.
    try:
        error = get_class("NSError").errorWithInfo_(
            ns_from_py({"kCBMsgArgResult": result})
        )
    except Exception:  # noqa: BLE001
        return "no description"

    if error is None:
        return "no error"

    return str(error.localizedDescription)


def _is_null_device(descriptor: int) -> bool:
    try:
        opened = os.fstat(descriptor)
    except OSError:
        return False

    return (
        stat.S_ISCHR(opened.st_mode) and opened.st_rdev == os.stat(os.devnull).st_rdev
    )


def _close_unclaimed_socket(opened: _OpenResult) -> None:
    """
    Close the descriptor a failed channel open leaves behind.

    Args:
        opened: What bluetoothd said, once CoreBluetooth has handled it.
    """
    if not opened.result or opened.socket is None:
        return

    # CoreBluetooth duplicates each descriptor bluetoothd sends, and only the
    # channel object made for a success closes it. A failure carries
    # bluetoothd's descriptor 0, /dev/null: measured on macOS 26.6.2, one
    # leaked per refused open. Only /dev/null is closed, so a real socket never
    # is.
    if _is_null_device(opened.socket):
        os.close(opened.socket)


# The hook and what it replaced, kept for the life of the process: the
# framework calls through them on threads of its own.
_hooked: list[object] = []

# The same for the hook keep_unread_bytes installs.
_keeping: list[object] = []


def hear_open_results() -> None:
    """
    Keep bluetoothd's result for each channel open, which nothing else sees.

    On macOS 26.6.2, ``-[CBClassicPeer handleRFCOMMChannelOpened:]`` turns
    the daemon's result into an ``NSError`` and passes IOBluetooth only its
    code, which is 0 for any result CoreBluetooth has no name for -- a refused
    channel among them (follow-ups §13.6). So the method is wrapped, in this
    process only, to note the result first and then do exactly what it did.

    Does nothing if this macOS has no such method, and nothing the second time.
    """
    if _hooked:
        return

    try:
        selector = _objc.sel_registerName(b"handleRFCOMMChannelOpened:")
        method = _objc.class_getInstanceMethod(get_class("CBClassicPeer").ptr, selector)
    except Exception:  # noqa: BLE001
        method = None
    if not method:
        logger.debug("CoreBluetooth has no handleRFCOMMChannelOpened: to hear")
        return

    original = _HandleOpened(_objc.method_getImplementation(method))

    def opened(peer: int, selector: int, info: int) -> None:
        said = None

        try:
            said = _read_open_result(peer, info)
            _note_open_result(said)
        except Exception:
            # Losing the note costs a clearer error, never the open itself.
            logger.debug("could not read a channel open's result", exc_info=True)

        original(peer, selector, info)

        if said is not None:
            try:
                _close_unclaimed_socket(said)
            except Exception:
                logger.debug("could not check a failed open's socket", exc_info=True)

    replacement = _HandleOpened(opened)
    _hooked.extend([original, replacement])
    _objc.method_setImplementation(method, ctypes.cast(replacement, ctypes.c_void_p))


_Removal = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
# How much of a closing channel's stream is read at a time. Only a buffer size.
_UNREAD_PIECE_BYTES = 4096
_RFCOMMCHANNEL = _objc.sel_registerName(b"rfcommchannel")
_INPUT_STREAM = _objc.sel_registerName(b"inputStream")
_PROPERTY_FOR_KEY = _objc.sel_registerName(b"propertyForKey:")
_DELEGATE = _objc.sel_registerName(b"delegate")
_NSDATA = _objc.objc_getClass(b"NSData")
_core_foundation = ctypes.CDLL(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_core_foundation.CFDataGetLength.restype = ctypes.c_long
_core_foundation.CFDataGetLength.argtypes = [ctypes.c_void_p]
_core_foundation.CFDataGetBytePtr.restype = ctypes.c_void_p
_core_foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
_SOCKET_HANDLE = ctypes.c_void_p.in_dll(
    _core_foundation, "kCFStreamPropertySocketNativeHandle"
).value


def _socket_of(stream: int) -> int | None:
    """
    Find the socket under a channel's input stream.

    Runs on a framework thread, so it uses plain message sends only.

    Args:
        stream: The address of the stream.

    Returns:
        The socket's descriptor, or ``None`` if the stream does not say.
    """
    handle = _send_with_object(stream, _PROPERTY_FOR_KEY, _SOCKET_HANDLE)

    if not handle or not _send_is_kind(handle, _IS_KIND_OF_CLASS, _NSDATA):
        return None

    if _core_foundation.CFDataGetLength(handle) != ctypes.sizeof(ctypes.c_int):
        return None

    return ctypes.c_int.from_address(_core_foundation.CFDataGetBytePtr(handle)).value


def _read_unread(channel: int) -> bytes:
    """
    Read what is left in a channel's input stream.

    Runs on a framework thread, so it uses plain message sends only.

    Args:
        channel: The address of the IOBluetoothRFCOMMChannel.

    Returns:
        The bytes, if any.
    """
    rfcomm = _send_for_object(channel, _RFCOMMCHANNEL)
    stream = _send_for_object(rfcomm, _INPUT_STREAM) if rfcomm else None

    if not stream:
        return b""

    descriptor = _socket_of(stream)

    if descriptor is None:
        return b""

    # Straight from the socket, without waiting, and never through the stream.
    # The stream's read can wait on a socket that has already ended: measured
    # on macOS 26.6.2 against a Pybricks EV3 on firmware 4.1.0b6, it waited on
    # one that poll() called hung up and empty, holding up the framework queue
    # this runs on, and every open after it failed until the helper exited.
    #
    # The main run loop may be reading the same socket through the stream. What
    # it has read it has already handed to the delegate, and the close reaches
    # the main thread only after this runs.
    pieces: list[bytes] = []

    with socket.socket(fileno=os.dup(descriptor)) as reader:
        while True:
            try:
                piece = reader.recv(_UNREAD_PIECE_BYTES, socket.MSG_DONTWAIT)
            except BlockingIOError:
                break

            if not piece:
                break

            pieces.append(piece)

    return b"".join(pieces)


def keep_unread_bytes() -> None:
    """
    Keep the bytes a channel received just before the device hung up.

    Wraps ``-[IOBluetoothRFCOMMChannel removeRFCOMMChannelforDevice]``, in
    this process only, to read the channel's socket dry before its input
    stream is closed. The delegate's ``rfcommChannelClosed:`` hands the bytes
    over.

    Does nothing if this macOS has no such method, and nothing the second time.
    """
    # When the device hangs up, the removal closes the input stream on a
    # framework thread without reading what is left, while the stream's own
    # reads happen on the main run loop. So bytes that arrive just before the
    # hang-up are thrown away. Read in the disassembly of IOBluetooth on macOS
    # 26.6.2 (25G83). Measured on the same macOS against a BlueZ 5.85 server
    # that sends and hangs up at once, with the helper's main thread held up
    # for 0.3 s as the channel opened: 12 of 12 sends lost without this hook,
    # none with it. The removal sends rfcommChannelClosed: to the main thread
    # after it has run, so the bytes are in place by then.
    if _keeping:
        return

    try:
        selector = _objc.sel_registerName(b"removeRFCOMMChannelforDevice")
        method = _objc.class_getInstanceMethod(
            get_class("IOBluetoothRFCOMMChannel").ptr, selector
        )
    except Exception:  # noqa: BLE001
        method = None
    if not method:
        logger.debug("IOBluetooth has no removeRFCOMMChannelforDevice to wrap")
        return

    original = _Removal(_objc.method_getImplementation(method))

    def removing(channel: int, selector: int) -> None:
        try:
            # A channel nobody has taken yet has nobody to hand bytes to.
            if _send_for_object(channel, _DELEGATE):
                unread = _read_unread(channel)

                if unread:
                    unread_by_channel[channel] = unread
        except Exception:
            # Losing these bytes is what happened without the hook.
            logger.debug("could not read a closing channel's stream", exc_info=True)

        original(channel, selector)

    replacement = _Removal(removing)
    _keeping.extend([original, replacement])
    _objc.method_setImplementation(method, ctypes.cast(replacement, ctypes.c_void_p))
