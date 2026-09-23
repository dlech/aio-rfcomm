# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The IOBluetooth layer, as thin a wrapper as the framework allows.

Everything here must run on the main thread of a process whose event loop is
backed by a CFRunLoop. That is not a style preference: an asynchronous
IOBluetooth call made from anywhere else returns success and then never
completes, and ``openRFCOMMChannelSync`` is refused outright. The helper
process exists to provide such a thread; nothing in this module arranges one.

This module knows nothing about the helper or its control protocol, so the
same code serves an in-process caller that already owns a CFRunLoop main
thread.
"""

# Do not add ``from __future__ import annotations`` to this module. rubicon
# reads the annotations on the delegate methods below at runtime to build
# each one's Objective-C type encoding, and under PEP 563 an annotation is
# stored as its own source text rather than the object, so a callback would
# be given the wrong signature and read rubbish off the stack.

import asyncio
import ctypes
import logging
from collections.abc import AsyncGenerator, Collection, Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

from rubicon.objc import (
    NSObject,
    ObjCClass,
    ObjCInstance,
    ObjCProtocol,
    objc_method,
)
from rubicon.objc.runtime import libc, libobjc, load_library, objc_id
from typing_extensions import override

from aio_rfcomm.discovery import RfcommAdapterInfo, RfcommDeviceInfo
from aio_rfcomm.errors import (
    CloseReason,
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
)

__all__ = [
    "Channel",
    "MachError",
    "list_adapters",
    "list_known_devices",
    "open_channel",
    "read_authorization",
    "read_power_state",
]

# Loading the framework is what makes its classes findable by name. It does
# not touch the radio, so it does not trigger the permission check that ends
# the process; the first call that needs Bluetooth does.
load_library("IOBluetooth")

# CoreBluetooth is loaded only for CBManager.authorization, which is the one
# way to tell "the user has not allowed Bluetooth" from "the radio is off".
# Reading it does not prompt and does not touch the radio.
load_library("CoreBluetooth")


# --------------------------------------------------------------------------
# Typed views of the framework
#
# rubicon looks methods up at runtime, so a class it hands back has nothing a
# type checker can see. Declaring the handful we call keeps the call sites
# checked rather than littered with ignores, and doubles as the list of what
# this module actually depends on. Objective-C spellings are kept exactly, so
# they can be looked up in Apple's documentation.
# --------------------------------------------------------------------------


class _UUID(Protocol):
    """
    An ``IOBluetoothSDPUUID``.

    Nothing is ever called on one: it is built from a Python UUID and handed
    straight back to the framework. Naming the type says that much at the
    signatures it appears in, which ``Any`` does not -- though, having no
    members, it cannot check that what arrives really is one.
    """


class _Record(Protocol):
    def getRFCOMMChannelID(self, out: Any, /) -> int: ...


class _Channel(Protocol):
    def isOpen(self) -> bool: ...
    def getMTU(self) -> int: ...
    def closeChannel(self) -> int: ...
    def writeAsync(self, data: bytes, /, *, length: int, refcon: int) -> int: ...


class _Device(Protocol):
    @property
    def addressString(self) -> Any: ...
    @property
    def name(self) -> Any: ...
    def isConnected(self) -> bool: ...
    def openConnection(self, target: "_ConnectionComplete", /) -> int: ...
    def closeConnection(self) -> int: ...
    def performSDPQuery(self, target: "_QueryComplete", /) -> int: ...
    def getServiceRecordForUUID(self, uuid: _UUID, /) -> _Record | None: ...
    def openRFCOMMChannelAsync(
        self, channel: Any, /, *, withChannelID: int, delegate: "_ChannelDelegate"
    ) -> int: ...


class _Controller(Protocol):
    @property
    def powerState(self) -> int: ...
    def addressAsString(self) -> Any: ...
    def nameAsString(self) -> Any: ...


class _DeviceClass(Protocol):
    def deviceWithAddressString(self, address: str, /) -> _Device | None: ...
    def pairedDevices(self) -> Iterable[_Device] | None: ...


class _ControllerClass(Protocol):
    def defaultController(self) -> _Controller | None: ...


class _ChannelClass(Protocol):
    # Unlike the others, this class is reached by wrapping a pointer the
    # framework filled in rather than by calling a method on it, so what
    # there is to describe is the call itself.
    def __call__(self, pointer: objc_id, /) -> _Channel: ...


class _UUIDClass(Protocol):
    def uuidWithData(self, data: bytes, /) -> _UUID: ...


def _class(name: str) -> Any:
    """
    Look up an Objective-C class by name.

    Args:
        name: The class's name.

    Returns:
        The class. Missing classes cannot happen once the framework is
        loaded, and would be a mistake in this module rather than anything a
        caller could act on.
    """
    found = ObjCClass(name)
    assert found is not None, f"IOBluetooth has no class {name}"
    return found


IOBluetoothDevice = cast(_DeviceClass, _class("IOBluetoothDevice"))
IOBluetoothHostController = cast(_ControllerClass, _class("IOBluetoothHostController"))
IOBluetoothSDPUUID = cast(_UUIDClass, _class("IOBluetoothSDPUUID"))
IOBluetoothRFCOMMChannel = cast(_ChannelClass, _class("IOBluetoothRFCOMMChannel"))
IOBluetoothRFCOMMChannelDelegate = ObjCProtocol("IOBluetoothRFCOMMChannelDelegate")

if TYPE_CHECKING:

    def _mach_error_string(code: int) -> bytes: ...

else:
    _mach_error_string = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_int)(
        ("mach_error_string", libc), ((1, "code"),)
    )

# IOBluetooth reports a page timeout as this bare HCI status rather than as an
# IOKit error, and the framework's description of it is unhelpful.
_HCI_PAGE_TIMEOUT = 10

# kBluetoothHCIPowerStateON. The framework's power states are C enum
# constants rather than exported symbols, so there is nothing to read out of
# the dylib at runtime and the value has to be written down.
_POWER_ON = 1

# A bound on waiting for the framework to finish closing a channel. This is
# the one place a limit belongs: it sits on the teardown path, and teardown
# that can block forever leaves a cancelled caller unable to unwind at all.
# Running out is not an error, so it gives up by carrying on.
_CLOSE_TIMEOUT = 5.0

# How many times to ask for a channel whose predecessor is still going away,
# and how long to leave between asks. Measured: a reconnect straight after a
# close needs exactly one wait, so this is twenty times what the framework
# actually takes. It is not a guess at how long an operation should be
# allowed -- that is the caller's to decide -- but the point at which a local
# quirk stops looking like settling and starts looking like a framework that
# is wedged, which is worth saying out loud rather than waiting on for ever.
_OPEN_ATTEMPTS = 20
_SETTLE_SECONDS = 0.1
# kIOReturnTimeout, which IOKit builds from a system (0x38, I/O Kit), a
# subsystem (0, common) and a code (0x2d6), giving 0xE00002D6. Statuses arrive
# as signed 32-bit integers, which is why this one shows up in tracebacks as a
# large negative number rather than as anything recognisable.
_IO_TIMEOUT = ctypes.c_int32(0xE00002D6).value

# A write's refcon is the address of the object holding its future, and that
# object has to stay alive until the completion callback reads it back out --
# including when whoever asked for the write was cancelled long ago and the
# frame that held it is gone. Keyed by the address the callback is handed.
_in_flight: dict[int, "ctypes.py_object[asyncio.Future[int]]"] = {}

logger = logging.getLogger(__name__)


class MachError(Exception):
    """
    A failure reported as a mach status code.

    The numbering is the operating system's own and spans its subsystems --
    the kernel, IOKit and the rest -- so a status is only meaningful once it
    is known which one it came from. That is also why one can arrive
    described in terms of something with no bearing on what was attempted.
    ``mach_error_string`` renders any of them.
    """

    def __init__(self, code: int) -> None:
        """
        Args:
            code: The status that was returned.
        """
        super().__init__(code, _mach_error_string(code).decode())

    @property
    def code(self) -> int:
        """
        The status that was returned.
        """
        return cast(int, self.args[0])

    @override
    def __str__(self) -> str:
        return f"[mach error {self.code}] {self.args[1]}"


def _check(code: int) -> None:
    """
    Turn a non-zero status into an exception.

    Args:
        code: The status to check.

    Raises:
        MachError: The status was non-zero.
    """
    if code:
        raise MachError(code)


# --------------------------------------------------------------------------
# Delegates
#
# IOBluetooth reports everything through delegate callbacks, and a delegate is
# held weakly, so each of these has to be kept alive by whoever installed it.
# --------------------------------------------------------------------------


class _ConnectionComplete(NSObject):
    """
    Completion for the ACL connection a channel is carried over.
    """

    future: asyncio.Future[None]

    # No return annotation: rubicon reads annotations to build the
    # Objective-C signature, and an initialiser's is already known.
    @objc_method
    def init(self):
        self.future = asyncio.get_running_loop().create_future()
        return self

    @objc_method
    def connectionComplete_status_(self, _device: ObjCInstance, status: int) -> None:
        if self.future.done():
            return
        if status:
            # A device that is switched off reports a bare 10 rather than an
            # IOKit status, which renders as a meaningless message. The I/O
            # timeout it really is says something a caller can act on.
            raise_as = _IO_TIMEOUT if status == _HCI_PAGE_TIMEOUT else status
            self.future.set_exception(MachError(raise_as))
        else:
            self.future.set_result(None)


class _QueryComplete(NSObject):
    """
    Completion for an SDP query.
    """

    future: asyncio.Future[None]

    @objc_method
    def init(self):
        self.future = asyncio.get_running_loop().create_future()
        return self

    @objc_method
    def sdpQueryComplete_status_(self, _device: ObjCInstance, status: int) -> None:
        if self.future.done():
            return
        if status:
            self.future.set_exception(MachError(status))
        else:
            self.future.set_result(None)


class _ChannelDelegate(NSObject, protocols=[IOBluetoothRFCOMMChannelDelegate]):
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
    def rfcommChannelClosed_(self, _channel: ObjCInstance) -> None:
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


# --------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------


class _ManagerClass(Protocol):
    """
    The one class property we want from ``CBManager``.
    """

    @property
    def authorization(self) -> int: ...


CBManager = cast(_ManagerClass, _class("CBManager"))


def read_authorization() -> int:
    """
    Read whether this process may use Bluetooth at all.

    macOS reports an unauthorised radio as switched off, so without this a
    program whose permission was never granted -- or was refused -- is told
    to turn Bluetooth on, which it already is. Reading this neither prompts
    nor touches the radio.

    Returns:
        A ``CBManagerAuthorization``. rubicon hands ``NSInteger`` back as a
        Python ``int`` already, so there is nothing to convert.
    """
    return CBManager.authorization


def read_power_state() -> bool:
    """
    Read whether the radio is switched on.

    Returns:
        True if the adapter is powered.
    """
    controller = IOBluetoothHostController.defaultController()
    return controller is not None and controller.powerState == _POWER_ON


def list_adapters() -> list[RfcommAdapterInfo]:
    """
    List the machine's Bluetooth adapters.

    Returns:
        The adapter, in a list of at most one: macOS exposes a single
        controller and gives no way to choose between radios.
    """
    controller = IOBluetoothHostController.defaultController()
    if controller is None:
        return []
    address = controller.addressAsString()
    return [
        RfcommAdapterInfo(
            "default",
            _normalise(str(address)) if address else None,
            str(controller.nameAsString() or "") or None,
        )
    ]


def list_known_devices(
    service: UUID | Collection[UUID] | None = None,
) -> list[RfcommDeviceInfo]:
    """
    List the devices macOS has paired with.

    Filtering happens against the cached SDP records, which is all that is
    available without connecting to each device in turn.

    Args:
        service: Return only devices offering this service, or any of these
            services. ``None`` returns every paired device.

    Returns:
        The matching devices.
    """
    wanted = [_sdp_uuid(u) for u in _each(service)]

    found: list[RfcommDeviceInfo] = []
    seen: set[str] = set()
    for device in IOBluetoothDevice.pairedDevices() or []:
        if wanted and not any(device.getServiceRecordForUUID(u) for u in wanted):
            continue
        address = _normalise(str(device.addressString))
        # macOS lists some devices twice. Measured rather than guessed at:
        # pairedDevices() returned thirteen entries holding twelve distinct
        # objects, with one device's object appearing at two non-adjacent
        # positions. The two entries are the same object, so there is nothing
        # to tell them apart and nothing to choose between them -- only a
        # caller left wondering which of two identical devices to use.
        if address in seen:
            continue
        seen.add(address)
        name = device.name
        found.append(RfcommDeviceInfo(address, str(name) if name else None))
    return found


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------


class Channel:
    """
    An RFCOMM channel, from the moment the framework hands one over.

    Reading is a queue rather than a method returning bytes, because
    IOBluetooth pushes: data arrives in a delegate callback whether or not
    anybody has asked for it, and there is no synchronous receive to call.

    This object is the only thing that holds the framework's channel. That is
    a requirement rather than a convention -- see :meth:`close`.
    """

    def __init__(
        self, channel: _Channel, delegate: _ChannelDelegate, device: _Device
    ) -> None:
        """
        Args:
            channel: The ``IOBluetoothRFCOMMChannel``, which may not be open
                yet.
            delegate: Its delegate, held here because the channel does not.
            device: The device it runs over, for telling a hang-up apart from
                a lost link.
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

    async def wait_until_open(self) -> None:
        """
        Wait for the framework to finish opening the channel.

        Raises:
            MachError: The channel could not be opened.
        """
        await self._delegate.opened

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
        # when this task is cancelled would leave the callback reading an
        # address whose memory has been freed, which crashes the process some
        # unrelated moment later.
        _in_flight[refcon] = holder
        try:
            _check(self._channel.writeAsync(piece, length=len(piece), refcon=refcon))
        except BaseException:
            del _in_flight[refcon]
            raise
        await future

    async def wait_until_ended(self) -> CloseReason:
        """
        Wait for the far end to close the channel.

        Returns:
            Why it closed. The baseband link tells the two cases apart: if it
            is still up, the peer closed just this channel; if it is gone, the
            whole link went away.
        """
        await self._delegate.ended.wait()
        if self._device.isConnected():
            return CloseReason.PEER_CLOSED
        return CloseReason.LINK_LOST

    async def close(self) -> None:
        """
        Close the channel and let the framework have it back.

        Two things have to happen and the order matters. ``closeChannel``
        only starts the teardown, so this waits for the framework to say it
        has finished -- with a bound, since teardown that can block forever
        leaves a cancelled caller unable to unwind, and giving up here means
        carrying on rather than raising.

        Then the channel is let go of, which is not tidiness. rubicon keeps a
        reference for as long as its Python wrapper lives and gives that
        reference back by autoreleasing it, and an autorelease with no pool
        in place is never actually carried out. The channel would stay alive,
        stay open, and be handed back the next time one is asked for on this
        number, complete with the previous caller's stream. Dropping the
        wrapper inside a pool is what makes the release happen, and this
        object being the only thing holding the channel is what makes
        dropping it enough.

        Calling this twice is harmless. The channel is unusable afterwards.
        """
        if not hasattr(self, "_channel"):
            return

        self._channel.closeChannel()
        try:
            if not self._delegate.ended.is_set():
                async with asyncio.timeout(_CLOSE_TIMEOUT):
                    await self._delegate.ended.wait()
        except TimeoutError:
            logger.warning("RFCOMM channel did not finish closing")
        finally:
            pool = libobjc.objc_autoreleasePoolPush()
            try:
                # Deleted rather than set aside, because this object is
                # finished: anything that reaches for the channel afterwards
                # is a mistake and should say so.
                del self._channel
                del self._delegate
            finally:
                libobjc.objc_autoreleasePoolPop(pool)


