# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
An RFCOMM channel, and the delegate IOBluetooth reports its events to.

A delegate is held weakly, so the channel object holds its own.
"""

# Do not add ``from __future__ import annotations`` to this module. rubicon
# reads the annotations on the delegate methods below at runtime to build
# each one's Objective-C type encoding, and under PEP 563 an annotation is
# stored as its own source text rather than the object, so a callback would
# be given the wrong signature and read rubbish off the stack.

import asyncio
import ctypes
import logging
from typing import cast

from rubicon.objc import NSObject, ObjCInstance, objc_method
from rubicon.objc.runtime import autoreleasepool

from aio_rfcomm.backend.iobluetooth.helper.native.bindings import (
    IOBluetoothRFCOMMChannelDelegate,
    MachError,
    NativeChannel,
    NativeDevice,
    check,
    normalize,
)
from aio_rfcomm.discovery import RfcommDeviceInfo

logger = logging.getLogger(__name__)

# A bound on waiting for the framework to finish closing a channel. This is
# the one place a limit belongs: it sits on the teardown path, and teardown
# that can block forever leaves a canceled caller unable to unwind at all.
# Running out is not an error, so it gives up by carrying on.
_CLOSE_TIMEOUT = 5.0

# A write's refcon is the address of the object holding its future, and that
# object has to stay alive until the completion callback reads it back out --
# including when whoever asked for the write was canceled long ago and the
# frame that held it is gone. Keyed by the address the callback is handed.
_in_flight: dict[int, "ctypes.py_object[asyncio.Future[int]]"] = {}


# Bytes read out of a closing channel's stream, by the address of its
# IOBluetoothRFCOMMChannel. Written on a framework thread and taken on the main
# thread, in the delegate's rfcommChannelClosed:.
unread_by_channel: dict[int, bytes] = {}


def _address_of(channel: object) -> int:
    """
    Get the address an IOBluetoothRFCOMMChannel is known by in ``unread_by_channel``.

    Args:
        channel: The channel, or ``None`` where the framework gave none.

    Returns:
        Its address, or 0 for anything that has none.
    """
    pointer = getattr(channel, "ptr", None)
    return cast(int, getattr(pointer, "value", None) or 0)


class ChannelDelegate(NSObject, protocols=[IOBluetoothRFCOMMChannelDelegate]):
    """
    Everything one open RFCOMM channel reports back.
    """

    opened: asyncio.Future[None]
    arrived: asyncio.Queue[bytes]
    ended: asyncio.Event

    @objc_method
    def init(self):
        self.opened = asyncio.get_running_loop().create_future()
        self.arrived = asyncio.Queue()
        self.ended = asyncio.Event()
        return self

    @objc_method
    def rfcommChannelOpenComplete_status_(
        self, _channel: ObjCInstance, status: int
    ) -> None:
        if self.opened.done():
            return
        if status:
            self.opened.set_exception(MachError(status))
        else:
            self.opened.set_result(None)

    @objc_method
    def rfcommChannelData_data_length_(
        self,
        _channel: ObjCInstance,
        _data: ctypes.c_void_p,
        _length: ctypes.c_size_t,
    ) -> None:
        # The bytes belong to IOBluetooth and are only valid for the duration
        # of this callback, so they are copied out rather than referenced.
        #
        # The casts below are for the type checker alone. These parameters are
        # annotated with ctypes types because rubicon reads the annotations to
        # build the selector's signature, but what it hands the callback are
        # plain integers.
        self.arrived.put_nowait(ctypes.string_at(_data, cast(int, _length)))

    @objc_method
    def rfcommChannelClosed_(self, channel: ObjCInstance) -> None:
        # What IOBluetooth was about to throw away. See keep_unread_bytes.
        unread = unread_by_channel.pop(_address_of(channel), None)

        if unread:
            self.arrived.put_nowait(unread)

        self.ended.set()

    @objc_method
    def rfcommChannelWriteComplete_refcon_status_bytesWritten_(
        self,
        _channel: ObjCInstance,
        _refcon: ctypes.c_void_p,
        status: int,
        _length: ctypes.c_size_t,
    ) -> None:
        refcon = cast(int, _refcon)
        holder = _in_flight.pop(refcon, None)
        if holder is None:
            # A write from a channel that has already been let go of. There
            # is nothing left to tell.
            return
        future = holder.value
        if future.done():
            return
        if status:
            future.set_exception(MachError(status))
        else:
            future.set_result(cast(int, _length))


class Channel:
    """
    An RFCOMM channel, from the moment the framework hands one over.

    Reading is a queue rather than a method returning bytes, because
    IOBluetooth pushes: data arrives in a delegate callback whether or not
    anybody has asked for it, and there is no synchronous receive to call.

    This object is the only thing that holds the framework's channel. That is
    a requirement rather than a convention -- see ``close``.
    """

    def __init__(
        self, channel: NativeChannel, delegate: ChannelDelegate, device: NativeDevice
    ) -> None:
        """
        Args:
            channel: The ``IOBluetoothRFCOMMChannel``, which may not be open
                yet.
            delegate: Its delegate, held here because the channel does not.
            device: The device it runs over.
        """
        self._channel = channel
        self._delegate = delegate
        self._device = device

    @property
    def arrived(self) -> asyncio.Queue[bytes]:
        """
        Bytes that have come in and not yet been taken.
        """
        return self._delegate.arrived

    def peer(self) -> RfcommDeviceInfo:
        """
        Returns:
            The device at the other end.
        """
        name = self._device.name
        return RfcommDeviceInfo(
            normalize(str(self._device.addressString)), str(name) if name else None
        )

    async def wait_until_open(self, seconds: float) -> bool:
        """
        Wait a while for the framework to finish opening the channel.

        Waiting again after running out of time is fine: the open is still
        heard, because only this wait ends, not the open's future.

        Args:
            seconds: How long to wait this time.

        Returns:
            Whether it opened in that time.

        Raises:
            MachError: The channel could not be opened.
        """
        opened = self._delegate.opened
        await asyncio.wait([opened], timeout=seconds)
        if not opened.done():
            return False

        opened.result()
        return True

    def connected_number(self) -> int | None:
        """
        Find which channel this one is actually connected to.

        Not always the one asked for. On macOS 26.6.2 a device's peer holds a
        single callback for opens, and each new open replaces it. So an
        earlier open that lands late is reported to the newest channel, which
        takes it on as its own, while ``getChannelID`` goes on giving the
        number that channel was asked for. Only the CoreBluetooth channel
        underneath says which it really is.

        Returns:
            The channel number bluetoothd connected, or ``None`` if the
            framework holds no connection for this channel.
        """
        connection = self._channel.rfcommchannel
        if connection is None:
            return None
        return int(connection.channelID)

    async def send(self, data: bytes) -> None:
        """
        Write all of ``data`` to the channel.

        The framework rejects a write longer than the channel's maximum
        transmission unit, so this splits one. The size is read each time
        rather than kept, since it belongs to the channel and not to us.

        Args:
            data: The bytes to write.

        Raises:
            MachError: The write failed.
        """
        limit = int(self._channel.getMTU())
        for start in range(0, len(data), limit):
            await self._write(data[start : start + limit])

    async def _write(self, piece: bytes) -> None:
        """
        Write one piece, no longer than the channel's maximum unit.

        Args:
            piece: The bytes to write.

        Raises:
            MachError: The write failed.
        """
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        holder = ctypes.py_object(future)
        refcon = ctypes.addressof(holder)
        # Held until the completion callback takes it back out. Letting it go
        # when this task is canceled would leave the callback reading an
        # address whose memory has been freed, which crashes the process some
        # unrelated moment later.
        _in_flight[refcon] = holder
        try:
            check(self._channel.writeAsync(piece, length=len(piece), refcon=refcon))
        except BaseException:
            del _in_flight[refcon]
            raise
        await future

    async def wait_until_ended(self) -> None:
        """
        Wait for the far end to close the channel, or the link under it to go.
        """
        await self._delegate.ended.wait()

    def release(self) -> None:
        """
        Let go of a channel without closing it.

        For one whose open was never reported: it may in fact be open, in
        which case ``closeChannel`` would close the channel the next attempt
        means to pick up, and the caller would be left writing into a channel
        the peer has already seen go away.

        Calling this twice is harmless. The channel is unusable afterwards.
        """
        if not hasattr(self, "_channel"):
            return

        # A report that comes after all is then ignored, rather than left
        # with nobody to read it.
        self._delegate.opened.cancel()
        unread_by_channel.pop(_address_of(self._channel), None)
        # Dropped inside a pool for the reason close() explains at length.
        with autoreleasepool():
            del self._channel
            del self._delegate

    async def close(self) -> None:
        """
        Close the channel and let the framework have it back.

        Two things have to happen and the order matters. ``closeChannel``
        only starts the teardown, so this waits for the framework to say it
        has finished -- with a bound, since teardown that can block forever
        leaves a canceled caller unable to unwind, and giving up here means
        carrying on rather than raising.

        Then the channel is let go of, which is not tidiness. rubicon keeps a
        reference for as long as its Python wrapper lives and gives that
        reference back by autoreleasing it, and an autorelease with no pool
        in place is never actually carried out. The channel would stay alive,
        stay open, and be handed back the next time one is asked for on this
        number, complete with the previous caller's stream, on macOS 26.6.2.
        Dropping the wrapper inside a pool is what makes the release happen,
        and this object being the only thing holding the channel is what makes
        dropping it enough.

        Calling this twice is harmless. The channel is unusable afterwards.
        """
        if not hasattr(self, "_channel"):
            return

        self._delegate.opened.cancel()
        self._channel.closeChannel()
        try:
            if not self._delegate.ended.is_set():
                async with asyncio.timeout(_CLOSE_TIMEOUT):
                    await self._delegate.ended.wait()
        except TimeoutError:
            logger.warning("RFCOMM channel did not finish closing")
        finally:
            unread_by_channel.pop(_address_of(self._channel), None)
            with autoreleasepool():
                # Deleted rather than set aside, because this object is
                # finished: anything that reaches for the channel afterwards
                # is a mistake and should say so.
                del self._channel
                del self._delegate
