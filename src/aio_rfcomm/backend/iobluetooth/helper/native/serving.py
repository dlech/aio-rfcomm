# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Publishing a service, and hearing the channels peers open to it.
"""

# Do not add ``from __future__ import annotations`` to this module. rubicon
# reads the annotations on the Objective-C methods below at runtime to build
# each one's Objective-C type encoding, and under PEP 563 an annotation is
# stored as its own source text rather than the object, so a callback would
# be given the wrong signature and read rubbish off the stack.

import asyncio
import ctypes
import logging
from collections.abc import AsyncGenerator, Collection, Iterable
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast
from uuid import UUID

from rubicon.objc import (
    SEL,
    NSObject,
    ObjCInstance,
    ns_from_py,
    objc_method,
)
from rubicon.objc.runtime import autoreleasepool

from aio_rfcomm.backend.iobluetooth.helper.native import bindings
from aio_rfcomm.backend.iobluetooth.helper.native.bindings import (
    DEVICE_MANAGER,
    NativeChannel,
    NativeDevice,
    NativeRecord,
    find_class,
    sdp_uuid,
)
from aio_rfcomm.backend.iobluetooth.helper.native.channel import (
    Channel,
    ChannelDelegate,
)
from aio_rfcomm.backend.iobluetooth.helper.native.manager import (
    wait_until_ready,
)
from aio_rfcomm.errors import (
    ChannelInUseError,
    ConnectionFailedError,
)

logger = logging.getLogger(__name__)


class _LocalServices(Protocol):
    def allValues(self) -> Iterable[NativeRecord]: ...


class _DeviceManager(Protocol):
    def getLocalServices(self) -> _LocalServices | None: ...


class _DeviceManagerClass(Protocol):
    def sharedDeviceManager(self) -> _DeviceManager: ...


class _IncomingChannel(NativeChannel, Protocol):
    # Handed over open, with no delegate yet.
    def setDelegate(self, delegate: "ChannelDelegate", /) -> int: ...
    def getDevice(self) -> "NativeDevice": ...


# kIOBluetoothUserNotificationChannelDirectionIncoming.
# Measured on macOS 26.6.2: IOBluetooth never passes on a registration for
# kIOBluetoothUserNotificationChannelDirectionAny.
_INCOMING = 1

# bluetoothd never registers these two with its RFCOMM stack (disassembly,
# macOS 26.6.2), so a service on one is not like a service anywhere else. They
# are left alone.
_NOT_CHOSEN = (10, 15)

# The protocol UUIDs a record names to say it is reached over RFCOMM.
_L2CAP = 0x0100
_RFCOMM = 0x0003


def _record(service: UUID, name: str, number: int) -> ObjCInstance:
    """
    Build the service record to publish.

    Args:
        service: The service class.
        name: The service name.
        number: The RFCOMM channel number.

    Returns:
        The record, in the dictionary form IOBluetooth publishes.
    """
    # Sequences as plain arrays. Written as data element dictionaries, which
    # is what the framework's own documentation shows, the encoder on macOS
    # 26.6.2 writes out the dictionaries' key names in place of their contents
    # (follow-ups §10.1). Arrays it encodes correctly, measured.
    record: dict[str, Any] = {
        "0001": [sdp_uuid(service)],
        "0004": [
            [bindings.IOBluetoothSDPUUID.uuid16(_L2CAP)],
            [
                bindings.IOBluetoothSDPUUID.uuid16(_RFCOMM),
                {
                    "DataElementType": 1,
                    "DataElementSize": 1,
                    "DataElementValue": number,
                },
            ],
        ],
        "0100": name,
    }
    return cast(ObjCInstance, ns_from_py(cast(Any, record)))


def _channels_in_use() -> set[int]:
    """
    Read which RFCOMM channels services published on this Mac name, by any
    process.

    Returns:
        The channel numbers, empty if the private path to them has gone.
    """
    try:
        manager = cast(_DeviceManagerClass, find_class(DEVICE_MANAGER))
        services = manager.sharedDeviceManager().getLocalServices()
        records = list(services.allValues()) if services is not None else []
    except Exception:
        logger.debug("could not read the local services", exc_info=True)
        return set()

    used: set[int] = set()

    for record in records:
        number = ctypes.c_char()
        if not record.getRFCOMMChannelID(ctypes.byref(number)):
            used.add(number.value[0])

    return used


def _choose_channel(channel: int | None, avoiding: Collection[int]) -> int:
    """
    Settle which channel to serve on: one with no service record.

    Args:
        channel: The one asked for, or ``None`` to choose.
        avoiding: Channels this program is already serving on, from other
            processes.

    Returns:
        The channel number.

    Raises:
        ChannelInUseError: The one asked for has a record, or every one does.
    """
    # A channel that has had a record since Bluetooth was last switched on is
    # never served on again, because it may not work and nothing would say
    # so. Measured on macOS 26.6.2, with the disassembly of bluetoothd: a
    # record can never be withdrawn (each removal is answered
    # OI_STATUS_INVALID_HANDLE, for the handle it gave out), and every
    # connection to a channel, accepted ones included, is kept in a list never
    # cleared. The next registration on the channel, in a later process, takes
    # one off the list and accepts it instead of listening. The accept fails,
    # the peer being long gone, and the service never hears a peer at all.
    used = _channels_in_use() | set(avoiding)
    if channel is not None:
        if channel in used:
            raise ChannelInUseError(
                f"RFCOMM channel {channel} already has a service published on "
                "this Mac, by this program or another. macOS keeps a channel "
                "it has served on, and may not deliver connections on it "
                "again, until Bluetooth has been switched off and on. Do that, "
                "or let the library choose a channel."
            )
        return channel

    # From the top, which this Mac's own services leave alone.
    for number in range(30, 0, -1):
        if number not in used and number not in _NOT_CHOSEN:
            return number
    raise ChannelInUseError(
        "every RFCOMM channel already has a service published on this Mac. "
        "macOS frees them only when Bluetooth is switched off and on."
    )


class _OpenListener(NSObject):
    """
    Hears each channel a peer opens to a published service.
    """

    arrived: asyncio.Queue[ObjCInstance]

    @objc_method
    def init(self):
        self.arrived = asyncio.Queue()
        return self

    # Called on the main queue, so on the loop's own thread.
    @objc_method
    def channelOpened_channel_(
        self, _notification: ObjCInstance, channel: ObjCInstance
    ) -> None:
        self.arrived.put_nowait(channel)


class Service:
    """
    A published service, handing over the channels peers open to it.
    """

    def __init__(self, number: int, listener: _OpenListener) -> None:
        """
        Args:
            number: The RFCOMM channel it is on.
            listener: Hears each channel opened to it.
        """
        self.channel = number
        self._listener = listener

    async def accept(self) -> Channel:
        """
        Wait for a peer to open a channel.

        Returns:
            The channel, already open.

        Raises:
            Exception: The channel could not be taken over. It is closed, and
                the next one is unaffected.
        """
        opened = cast(_IncomingChannel, await self._listener.arrived.get())

        try:
            delegate = ChannelDelegate.alloc().init()
            opened.setDelegate(delegate)
            if not opened.isOpen():
                # Closed while it waited, before it had a delegate to say so.
                delegate.ended.set()

            return Channel(opened, delegate, opened.getDevice())
        except Exception:
            with autoreleasepool():
                opened.closeChannel()
                del opened
            raise


@asynccontextmanager
async def serve(
    service: UUID,
    *,
    name: str,
    channel: int | None = None,
    avoiding: Collection[int] = (),
) -> AsyncGenerator[Service, None]:
    """
    Publish a service and hear the channels peers open to it. Call once per
    process.

    Args:
        service: The service class.
        name: The service name.
        channel: The RFCOMM channel, or ``None`` to choose one.
        avoiding: Channels this program is already serving on, from other
            processes.

    Yields:
        The service, withdrawn on exit. Channels already handed over carry on.

    Raises:
        BluetoothUnavailableError: Bluetooth cannot be used.
        ChannelInUseError: The channel is taken.
        ConnectionFailedError: The service record could not be published.
    """
    # Once per process: bluetoothd keeps one registration per process, each
    # replacing the last, and frees only the last when the process ends.
    # Every one before it holds a slot in its RFCOMM stack until Bluetooth is
    # next switched off, and once the slots run out nothing on this Mac can
    # serve -- measured on macOS 26.6.2, each registration reported as
    # OI_STATUS_NO_RESOURCES and every peer's connection left waiting.
    # Unregistering never reaches bluetoothd.
    #
    # Made before this, IOBluetooth on macOS 26.6.2 drops the registration
    # unsent.
    await wait_until_ready()
    number = _choose_channel(channel, avoiding)

    listener = _OpenListener.alloc().init()
    # A selector, not a string: given one, rubicon stores a selector made of
    # its first letter, the listener does not respond to it, and IOBluetooth
    # silently lets every channel go -- measured on macOS 26.6.2.
    notification = (
        bindings.IOBluetoothRFCOMMChannel.registerForChannelOpenNotifications(
            listener,
            selector=SEL("channelOpened:channel:"),
            withChannelID=number,
            direction=_INCOMING,
        )
    )

    try:
        record = (
            bindings.IOBluetoothSDPServiceRecord.publishedServiceRecordWithDictionary(
                _record(service, name, number)
            )
        )
        if record is None:
            raise ConnectionFailedError(
                f"could not publish {service} on RFCOMM channel {number}"
            )

        try:
            yield Service(number, listener)
        finally:
            # Refused by macOS 26.6.2 (see _choose_channel), but asked, in
            # case another is not.
            record.removeServiceRecord()
    finally:
        notification.unregister()
        # Channels nobody took are let go of, which closes them.
        with autoreleasepool():
            while not listener.arrived.empty():
                cast(NativeChannel, listener.arrived.get_nowait()).closeChannel()