@asynccontextmanager
async def open_channel(
    address: str, *, service: UUID | None = None, channel: int | None = None
) -> AsyncGenerator[Channel, None]:
    """
    Open an RFCOMM channel to a device.

    Exactly one of ``service`` and ``channel`` says which channel to open. A
    service is looked up in the device's SDP records, querying the device if
    they are not already cached.

    Args:
        address: The device's Bluetooth address.
        service: The service to look up.
        channel: The channel number to open directly.

    Yields:
        The open channel, closed on exit.

    Raises:
        DeviceNotFoundError: The device did not answer.
        ServiceNotFoundError: The device does not offer that service.
        ConnectionFailedError: The channel could not be opened.
    """
    if (service is None) == (channel is None):
        raise TypeError("open_channel takes exactly one of service and channel")

    device = IOBluetoothDevice.deviceWithAddressString(address)
    if not device:
        raise DeviceNotFoundError(f"{address} is not a Bluetooth address")
    logger.debug("%s: connected=%s", address, device.isConnected())

    # Both paths need the baseband link: an SDP query runs over it, and so
    # does the channel itself.
    if not device.isConnected():
        await _connect(device, address)

    if channel is not None:
        number = channel
    else:
        # Guaranteed by the check above, which the type checker cannot see.
        number = await _look_up(device, address, cast(UUID, service))

    opening = await _request(device, number, address)
    try:
        await opening.wait_until_open()
    except MachError as error:
        await opening.close()
        raise ConnectionFailedError(
            f"could not open channel {number} on {address}: {error}"
        ) from error
    except BaseException:
        await opening.close()
        raise

    logger.debug("channel %d to %s is open", number, address)
    try:
        yield opening
    finally:
        await opening.close()


