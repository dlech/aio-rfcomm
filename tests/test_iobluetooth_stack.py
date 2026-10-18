# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The macOS backend end to end, with only the framework stood in for.

The library and its helper are otherwise tested on either side of the socket
between them, which cannot show that the two halves agree. Here both run in
this process: the public API over the real backend and helper client on one
end of a socket pair, the helper's own request loop and the native layer on
the other, and the fake framework beneath that. Only spawning the helper is
left out, since the child would not have the fake.
"""

import asyncio
import socket
import sys
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID

assert not TYPE_CHECKING or sys.platform == "darwin"

import pytest
from typing_extensions import override

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.iobluetooth import IOBluetoothBackend, IOBluetoothBluetooth
from aio_rfcomm.backend.iobluetooth.codes import CBManagerAuthorization, CBManagerState
from aio_rfcomm.backend.iobluetooth.helper import __main__ as helper_main
from aio_rfcomm.backend.iobluetooth.helper.native import bindings
from aio_rfcomm.backend.iobluetooth.process import Helper, HelperFailedError
from aio_rfcomm.backend.iobluetooth.protocol import Link
from aio_rfcomm.bluetooth import Bluetooth, _open_bluetooth
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    ChannelInUseError,
    DeviceNotFoundError,
    RfcommError,
)
from aio_rfcomm.service import RfcommConnectionRequest, RfcommService
from tests.fake_iobluetooth import (
    FakeController,
    FakeDeviceClass,
    FakeManager,
    Framework,
    change_state,
    # An autouse fixture, used by being imported.
    clean_state,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    fake_framework,
    fake_framework_parts,
    opened,
    until_observed,
)

ADDRESS = "00:1A:7D:DA:71:0C"
DEVICE = RfcommDeviceInfo(ADDRESS)


class _Helpers:
    """
    Starts helpers in this process, the way the backend starts real ones.
    """

    def __init__(self) -> None:
        self.started: list[tuple[Helper, socket.socket]] = []

    @asynccontextmanager
    async def start(self) -> AsyncGenerator[Helper, None]:
        near, far = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        helper = Helper(Link(near))
        self.started.append((helper, far))
        serving = asyncio.ensure_future(helper_main._serve(far))
        reading = asyncio.ensure_future(helper.deliver())
        try:
            yield helper
        finally:
            # What run_helper ends with: whatever still waits is released, and
            # the helper reads end of file and stops.
            helper.stop(HelperFailedError("the helper was stopped"))
            helper._link.close()
            async with asyncio.timeout(1):
                await asyncio.gather(serving, return_exceptions=True)
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)

    def kill(self) -> None:
        """
        End the running helper without warning, as a crash would.
        """
        _helper, far = self.started[-1]
        far.shutdown(socket.SHUT_RDWR)


class _HeldHelper:
    """
    A helper the test speaks for, which can stay held as a permission dialog
    holds a real one.
    """

    def __init__(
        self,
        says: list[tuple[int, int]],
        explain: Callable[[], Awaitable[RfcommError]] | None = None,
        name: str | None = None,
    ) -> None:
        """
        Args:
            says: The states it reports as it starts, as the manager's state
                and the authorization.
            explain: Why it stopped, once it has.
            name: The name macOS asks about it under.
        """
        self.says = says
        self.explain = explain
        self.name = name
        self.far: Link | None = None
        self.helper: Helper | None = None
        self.starts = 0

    @asynccontextmanager
    async def start(self) -> AsyncGenerator[Helper, None]:
        self.starts += 1
        near, far = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.far = Link(far)
        for state, authorization in self.says:
            self.far.send(
                {"event": "state", "state": state, "authorization": authorization}
            )
        helper = Helper(Link(near), self.name)
        self.helper = helper
        reading = asyncio.ensure_future(helper.deliver(self.explain))
        try:
            yield helper
        finally:
            helper.stop(HelperFailedError("the helper was stopped"))
            helper._link.close()
            self.far.close()
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)

    async def answer_ready(self) -> None:
        """
        Answer the "ready" the library is waiting on.
        """
        assert self.far is not None
        async with asyncio.timeout(1):
            frame = await self.far.receive()
        assert frame is not None
        message, _fds = frame
        assert message["op"] == "ready"
        self.far.send({"id": message["id"], "ok": None})

    def stop(self) -> None:
        """
        End the helper, as macOS ending it would.
        """
        assert self.far is not None
        self.far.close()


class _Dialog:
    """
    A permission dialog left open, as the helper's own framework calls meet it.

    Measured on macOS 26.6.2: the first to reach the coordinator waits in its
    init until the dialog is answered, and a call that reaches it meanwhile,
    as ``IOBluetoothHostController.defaultController`` does, waits with it.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, framework: Framework) -> None:
        self.answered = threading.Event()
        self.held: list[threading.Thread] = []
        dialog = self

        def find_class(name: str) -> object:
            if name == bindings.COORDINATOR:
                dialog.wait()
            return framework.find_class(name)

        class Controller(FakeController):
            @classmethod
            @override
            def defaultController(cls) -> FakeController:
                dialog.wait()
                return super().defaultController()

        monkeypatch.setattr(bindings, "ObjCClass", find_class)
        monkeypatch.setattr(bindings, "IOBluetoothHostController", Controller)

    def wait(self) -> None:
        self.held.append(threading.current_thread())
        # Bounded, so that a helper holding its loop's thread here fails the
        # test rather than hanging it.
        self.answered.wait(5)


