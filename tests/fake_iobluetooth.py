# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Stand-ins for IOBluetooth and CoreBluetooth, and nothing above them.

Each one reproduces what the real framework was measured doing on macOS 26.6.2
against a BlueZ 5.85 peer, and every report reaches the library through the
delegate's own Objective-C methods, so the code that receives it runs for
real. Reports are deferred to the next turn of the loop, because the framework
always reports later than the call that asked. The measured latencies are
noted but not reproduced, since none of the code under test depends on them.

macOS only: the manager is a real Objective-C object, so key-value observing
it is the real thing too.
"""

# Measurements cited in this module were made on macOS 26.6.2 (25G83), with a
# BCM_4387 controller on firmware 23.5.636.5345, unless a claim says otherwise.
# The peers were a Linux machine, on BlueZ 5.72 until 2026-10-05 and BlueZ 5.85
# after, and a Pybricks EV3 on firmware 4.1.0b5. Claims that depend on the
# peer name its version.

import asyncio
import ctypes
import threading
from collections.abc import Generator
from ctypes import c_int, c_void_p
from typing import Any, ClassVar, cast

import pytest
from rubicon.objc import (
    SEL,
    NSObject,
    NSUInteger,
    ObjCInstance,
    objc_id,
    objc_method,
    objc_property,
    py_from_ns,
    send_message,
    send_super,
)
from rubicon.objc.runtime import load_library, set_ivar
from typing_extensions import override

load_library("AppKit")

from aio_rfcomm.backend.iobluetooth.codes import (
    CBError,
    CBManagerAuthorization,
    CBManagerState,
    IOReturn,
)
from aio_rfcomm.backend.iobluetooth.helper import native


class FakeManager(NSObject):
    """
    A stand-in for ``CBClassicManager``, observable the same way.
    """

    state = objc_property(int)

    @objc_method
    def init(self):
        # Held as a plain int, because a test needs to set a value that is not
        # a CBManagerState at all.
        self.state = int(CBManagerState.UNKNOWN)
        return self


class SettlingManager(FakeManager):
    """
    A stand-in for ``CBClassicManager`` in a process that has only just
    reached it.

    Measured in fresh processes: the state reads 0 when an observer is added,
    settles on powered on as it is, and no notification ever reports the
    change. Reproduced by settling once the registration is done, without
    going through the setter that would notify.
    """

    @objc_method
    def addObserver_forKeyPath_options_context_(
        self,
        observer: ObjCInstance,
        path: ObjCInstance,
        options: int,
        context: c_void_p,
    ) -> None:
        send_super(
            __class__,  # type: ignore[name-defined]
            self,
            "addObserver:forKeyPath:options:context:",
            observer,
            path,
            options,
            context,
            argtypes=[objc_id, objc_id, NSUInteger, c_void_p],
        )
        set_ivar(self, "_state", c_int(int(CBManagerState.POWERED_ON)))


def change_state(manager: FakeManager, *states: int) -> None:
    """
    Move the manager through states the way the framework does: from a thread
    of its own.

    Measured across real toggles: every transition was delivered to the
    observer on a framework thread, never on the main one. Key-value observing
    notifies on the thread that sets the property, so setting it from another
    thread reproduces that.

    The loop's own thread check is turned on as well. Here the loop's thread
    is only waiting for this one to finish and picks up a careless call on its
    next pass. In the helper it would be asleep in its run loop, and nothing
    would wake it.

    Args:
        manager: The fake manager.
        states: The states to pass through, in order.
    """

    def run() -> None:
        for state in states:
            manager.state = int(state)

    asyncio.get_running_loop().set_debug(True)
    framework = threading.Thread(target=run)
    framework.start()
    framework.join()


async def until_observed(manager: FakeManager) -> None:
    """
    Wait until something is observing the manager's state.

    Without this a test could change the state before the observer is
    registered, and pass on the initial notification without ever taking the
    path a real transition takes.
    """
    async with asyncio.timeout(1):
        while not is_observed(manager):
            await asyncio.sleep(0)


def is_observed(manager: FakeManager) -> bool:
    """
    Whether anything is observing the manager at all.
    """
    # A property, though the stubs only know it as a method.
    info = cast(ctypes.c_void_p, manager.observationInfo)
    return info.value is not None


class FakeAuthorization:
    """
    Stands in for ``CBManager``, which is only ever asked one thing.
    """

    def __init__(self, authorization: int) -> None:
        self.authorization = authorization


class FakeCoordinator:
    """
    Stands in for ``IOBluetoothCoreBluetoothCoordinator``'s shared instance.
    """

    def __init__(self, manager: FakeManager) -> None:
        self.classicManager = manager


class FakeCoordinatorClass:
    """
    Stands in for the coordinator class itself, as rubicon hands it over.
    """

    def __init__(self, manager: FakeManager) -> None:
        self.shared = FakeCoordinator(manager)

    def sharedInstance(self) -> FakeCoordinator:
        return self.shared


class FakeChannel:
    """
    An ``IOBluetoothRFCOMMChannel`` that opens, carries bytes and closes.

    Measured: the open completes about 80 ms after it is asked for, a write
    completes within a millisecond with every byte written, and a close is
    reported 33 ms after ``closeChannel`` returns. All on the main thread.
    """

    # What getMTU returned while the open was pending, and once it completed.
    PENDING_MTU = 672
    OPEN_MTU = 667

    def __init__(self, device: "FakeDevice", number: int, delegate: object) -> None:
        self.device = device
        self.number = number
        self.delegate: object = delegate
        self.open = False
        self.written = bytearray()

    def isOpen(self) -> bool:
        return self.open

    def getMTU(self) -> int:
        return self.OPEN_MTU if self.open else self.PENDING_MTU

    def writeAsync(self, data: bytes, /, *, length: int, refcon: int) -> int:
        if not self.open:
            return IOReturn.NOT_OPEN
        self.written += data[:length]
        asyncio.get_running_loop().call_soon(
            self.delegate.rfcommChannelWriteComplete_refcon_status_bytesWritten_,  # pyright: ignore[reportAttributeAccessIssue]
            None,
            refcon,
            0,
            length,
        )
        return 0

    def closeChannel(self) -> int:
        if self.open:
            self.open = False
            asyncio.get_running_loop().call_soon(self._report_closed)
        return 0

    # -- what the peer and the framework do ------------------------------

    def complete_open(self) -> None:
        self.open = True
        self.delegate.rfcommChannelOpenComplete_status_(None, 0)  # pyright: ignore[reportAttributeAccessIssue]

    def arrive(self, data: bytes) -> None:
        """
        Deliver bytes from the peer, in a buffer the callback may only borrow.
        """
        buffer = ctypes.create_string_buffer(data, len(data))
        self.delegate.rfcommChannelData_data_length_(  # pyright: ignore[reportAttributeAccessIssue]
            None, ctypes.addressof(buffer), len(data)
        )

    def hang_up(self) -> None:
        """
        The peer closes this channel and keeps the link up.
        """
        self.open = False
        self._report_closed()

    def _report_closed(self) -> None:
        self.delegate.rfcommChannelClosed_(None)  # pyright: ignore[reportAttributeAccessIssue]


class FakeDevice:
    """
    An ``IOBluetoothDevice``, one per address for the life of the process.

    The framework keeps every device it makes in a table it never empties, so
    asking for an address twice gives the same object back.
    """

    def __init__(self, address: str, connected: bool, answers: bool) -> None:
        self.address = address
        self.connected = connected
        self.answers = answers
        self.channels: list[FakeChannel] = []

    def isConnected(self) -> bool:
        return self.connected

    def openConnection(self, target: object, /) -> int:
        # Success either way. An unanswered page is reported later, through
        # the target, as a CoreBluetooth error code rather than an IOKit
        # status: measured as "Inform delegate connectionComplete: 10" 15.4 s
        # after the call returned. The controller said Page Timeout.
        status = 0 if self.answers else CBError.CONNECTION_FAILED
        if self.answers:
            self.connected = True
        asyncio.get_running_loop().call_soon(
            target.connectionComplete_status_,  # pyright: ignore[reportAttributeAccessIssue]
            None,
            status,
        )
        return 0

    def openRFCOMMChannelAsync(
        self, _out: object, /, *, withChannelID: int, delegate: object
    ) -> int:
        channel = FakeChannel(self, withChannelID, delegate)
        self.channels.append(channel)
        opened.append(channel)
        asyncio.get_running_loop().call_soon(channel.complete_open)
        return 0


class FakeDeviceClass:
    """
    Hands back the device for an address, or nothing for the unknown one.
    """

    unknown = "not an address"
    #: Set by a test that wants devices not to answer their page.
    answers = True
    #: Set by a test that wants the link brought up rather than already there.
    connected = True
    #: Every device handed out, by address.
    made: ClassVar[dict[str, FakeDevice]] = {}

    @classmethod
    def deviceWithAddressString(cls, address: str, /) -> FakeDevice | None:
        if address == cls.unknown:
            return None
        if address not in cls.made:
            cls.made[address] = FakeDevice(address, cls.connected, cls.answers)
        return cls.made[address]


#: Every channel asked for, in order.
opened: list[FakeChannel] = []


def wrap_channel(_handle: object) -> FakeChannel:
    """
    Stand in for wrapping the pointer the framework filled in: the channel the
    last open made.
    """
    return opened[-1]


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


class FakeDeviceObject(NSObject):
    """
    An ``IOBluetoothDevice`` as the framework hands one over with an incoming
    channel: a real object, reached through Objective-C.
    """

    addressString = objc_property()
    name = objc_property()
    connected: bool

    @objc_method
    def isConnected(self) -> bool:
        return bool(self.connected)


class FakeIncomingChannel(NSObject):
    """
    An ``IOBluetoothRFCOMMChannel`` a peer opened, handed over open and with
    no delegate.

    A real object, since the framework passes it through Objective-C. Measured:
    the open is reported complete once a delegate is set.
    """

    inner: FakeChannel
    device: ObjCInstance

    @objc_method
    def setDelegate_(self, delegate: ObjCInstance) -> int:
        inner = self.inner
        inner.delegate = delegate
        asyncio.get_running_loop().call_soon(inner.complete_open)
        return 0

    @objc_method
    def getDevice(self) -> ObjCInstance:
        return self.device

    @objc_method
    def isOpen(self) -> bool:
        return self.inner.open

    @objc_method
    def getMTU(self) -> int:
        return self.inner.getMTU()

    @objc_method
    def closeChannel(self) -> int:
        return self.inner.closeChannel()

    @objc_method
    def writeAsync_length_refcon_(
        self, data: c_void_p, length: int, refcon: c_void_p
    ) -> int:
        # The ctypes annotations are for rubicon's signature. What arrives are
        # plain integers.
        piece = ctypes.string_at(data, length)
        return self.inner.writeAsync(piece, length=length, refcon=cast(int, refcon))


class FakeNotification:
    """
    What registering for open notifications hands back.

    Measured from the disassembly: unregistering only stops this process
    listening, and never reaches bluetoothd.
    """

    def __init__(self, observer: ObjCInstance, selector: SEL, number: int) -> None:
        self.observer = observer
        self.selector = selector
        self.number = number
        self.registered = True

    def unregister(self) -> None:
        self.registered = False

    @override
    def __str__(self) -> str:
        return f"Notification L2CAP Published In, CID: {self.number}"


class FakeChannelClass:
    """
    ``IOBluetoothRFCOMMChannel`` itself: wrapping pointers the framework
    filled in, and registering for channels peers open.
    """

    def __init__(self, manager: FakeManager) -> None:
        self.manager = manager
        #: Every registration that reached bluetoothd, in order.
        self.registrations: list[FakeNotification] = []

    def __call__(self, handle: object) -> FakeChannel:
        return wrap_channel(handle)

    def registerForChannelOpenNotifications(
        self,
        observer: ObjCInstance,
        /,
        *,
        selector: object,
        withChannelID: int,
        direction: int,
    ) -> FakeNotification:
        # Measured: a string is stored as a selector of its first letter.
        chosen = selector if isinstance(selector, SEL) else SEL(str(selector)[0])
        notification = FakeNotification(observer, chosen, withChannelID)
        # Measured: registered for any direction, or before the manager is
        # ready, it never reaches bluetoothd at all.
        if direction == 1 and self.manager.state == CBManagerState.POWERED_ON:
            self.registrations.append(notification)
        return notification

    # -- what peers do ----------------------------------------------------

    def connect_in(self, address: str, number: int) -> FakeChannel:
        """
        A peer opens a channel to this Mac.

        bluetoothd holds one registration per process, the latest, and the
        framework hands the channel over on the main queue -- or, with nobody
        there to take it, lets it go, which closes it.

        Returns:
            The channel, as the framework sees it.
        """
        device = FakeDeviceObject.alloc().init()
        # As IOBluetooth writes a device's address.
        cast(Any, device).addressString = address.replace(":", "-").lower()
        cast(Any, device).name = "fake peer"
        device.connected = True
        inner = FakeChannel(cast("FakeDevice", None), number, None)
        inner.open = True
        incoming = FakeIncomingChannel.alloc().init()
        incoming.inner = inner
        incoming.device = device

        latest = self.registrations[-1] if self.registrations else None
        taken = (
            latest is not None
            and latest.registered
            and latest.number == number
            and latest.observer.respondsToSelector_(latest.selector)
        )
        if taken:
            assert latest is not None
            asyncio.get_running_loop().call_soon(
                # The notification itself is not passed: nothing reads it.
                lambda: send_message(
                    latest.observer,
                    latest.selector,
                    None,
                    incoming,
                    restype=None,
                    argtypes=[objc_id, objc_id],
                )
            )
        else:
            inner.open = False
        return inner


class FakePublished:
    """
    A service record that has been published.
    """

    def __init__(self, record: dict[str, object]) -> None:
        self.record = record
        self.withdrawn = False

    def removeServiceRecord(self) -> int:
        # Asked, and refused: measured, bluetoothd answers every removal
        # OI_STATUS_INVALID_HANDLE and goes on advertising the record.
        self.withdrawn = True
        return 0


class FakeLocalRecord:
    """
    A service published on this Mac.
    """

    def __init__(self, number: int | None) -> None:
        self.number = number

    def getRFCOMMChannelID(self, out: object, /) -> int:
        if self.number is None:
            # What a record that is not an RFCOMM service returned, measured.
            return -536870160
        out._obj.value = bytes([self.number])  # type: ignore[attr-defined]
        return 0


class FakeDeviceManager:
    """
    ``BluetoothDeviceManager``'s shared instance, asked only for this Mac's
    own services.
    """

    def __init__(self, in_use: list[int]) -> None:
        # Measured on this Mac: ten local records, two on RFCOMM channels.
        self.records = [FakeLocalRecord(n) for n in in_use] + [
            FakeLocalRecord(None)
        ] * 8

    def getLocalServices(self) -> "FakeLocalServices":
        return FakeLocalServices(self.records)


class FakeLocalServices:
    def __init__(self, records: list[FakeLocalRecord]) -> None:
        self._records = records

    def allValues(self) -> list[FakeLocalRecord]:
        return self._records


class FakeDeviceManagerClass:
    def __init__(self, in_use: list[int]) -> None:
        self.shared = FakeDeviceManager(in_use)

    def sharedDeviceManager(self) -> FakeDeviceManager:
        return self.shared


class FakeRecordClass:
    """
    ``IOBluetoothSDPServiceRecord``, publishing to this Mac's own services.
    """

    def __init__(self, devices: FakeDeviceManagerClass) -> None:
        self.published: list[FakePublished] = []
        self._devices = devices

    def publishedServiceRecordWithDictionary(
        self, record: ObjCInstance, /
    ) -> FakePublished:
        published = FakePublished(py_from_ns(cast(Any, record)))
        self.published.append(published)
        attributes = cast(Any, published.record)
        self._devices.shared.records.append(
            FakeLocalRecord(attributes["0004"][1][1]["DataElementValue"])
        )
        return published


class Framework:
    """
    The fake framework's pieces a test may want to reach.
    """

    def __init__(self, manager: FakeManager) -> None:
        self.manager = manager
        self.channels = FakeChannelClass(manager)
        self.devices = FakeDeviceManagerClass([1, 8])
        self.records = FakeRecordClass(self.devices)

    def find_class(self, name: str) -> object:
        """
        Stand in for ``ObjCClass``, for the private classes looked up by name.
        """
        match name:
            case native._COORDINATOR:
                return FakeCoordinatorClass(self.manager)
            case native._DEVICE_MANAGER:
                return self.devices
            case _:
                raise NameError(f"ObjC Class {name} couldn't be found.")


def fake_framework(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: int = CBManagerState.POWERED_ON,
    authorization: int = CBManagerAuthorization.ALLOWED_ALWAYS,
    answers: bool = True,
    connected: bool = True,
) -> FakeManager:
    """
    Stand in for IOBluetooth and CoreBluetooth, and nothing else.

    Everything in the library and in the native layer runs for real. Only what
    needs a radio is replaced.

    Args:
        monkeypatch: Where to record the replacements.
        state: What the manager says about itself.
        authorization: What CBManager reports.
        answers: Whether the device answers when paged.
        connected: Whether the link is already up.

    Returns:
        The fake manager, so a test can move it on afterwards.
    """
    return fake_framework_parts(
        monkeypatch,
        state=state,
        authorization=authorization,
        answers=answers,
        connected=connected,
    ).manager


def fake_framework_parts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: int = CBManagerState.POWERED_ON,
    authorization: int = CBManagerAuthorization.ALLOWED_ALWAYS,
    answers: bool = True,
    connected: bool = True,
) -> Framework:
    """
    :func:`fake_framework`, handing back every part rather than the manager.
    """
    fake = FakeManager.alloc().init()
    fake.state = int(state)
    framework = Framework(fake)
    monkeypatch.setattr(native, "ObjCClass", framework.find_class)
    monkeypatch.setattr(native, "CBManager", FakeAuthorization(authorization))
    monkeypatch.setattr(native, "IOBluetoothDevice", FakeDeviceClass)
    monkeypatch.setattr(native, "IOBluetoothRFCOMMChannel", framework.channels)
    monkeypatch.setattr(native, "IOBluetoothSDPServiceRecord", framework.records)
    monkeypatch.setattr(FakeDeviceClass, "answers", answers)
    monkeypatch.setattr(FakeDeviceClass, "connected", connected)
    return framework


@pytest.fixture(autouse=True)
def clean_state() -> Generator[None, None, None]:
    """
    Clear the state the native layer and the fake framework keep per process.

    Objective-C registers its classes by name, so the module cannot be
    reloaded to get this back. Clearing what it holds is the nearest thing to
    the fresh process each test is written as though it had.
    """
    native._in_use.clear()
    native._closed_at.clear()
    FakeDeviceClass.made.clear()
    opened.clear()
    yield
    native._in_use.clear()
    native._closed_at.clear()
    FakeDeviceClass.made.clear()
    opened.clear()