async def _request(device: _Device, number: int, address: str) -> Channel:
    """
    Ask the framework for a channel, waiting out the previous one if need be.

    A channel that comes back already open is the previous channel to this
    device on this number, which the framework keeps handing over until it has
    finished tearing that one down. It sends no open-complete callback for it,
    so waiting on one would wait for ever, and writing to it would deliver
    this caller's bytes to whoever had it before.

    Waiting and asking again is cruder than waiting for the framework to say
    the old channel is gone, but nothing says so. ``rfcommChannelClosed:``
    answers a different question -- it says the channel closed, which it
    reports around thirteen milliseconds after ``closeChannel`` and well
    before the framework stops handing that channel back.

    ``registerForChannelCloseNotification`` was tried, and does not fire at
    all. That was measured rather than assumed: registered on the channel
    with the two-argument selector its documentation asks for, on an observer
    that ``respondsToSelector:`` confirms, with a non-nil notification
    returned -- and nothing arrives, for a close this program asks for or for
    one the peer causes by going away, where the delegate callback fires
    normally. The notification it hands back calls itself "L2CAP Destroyed",
    which may be why: closing one RFCOMM channel does not destroy the L2CAP
    channel that RFCOMM multiplexes over.

    Args:
        device: The connected ``IOBluetoothDevice``.
        number: The RFCOMM channel number.
        address: The device's address, for the error message.

    Returns:
        The channel, which is not open yet.

    Raises:
        ConnectionFailedError: The channel could not be requested, or the
            previous one never finished closing.
    """
    for _attempt in range(_OPEN_ATTEMPTS):
        delegate = _ChannelDelegate.alloc().init()
        handle = objc_id()
        try:
            _check(
                device.openRFCOMMChannelAsync(
                    ctypes.byref(handle), withChannelID=number, delegate=delegate
                )
            )
        except MachError as error:
            raise ConnectionFailedError(
                f"could not open channel {number} on {address}: {error}"
            ) from error

        opened = IOBluetoothRFCOMMChannel(handle)
        if not opened.isOpen():
            return Channel(opened, delegate, device)

        logger.debug(
            "channel %d on %s is still closing from an earlier use; waiting",
            number,
            address,
        )
        # Let go of inside a pool for the same reason Channel.close does it,
        # and without closing it: this is the previous channel, already on its
        # way out under its own steam.
        pool = libobjc.objc_autoreleasePoolPush()
        try:
            del opened, delegate
        finally:
            libobjc.objc_autoreleasePoolPop(pool)
        await asyncio.sleep(_SETTLE_SECONDS)

    raise ConnectionFailedError(
        f"channel {number} on {address} is still open from an earlier "
        "connection and did not finish closing"
    )