class _RestartedDialog:
    """
    A permission dialog left open across a bluetoothd restart.

    Measured on macOS 26.6.2: the restart let the first helper's coordinator
    init go on with the dialog still open, and its classic manager was not
    approved. A helper started after that waited in init until the dialog was
    answered, and macOS settled its request with the same answer.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, framework: Framework) -> None:
        self.restarted = threading.Event()
        self.answered = threading.Event()
        self.first: threading.Thread | None = None
        dialog = self

        def find_class(name: str) -> object:
            if name == bindings.COORDINATOR:
                dialog.wait()
            return framework.find_class(name)

        monkeypatch.setattr(bindings, "ObjCClass", find_class)

    def wait(self) -> None:
        here = threading.current_thread()
        if here is threading.main_thread():
            # A request, which waits until the manager has been reached.
            return

        if self.first is None:
            self.first = here
        # Bounded, so that a mistake fails the test rather than hanging it.
        if here is self.first:
            self.restarted.wait(5)
        else:
            self.answered.wait(5)

    def end(self) -> None:
        self.restarted.set()
        self.answered.set()


@asynccontextmanager
async def _stack(helpers: _Helpers | None = None) -> AsyncGenerator[Bluetooth, None]:
    """
    Bluetooth whose helpers run in this process, over the fake framework.

    Yields:
        The Bluetooth object, as a caller of the library holds it.
    """
    helpers = helpers or _Helpers()
    async with _open_bluetooth(IOBluetoothBackend(helpers.start)) as bluetooth:
        yield bluetooth


async def _next_availability(bluetooth: Bluetooth, wanted: Availability) -> None:
    async with asyncio.timeout(1):
        async for availability in bluetooth.watch_availability():
            if availability is wanted:
                return


async def test_bytes_cross_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        fake = opened[-1]

        await channel.send(b"hello\n")
        # The bytes cross the helper before they reach the framework.
        await asyncio.sleep(0.01)
        assert bytes(fake.written) == b"hello\n"

        fake.arrive(b"ch5:hello\n")
        async with asyncio.timeout(1):
            assert await channel.receive() == b"ch5:hello\n"


async def test_a_peer_that_hangs_up_ends_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on macOS 26.6.2 against a peer that drops an idle connection: the
    channel is reported closed while the link stays up, which is an orderly
    end.
    """
    fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        opened[-1].hang_up()
        async with asyncio.timeout(1):
            with pytest.raises(ChannelClosedError):
                await channel.receive()


