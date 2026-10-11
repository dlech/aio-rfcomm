# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Stand-ins for IOBluetooth and CoreBluetooth, and nothing above them.

Each one reproduces what the real framework was measured doing on macOS 26.6.2
against a BlueZ peer, and every report reaches the library through the
delegate's own Objective-C methods, so the code that receives it runs for
real. Reports are deferred to the next turn of the loop, because the framework
always reports later than the call that asked; the measured latencies are
noted but not reproduced, since none of the code under test depends on them.

macOS only: the manager is a real Objective-C object, so key-value observing
it is the real thing too.
"""

import asyncio
import ctypes
from collections.abc import Generator
from typing import ClassVar

import pytest
from rubicon.objc import NSObject, objc_method, objc_property
from rubicon.objc.runtime import load_library

load_library("AppKit")

from aio_rfcomm.backend.iobluetooth import _native
from aio_rfcomm.backend.iobluetooth._native import CBManagerState

# CBManagerAuthorization, as read_authorization reports it.
ALLOWED = 3
DENIED = 2

# CBErrorConnectionFailed, which is how an unanswered page is reported:
# measured as "Inform delegate connectionComplete: 10" 15.4 s after
# openConnection: had already returned success. The controller said Page
# Timeout; CoreBluetooth passes on only the code of the error it makes of it.
CONNECTION_FAILED = 10

# kIOReturnNotOpen, what writeAsync returned once the channel had closed.
NOT_OPEN = ctypes.c_int32(0xE00002CD).value


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
            return NOT_OPEN
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
        # Success either way: an unanswered page is reported later, through
        # the target, as a CoreBluetooth error code.
        status = 0 if self.answers else CONNECTION_FAILED
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


def fake_framework(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: int = CBManagerState.POWERED_ON,
    authorization: int = ALLOWED,
    answers: bool = True,
    connected: bool = True,
) -> FakeManager:
    """
    Stand in for IOBluetooth and CoreBluetooth, and nothing else.

    Everything in the library and in the native layer runs for real; only what
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
    fake = FakeManager.alloc().init()
    fake.state = int(state)
    monkeypatch.setattr(_native, "ObjCClass", lambda _name: FakeCoordinatorClass(fake))
    monkeypatch.setattr(_native, "CBManager", FakeAuthorization(authorization))
    monkeypatch.setattr(_native, "IOBluetoothDevice", FakeDeviceClass)
    monkeypatch.setattr(_native, "IOBluetoothRFCOMMChannel", wrap_channel)
    monkeypatch.setattr(FakeDeviceClass, "answers", answers)
    monkeypatch.setattr(FakeDeviceClass, "connected", connected)
    return fake


@pytest.fixture(autouse=True)
def clean_state() -> Generator[None, None, None]:
    """
    Clear the state the native layer and the fake framework keep per process.

    Objective-C registers its classes by name, so the module cannot be
    reloaded to get this back; clearing what it holds is the nearest thing to
    the fresh process each test is written as though it had.
    """
    _native._in_use.clear()
    _native._closed_at.clear()
    FakeDeviceClass.made.clear()
    opened.clear()
    yield
    _native._in_use.clear()
    _native._closed_at.clear()
    FakeDeviceClass.made.clear()
    opened.clear()