async def _connect(device: _Device, address: str) -> None:
    """
    Bring up the baseband link to a device.

    Args:
        device: The ``IOBluetoothDevice``.
        address: Its address, for the error message.

    Raises:
        DeviceNotFoundError: The device did not answer.
        ConnectionFailedError: The link could not be brought up.
    """
    complete = _ConnectionComplete.alloc().init()
    try:
        _check(device.openConnection(complete))
        await complete.future
    except MachError as error:
        raise _translate(error, address) from error


async def _look_up(device: _Device, address: str, service: UUID) -> int:
    """
    Find which RFCOMM channel a service is on.

    The cached record is tried first. macOS fills its cache from a real SDP
    query rather than from advertising data, so a hit here is trustworthy;
    a miss is worth one query before giving up.

    Args:
        device: The connected ``IOBluetoothDevice``.
        address: Its address, for the error message.
        service: The service to find.

    Returns:
        The RFCOMM channel number.

    Raises:
        ServiceNotFoundError: The device does not offer that service.
    """
    uuid = _sdp_uuid(service)
    record = device.getServiceRecordForUUID(uuid)
    if not record:
        complete = _QueryComplete.alloc().init()
        try:
            _check(device.performSDPQuery(complete))
            await complete.future
        except MachError as error:
            raise ServiceNotFoundError(
                f"could not read {address}'s services: {error}"
            ) from error
        record = device.getServiceRecordForUUID(uuid)

    if not record:
        raise ServiceNotFoundError(f"{address} does not offer {service}")

    # A channel number is one byte, and the framework declares the
    # out-parameter as a pointer to unsigned char -- which rubicon reads as
    # ``char *``, so it is the one ctypes type it will accept here. The value
    # comes back as a one-byte string rather than as a number.
    number = ctypes.c_char()
    if record.getRFCOMMChannelID(ctypes.byref(number)):
        raise ServiceNotFoundError(f"{service} on {address} is not an RFCOMM service")
    return number.value[0]