async def test_what_arrives_as_the_peer_hangs_up_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The last bytes and the hang-up can arrive together while the helper is
    still waiting to pass on what came before. The bytes go first.
    """
    fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        fake = opened[-1]
        # More than every buffer between here and the helper holds.
        fake.arrive(bytes(4 * 1024 * 1024))
        await asyncio.sleep(0.05)
        fake.arrive(b"tail")
        fake.hang_up()

        received = bytearray()
        async with asyncio.timeout(5):
            with pytest.raises(ChannelClosedError):
                while True:
                    received += await channel.receive()

        assert len(received) == 4 * 1024 * 1024 + 4
        assert received.endswith(b"tail")


async def test_a_lost_link_ends_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        fake = opened[-1]
        fake.device.connected = False
        fake.hang_up()
        async with asyncio.timeout(1):
            with pytest.raises(ChannelClosedError):
                await channel.receive()


async def test_a_closed_channel_leaves_the_device_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Found only by running the library the way a caller does: closing returned
    while the helper still held the device, and the next open was refused.
    """
    fake_framework(monkeypatch)
    async with _stack() as bluetooth:
        for _round in range(3):
            async with bluetooth.open_channel(DEVICE, 5) as channel:
                await channel.send(b"x")
        channels = FakeDeviceClass.made[ADDRESS].channels
        assert len(channels) == 3
        assert not any(c.open for c in channels)


async def test_a_device_that_does_not_answer_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    On macOS 26.6.2, the page is accepted and its timeout reported later, as a
    CoreBluetooth error code. That has to arrive here as the device not
    answering.
    """
    fake_framework(monkeypatch, answers=False, connected=False)
    async with _stack() as bluetooth:
        with pytest.raises(DeviceNotFoundError, match="did not answer"):
            async with bluetooth.open_channel(DEVICE, 5):
                pass


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


async def test_the_first_reading_is_the_manager_s_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework(monkeypatch, state=CBManagerState.POWERED_OFF)
    async with _stack() as bluetooth:
        assert await anext(bluetooth.watch_availability()) is Availability.SWITCHED_OFF
        with pytest.raises(BluetoothUnavailableError, match="Turn it on"):
            async with bluetooth.open_channel(DEVICE, 5):
                pass


async def test_a_radio_switched_off_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = fake_framework(monkeypatch)
    async with _stack() as bluetooth:
        await until_observed(manager)
        change_state(manager, CBManagerState.POWERED_OFF)
        await _next_availability(bluetooth, Availability.SWITCHED_OFF)

        change_state(manager, CBManagerState.POWERED_ON)
        await _next_availability(bluetooth, Availability.USABLE)


async def test_a_radio_switched_off_with_a_channel_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on a clean toggle on macOS 26.6.2: the manager says powered off,
    and a millisecond later the channel hears its link drop. The radio is why.
    """
    manager = fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        fake = opened[-1]
        await until_observed(manager)
        change_state(manager, CBManagerState.POWERED_OFF)
        fake.device.connected = False
        fake.hang_up()
        async with asyncio.timeout(1):
            with pytest.raises(BluetoothUnavailableError) as caught:
                await channel.receive()
        assert caught.value.reason is Availability.SWITCHED_OFF