def _translate(error: MachError, address: str) -> RfcommError:
    """
    Turn an IOBluetooth status into one of ours.

    Args:
        error: What IOBluetooth reported.
        address: The device being reached.

    Returns:
        The error to raise instead.
    """
    if error.code == _IO_TIMEOUT:
        return DeviceNotFoundError(
            f"{address} did not answer; it may be switched off or out of range"
        )
    return ConnectionFailedError(f"could not reach {address}: {error}")


# --------------------------------------------------------------------------
# Odds and ends
# --------------------------------------------------------------------------


def _each(service: UUID | Collection[UUID] | None) -> list[UUID]:
    """
    Normalise the service filter to a list.

    Args:
        service: One service, several, or ``None`` for no filter.

    Returns:
        The services to match, empty for no filter.
    """
    if service is None:
        return []
    if isinstance(service, UUID):
        return [service]
    return list(service)


def _sdp_uuid(service: UUID) -> _UUID:
    """
    Turn a Python UUID into the framework's own.

    Args:
        service: The service UUID.

    Returns:
        The matching ``IOBluetoothSDPUUID``.
    """
    return IOBluetoothSDPUUID.uuidWithData(service.bytes)


def _normalise(address: str) -> str:
    """
    Put a Bluetooth address into the form this library reports.

    IOBluetooth writes device addresses with dashes and in lower case, and
    controller addresses with colons and in upper case. Callers compare these
    against addresses from other platforms, so they are all made to look the
    same.

    Args:
        address: The address as IOBluetooth wrote it.

    Returns:
        The address, colon separated and upper case.
    """
    return address.replace("-", ":").upper()