async def test_an_open_the_helper_refuses_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The helper checks the radio itself before opening, and the reason has to
    survive the trip back across the socket.
    """
    fake_framework(monkeypatch, state=CBManagerState.POWERED_OFF)
    helpers = _Helpers()
    async with helpers.start() as helper:
        bluetooth = IOBluetoothBluetooth()
        bluetooth.helper = helper
        with pytest.raises(BluetoothUnavailableError) as caught:
            async with asyncio.timeout(1), bluetooth.open_channel(DEVICE, 5):
                pass
    assert caught.value.reason is Availability.SWITCHED_OFF


# --------------------------------------------------------------------------
# The Bluetooth service restarting
# --------------------------------------------------------------------------


async def _restart(manager: FakeManager) -> None:
    await until_observed(manager)
    # Measured on macOS 26.6.2 when bluetoothd restarted on its way to
    # powering off. Here it comes back on.
    change_state(manager, CBManagerState.RESETTING, CBManagerState.POWERED_ON)


async def test_a_restart_brings_a_new_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    On macOS 26.6.2, after bluetoothd restarts, every device object in the old
    helper stays bound to the old daemon, so only a new helper recovers.
    """
    manager = fake_framework(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.USABLE

        await _restart(manager)
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE
            assert await anext(watcher) is Availability.USABLE
        assert len(helpers.started) == 2

        async with bluetooth.open_channel(DEVICE, 5) as channel:
            await channel.send(b"through the new helper")


async def test_a_restart_ends_the_channels_it_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on macOS 26.6.2: after a restart a channel is never told, and
    writes to it fail quietly. It has to be ended from here.
    """
    manager = fake_framework(monkeypatch)
    async with _stack() as bluetooth, bluetooth.open_channel(DEVICE, 5) as channel:
        await _restart(manager)
        async with asyncio.timeout(1):
            with pytest.raises(BluetoothUnavailableError) as caught:
                await channel.receive()
        assert caught.value.reason is Availability.SERVICE_UNAVAILABLE


async def test_a_helper_that_stops_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.USABLE

        helpers.kill()
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE
            assert await anext(watcher) is Availability.USABLE
        assert len(helpers.started) == 2


async def test_a_first_helper_that_will_not_start_is_an_error() -> None:
    @asynccontextmanager
    async def broken() -> AsyncGenerator[Helper, None]:
        raise HelperFailedError("the helper stopped during startup")
        yield  # pragma: no cover

    with pytest.raises(HelperFailedError):
        async with _open_bluetooth(IOBluetoothBackend(broken)):
            pass


async def test_a_helper_refused_bluetooth_is_not_allowed() -> None:
    """
    macOS 26.6.2 ends a helper it will not let use Bluetooth, and would end a
    new one the same way, so nothing is retried and the reason is reported.
    """

    @asynccontextmanager
    async def refused() -> AsyncGenerator[Helper, None]:
        raise BluetoothUnavailableError(
            Availability.NOT_ALLOWED, "call prompt_under_name() first"
        )
        yield  # pragma: no cover

    async with _open_bluetooth(IOBluetoothBackend(refused)) as bluetooth:
        assert await anext(bluetooth.watch_availability()) is Availability.NOT_ALLOWED
        with pytest.raises(BluetoothUnavailableError, match="prompt_under_name"):
            await bluetooth.list_known_devices()


async def test_a_replacement_that_will_not_start_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Retrying at once would only fail again, as fast as it could.
    """
    fake_framework(monkeypatch)
    helpers = _Helpers()
    attempts = 0

    @asynccontextmanager
    async def first_only() -> AsyncGenerator[Helper, None]:
        nonlocal attempts
        attempts += 1
        if helpers.started:
            raise HelperFailedError("could not start the macOS Bluetooth helper")
        async with helpers.start() as helper:
            yield helper

    async with _open_bluetooth(IOBluetoothBackend(first_only)) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.USABLE

        helpers.kill()
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE
            # Said once the replacement has failed, a moment later.
            while True:
                with pytest.raises(BluetoothUnavailableError) as caught:
                    await bluetooth.list_known_devices()
                if "could not be restarted" in str(caught.value):
                    break
                await asyncio.sleep(0.01)

        await asyncio.sleep(0.05)
    assert attempts == 2


async def test_a_permission_dialog_nobody_answers_is_awaited() -> None:
    """
    Measured on GitHub's macOS 15.7.9 and 26.6.2 runners: a helper asking
    under its own name read NOT_DETERMINED, and was then held for as long as
    the permission dialog was open, which there was forever.
    """
    unasked = CBManagerAuthorization.NOT_DETERMINED
    held = _HeldHelper([(CBManagerState.UNKNOWN, unasked)])

    async with _open_bluetooth(IOBluetoothBackend(held.start)) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.AWAITING_PERMISSION
        with pytest.raises(BluetoothUnavailableError, match="Answer the permission"):
            await bluetooth.list_known_devices()


@pytest.mark.parametrize(
    ("answer", "state", "availability"),
    [
        (
            CBManagerAuthorization.ALLOWED_ALWAYS,
            CBManagerState.POWERED_ON,
            Availability.USABLE,
        ),
        # Measured on macOS 26.6.2 after Don't Allow.
        (
            CBManagerAuthorization.DENIED,
            CBManagerState.UNSUPPORTED,
            Availability.NOT_ALLOWED,
        ),
    ],
)
async def test_the_answer_to_a_permission_dialog_is_reported(
    answer: CBManagerAuthorization,
    state: CBManagerState,
    availability: Availability,
) -> None:
    unasked = CBManagerAuthorization.NOT_DETERMINED
    held = _HeldHelper([(CBManagerState.UNKNOWN, unasked)])

    async with _open_bluetooth(IOBluetoothBackend(held.start)) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.AWAITING_PERMISSION

        # The user answers, and the helper is let go.
        assert held.far is not None
        held.far.send({"event": "state", "state": state, "authorization": answer})
        await held.answer_ready()
        async with asyncio.timeout(1):
            assert await anext(watcher) is availability


async def test_a_refusal_names_what_to_allow() -> None:
    unasked = CBManagerAuthorization.NOT_DETERMINED
    held = _HeldHelper([(CBManagerState.UNKNOWN, unasked)], name="Robot Console")

    async with _open_bluetooth(IOBluetoothBackend(held.start)) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.AWAITING_PERMISSION
        with pytest.raises(BluetoothUnavailableError, match='"Robot Console"'):
            await bluetooth.list_known_devices()

        assert held.far is not None
        held.far.send(
            {
                "event": "state",
                "state": CBManagerState.UNSUPPORTED,
                "authorization": CBManagerAuthorization.DENIED,
            }
        )
        await held.answer_ready()
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.NOT_ALLOWED
        with pytest.raises(BluetoothUnavailableError, match='Allow "Robot Console"'):
            await bluetooth.list_known_devices()


async def test_a_controller_that_never_comes_up_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on GitHub's macOS 15.7.9 and 26.6.2 runners, which have a
    virtual controller: the classic manager stayed unknown for good, and the
    central manager beside it said unsupported.
    """
    framework = fake_framework_parts(monkeypatch, state=CBManagerState.UNKNOWN)
    central = FakeManager.alloc().init()
    central.state = CBManagerState.UNSUPPORTED
    framework.central = central

    async with _stack() as bluetooth:
        async with asyncio.timeout(1):
            availability = await anext(bluetooth.watch_availability())
        assert availability is Availability.UNSUPPORTED


async def test_a_first_helper_that_stops_before_saying_anything_is_an_error() -> None:
    held = _HeldHelper([])

    async def stop_soon() -> None:
        while held.far is None:
            await asyncio.sleep(0)
        held.stop()

    stopping = asyncio.ensure_future(stop_soon())
    try:
        with pytest.raises(HelperFailedError):
            async with _open_bluetooth(IOBluetoothBackend(held.start)):
                pass
    finally:
        await stopping


async def test_a_helper_macos_ends_while_held_is_not_allowed() -> None:
    """
    Measured on macOS 26.6.2: a helper going by a program with no usage
    description reads NOT_DETERMINED, and macOS ends it 15 to 44 ms later
    without asking. Until then it looks the same as one whose dialog is open.
    It would be ended the same way again, so nothing is retried.
    """

    async def refused() -> RfcommError:
        return BluetoothUnavailableError(
            Availability.NOT_ALLOWED, "call prompt_under_name() first"
        )

    unasked = CBManagerAuthorization.NOT_DETERMINED
    held = _HeldHelper([(CBManagerState.UNKNOWN, unasked)], refused)

    async with _open_bluetooth(IOBluetoothBackend(held.start)) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.AWAITING_PERMISSION
        held.stop()
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.NOT_ALLOWED
        with pytest.raises(BluetoothUnavailableError, match="prompt_under_name"):
            await bluetooth.list_known_devices()
        await asyncio.sleep(0.05)
    assert held.starts == 1


async def test_a_helper_that_stops_just_after_it_is_ready_is_replaced() -> None:
    """
    Its last state, its answer to "ready" and its end can be read together.
    It was ready, so it is replaced, like any helper that stops.
    """
    unasked = CBManagerAuthorization.NOT_DETERMINED
    held = _HeldHelper([(CBManagerState.UNKNOWN, unasked)])

    async with _open_bluetooth(IOBluetoothBackend(held.start)) as bluetooth:
        assert await anext(bluetooth.watch_availability()) is (
            Availability.AWAITING_PERMISSION
        )
        far = held.far
        assert far is not None
        async with asyncio.timeout(1):
            frame = await far.receive()
        assert frame is not None
        asked, _fds = frame
        assert asked["op"] == "ready"

        # Queued as the reader queues frames that arrive together, so nothing
        # else runs between them.
        assert held.helper is not None
        arrived = held.helper._link._arrived
        state = {
            "event": "state",
            "state": CBManagerState.POWERED_ON,
            "authorization": CBManagerAuthorization.ALLOWED_ALWAYS,
        }
        arrived.put_nowait((state, []))
        arrived.put_nowait(({"id": asked["id"], "ok": None}, []))
        arrived.put_nowait(None)

        async with asyncio.timeout(1):
            while held.starts < 2:
                await asyncio.sleep(0.01)


async def test_a_held_helper_that_stops_is_not_replaced() -> None:
    """
    It stopped before it was ready, and a new one would do the same.
    """
    unasked = CBManagerAuthorization.NOT_DETERMINED
    held = _HeldHelper([(CBManagerState.UNKNOWN, unasked)])

    async with _open_bluetooth(IOBluetoothBackend(held.start)) as bluetooth:
        watcher = bluetooth.watch_availability()
        assert await anext(watcher) is Availability.AWAITING_PERMISSION
        held.stop()
        async with asyncio.timeout(1):
            assert await anext(watcher) is Availability.SERVICE_UNAVAILABLE
        await asyncio.sleep(0.05)
    assert held.starts == 1


async def test_a_helper_held_by_a_permission_dialog_stops_when_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Seen in pybricks-tools' CI on a GitHub runner: the helper did not exit
    while its dialog was open, and was killed two seconds later.
    """
    framework = fake_framework_parts(
        monkeypatch, authorization=CBManagerAuthorization.NOT_DETERMINED
    )
    dialog = _Dialog(monkeypatch, framework)
    try:
        # Leaving waits at most a second for the helper to stop.
        async with _stack() as bluetooth:
            watcher = bluetooth.watch_availability()
            assert await anext(watcher) is Availability.AWAITING_PERMISSION

        assert dialog.held
        assert threading.current_thread() not in dialog.held
    finally:
        dialog.answered.set()


async def test_a_helper_held_by_a_permission_dialog_goes_on_once_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(
        monkeypatch, authorization=CBManagerAuthorization.NOT_DETERMINED
    )
    dialog = _Dialog(monkeypatch, framework)
    try:
        async with _stack() as bluetooth:
            watcher = bluetooth.watch_availability()
            assert await anext(watcher) is Availability.AWAITING_PERMISSION

            monkeypatch.setattr(
                bindings.CBManager,
                "authorization",
                CBManagerAuthorization.ALLOWED_ALWAYS,
            )
            dialog.answered.set()
            async with asyncio.timeout(1):
                assert await anext(watcher) is Availability.USABLE
                await bluetooth.list_known_devices()

        assert threading.current_thread() not in dialog.held[:1]
    finally:
        dialog.answered.set()


async def _restart_under_the_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Framework, _RestartedDialog, _Helpers]:
    framework = fake_framework_parts(
        monkeypatch,
        state=CBManagerState.UNSUPPORTED,
        authorization=CBManagerAuthorization.NOT_DETERMINED,
    )
    framework.manager.tccApproved = False
    return framework, _RestartedDialog(monkeypatch, framework), _Helpers()


def _answer(
    monkeypatch: pytest.MonkeyPatch,
    framework: Framework,
    dialog: _RestartedDialog,
    authorization: int,
    state: int,
) -> None:
    # A new helper is a new process, with a manager of its own.
    fresh = FakeManager.alloc().init()
    fresh.state = state
    fresh.tccApproved = authorization == CBManagerAuthorization.ALLOWED_ALWAYS
    framework.manager = fresh
    monkeypatch.setattr(bindings.CBManager, "authorization", authorization)
    dialog.answered.set()


async def test_a_bluetoothd_restart_under_a_permission_dialog_waits_for_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework, dialog, helpers = await _restart_under_the_dialog(monkeypatch)
    try:
        async with _stack(helpers) as bluetooth:
            watcher = bluetooth.watch_availability()
            assert await anext(watcher) is Availability.AWAITING_PERMISSION

            dialog.restarted.set()
            async with asyncio.timeout(1):
                while len(helpers.started) < 2:
                    await asyncio.sleep(0.01)
            with pytest.raises(BluetoothUnavailableError) as raised:
                await bluetooth.list_known_devices()
            assert raised.value.reason is Availability.AWAITING_PERMISSION

            _answer(
                monkeypatch,
                framework,
                dialog,
                CBManagerAuthorization.ALLOWED_ALWAYS,
                CBManagerState.POWERED_ON,
            )
            async with asyncio.timeout(1):
                assert await anext(watcher) is Availability.USABLE
                await bluetooth.list_known_devices()
            assert len(helpers.started) == 2
    finally:
        dialog.end()


async def test_a_bluetoothd_restart_under_a_permission_dialog_waits_for_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on macOS 26.6.2: after Don't Allow, the new helper's classic
    manager was not approved, said unsupported, and the authorization denied.
    """
    framework, dialog, helpers = await _restart_under_the_dialog(monkeypatch)
    try:
        async with _stack(helpers) as bluetooth:
            watcher = bluetooth.watch_availability()
            assert await anext(watcher) is Availability.AWAITING_PERMISSION

            dialog.restarted.set()
            async with asyncio.timeout(1):
                while len(helpers.started) < 2:
                    await asyncio.sleep(0.01)

            _answer(
                monkeypatch,
                framework,
                dialog,
                CBManagerAuthorization.DENIED,
                CBManagerState.UNSUPPORTED,
            )
            async with asyncio.timeout(1):
                assert await anext(watcher) is Availability.NOT_ALLOWED
            await asyncio.sleep(0.05)
            assert len(helpers.started) == 2
    finally:
        dialog.end()


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

CHAT = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")


async def _next_request(service: RfcommService) -> RfcommConnectionRequest:
    async with asyncio.timeout(1):
        return await anext(aiter(service.connection_requests()))


async def test_a_peer_that_connects_is_talked_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, bluetooth.serve(CHAT, name="chat") as service:
        fake = framework.channels.connect_in(ADDRESS, service.channel)
        request = await _next_request(service)
        assert request.info.address == ADDRESS

        async with request.accept() as channel:
            fake.arrive(b"hello\n")
            async with asyncio.timeout(1):
                assert await channel.receive() == b"hello\n"
            await channel.send(b"echo\n")
            await asyncio.sleep(0.01)
            assert bytes(fake.written) == b"echo\n"

            fake.hang_up()
            with pytest.raises(ChannelClosedError):
                async with asyncio.timeout(1):
                    await channel.receive()


async def test_a_declined_peer_is_hung_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, bluetooth.serve(CHAT, name="chat") as service:
        fake = framework.channels.connect_in(ADDRESS, service.channel)
        request = await _next_request(service)
        await request.decline()
        async with asyncio.timeout(1):
            while fake.open:
                await asyncio.sleep(0.01)


async def test_each_service_has_a_helper_of_its_own_and_registers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    bluetoothd frees a registration only when the process that made it ends,
    and a second from one process leaks the first's slot -- measured on macOS
    26.6.2.
    """
    framework = fake_framework_parts(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth:
        for _ in range(2):
            async with bluetooth.serve(CHAT, name="chat"):
                pass
        assert len(helpers.started) == 3
        assert len(framework.channels.registrations) == 2
        # Withdrawn with nothing open, each service's helper has gone.
        async with asyncio.timeout(1):
            while not all(h.stopped for h, _far in helpers.started[1:]):
                await asyncio.sleep(0.01)
    assert all(p.withdrawn for p in framework.records.published)


async def test_an_accepted_channel_outlives_the_service_and_its_helper_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    helpers = _Helpers()
    async with _stack(helpers) as bluetooth, AsyncExitStack() as stack:
        async with bluetooth.serve(CHAT, name="chat") as service:
            fake = framework.channels.connect_in(ADDRESS, service.channel)
            request = await _next_request(service)
            channel = await stack.enter_async_context(request.accept())

        serving, _far = helpers.started[-1]
        assert framework.records.published[-1].withdrawn
        await asyncio.sleep(0.05)
        assert not serving.stopped

        fake.arrive(b"still here")
        async with asyncio.timeout(1):
            assert await channel.receive() == b"still here"

        await stack.aclose()
        async with asyncio.timeout(1):
            while not serving.stopped:
                await asyncio.sleep(0.01)
        assert not fake.open


async def test_a_channel_already_served_here_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth:
        with pytest.raises(ChannelInUseError):
            async with bluetooth.serve(CHAT, name="chat", channel=8):
                pass


async def test_a_peer_still_waiting_at_withdrawal_is_hung_up_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The helper lives on for the channel already accepted, so a peer queued
    behind it would otherwise stay connected, unread, for as long as it lasts.
    """
    framework = fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, AsyncExitStack() as stack:
        async with bluetooth.serve(CHAT, name="chat") as service:
            framework.channels.connect_in(ADDRESS, service.channel)
            request = await _next_request(service)
            await stack.enter_async_context(request.accept())
            waiting = framework.channels.connect_in(ADDRESS, service.channel)
            await asyncio.sleep(0.05)

        async with asyncio.timeout(1):
            while waiting.open:
                await asyncio.sleep(0.01)


async def test_a_service_given_up_on_while_its_helper_starts_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Starting can wait on a permission dialog for as long as the user leaves it.
    """
    fake_framework(monkeypatch)
    helpers = _Helpers()
    starting = asyncio.Event()
    abandoned = asyncio.Event()

    @asynccontextmanager
    async def slow_after_first() -> AsyncGenerator[Helper, None]:
        if helpers.started:
            starting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                abandoned.set()
                raise
        async with helpers.start() as helper:
            yield helper

    async with _open_bluetooth(IOBluetoothBackend(slow_after_first)) as bluetooth:
        serving = asyncio.ensure_future(bluetooth.serve(CHAT, name="chat").__aenter__())
        async with asyncio.timeout(1):
            await starting.wait()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        async with asyncio.timeout(1):
            await abandoned.wait()


async def test_two_services_at_once_are_on_channels_of_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    async with (
        _stack() as bluetooth,
        bluetooth.serve(CHAT, name="chat") as one,
        bluetooth.serve(CHAT, name="chat") as other,
    ):
        assert one.channel != other.channel


async def test_two_services_started_together_choose_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Each chooses in a process of its own, where the other's record may not
    be published yet.
    """
    fake_framework_parts(monkeypatch)
    async with _stack() as bluetooth, AsyncExitStack() as stack:
        one, other = await asyncio.gather(
            stack.enter_async_context(bluetooth.serve(CHAT, name="chat")),
            stack.enter_async_context(bluetooth.serve(CHAT, name="chat")),
        )
        assert one.channel != other.channel
