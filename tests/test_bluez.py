# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The parts of the Linux backend that need neither D-Bus nor a radio.

Routing a callback to the right waiter, sharing one profile registration
between concurrent connects, and turning BlueZ's error strings into ours are
all ordinary logic. They are also where the bugs have actually been, so they
are worth pinning down here rather than leaving to a machine with Bluetooth.
"""

import errno
import os
import socket
import uuid
from collections.abc import Awaitable, Callable

import pytest

pytest.importorskip("dbus_fast", reason="the Linux backend needs dbus-fast")

from dbus_fast import DBusError

from aio_rfcomm.availability import Availability
from aio_rfcomm.backend.bluez import BlueZBluetooth, _wanted
from aio_rfcomm.backend.bluez.availability import _Adapter, _powered
from aio_rfcomm.backend.bluez.connection import _Request, _take_connection
from aio_rfcomm.backend.bluez.errors import (
    translate_connect,
    translate_registration,
    translate_socket_connect,
)
from aio_rfcomm.backend.bluez.profile import Profiles, _Profile
from aio_rfcomm.discovery import RfcommDeviceInfo
from aio_rfcomm.errors import (
    ChannelInUseError,
    ConnectionFailedError,
    DeviceNotFoundError,
    ServiceNotFoundError,
    UnsupportedOperationError,
)

DEVICE = "/org/bluez/hci0/dev_00_11_22_33_44_55"
OTHER = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
SERVICE = "539f44f8-e629-4723-bd40-9bd0d2807056"


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def test_no_filter_matches_everything() -> None:
    assert _wanted(None) == set()


def test_one_service_is_lower_cased() -> None:
    assert _wanted(uuid.UUID(SERVICE.upper())) == {SERVICE}


def test_several_services_are_all_accepted() -> None:
    spp = uuid.UUID("00001101-0000-1000-8000-00805f9b34fb")
    assert _wanted([uuid.UUID(SERVICE), spp]) == {SERVICE, str(spp)}


# --------------------------------------------------------------------------
# Error translation
#
# These match on BlueZ's error text, which is the fragile part: a wording
# change upstream turns a precise error into a vague one, and only a test
# says so.
# --------------------------------------------------------------------------


def test_unavailable_profile_means_the_service_is_missing() -> None:
    error = translate_connect(
        DBusError("org.bluez.Error.Failed", "br-connection-profile-unavailable"),
        "00:11",
        SERVICE,
    )
    assert isinstance(error, ServiceNotFoundError)


def test_a_missing_profile_named_only_by_the_error_is_the_service_missing() -> None:
    """
    Measured on BlueZ 5.85: a service missing from the device's stored
    services fails ``ConnectProfile`` with this name and this text.
    """
    error = translate_connect(
        DBusError(
            "org.bluez.Error.BREDR.ProfileUnavailable", "No more profiles to connect to"
        ),
        "00:11",
        SERVICE,
    )
    assert isinstance(error, ServiceNotFoundError)


def test_page_timeout_means_the_device_did_not_answer() -> None:
    error = translate_connect(
        # Measured on BlueZ 5.85, for a paired device that is switched off.
        DBusError("org.bluez.Error.Failed", "br-connection-page-timeout"),
        "00:11",
        SERVICE,
    )
    assert isinstance(error, DeviceNotFoundError)


def test_anything_else_is_a_connection_failure() -> None:
    error = translate_connect(
        DBusError("org.bluez.Error.Failed", "br-connection-busy"), "00:11", SERVICE
    )
    assert isinstance(error, ConnectionFailedError)
    assert "busy" in str(error)


def test_a_uuid_held_elsewhere_says_so() -> None:
    # Measured on BlueZ 5.85.
    error = translate_registration(
        DBusError("org.bluez.Error.NotPermitted", "UUID already registered"), SERVICE
    )
    assert isinstance(error, ConnectionFailedError)
    assert "another program" in str(error)
    assert SERVICE in str(error)


# --------------------------------------------------------------------------
# Hanging up
#
# Measured on BlueZ 5.85 (kernel 7.0.0-38): closing a descriptor BlueZ handed
# over does not hang up, because another copy of it stays open. Each test
# keeps that copy open, so only a real hang-up reaches the peer.
# --------------------------------------------------------------------------


async def _nothing() -> None:
    pass


def _served() -> tuple[socket.socket, socket.socket, int]:
    """
    A connection as BlueZ hands it over.

    Returns:
        The peer's end, the copy BlueZ keeps, and the descriptor handed over.
    """
    ours, peer = socket.socketpair()
    return peer, ours, os.dup(ours.fileno())


def test_declining_hangs_up_on_the_peer() -> None:
    peer, kept, fd = _served()
    with peer, kept:
        _Request(RfcommDeviceInfo("00:11:22:33:44:55"), fd, _nothing).close()

        peer.settimeout(5)
        assert peer.recv(1) == b""


async def test_closing_a_channel_hangs_up_on_the_peer() -> None:
    peer, kept, fd = _served()
    with peer, kept:
        async with _take_connection(fd, _nothing) as channel:
            await channel.send(b"bye")

        peer.settimeout(5)
        assert peer.recv(3) == b"bye"
        assert peer.recv(1) == b""


# --------------------------------------------------------------------------
# Routing callbacks to waiters
# --------------------------------------------------------------------------


async def test_a_connection_goes_to_the_waiter_for_that_device() -> None:
    profile = _Profile()
    waiting = profile.expect(DEVICE)

    profile.NewConnection(DEVICE, 42, {})

    assert await waiting == 42


async def test_connections_are_handed_out_in_order() -> None:
    profile = _Profile()
    first = profile.expect(DEVICE)
    second = profile.expect(DEVICE)

    profile.NewConnection(DEVICE, 1, {})
    profile.NewConnection(DEVICE, 2, {})

    assert await first == 1
    assert await second == 2


async def test_another_device_does_not_steal_the_connection() -> None:
    profile = _Profile()
    waiting = profile.expect(DEVICE)

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    profile.NewConnection(OTHER, read_fd, {})

    assert not waiting.done()


async def test_a_connection_nobody_wants_is_closed_not_leaked() -> None:
    """
    An attempt canceled just before BlueZ answers leaves a descriptor that
    is ours to close. Dropping it silently would leak it.
    """
    profile = _Profile()
    read_fd, write_fd = os.pipe()
    os.close(write_fd)

    profile.NewConnection(DEVICE, read_fd, {})

    with pytest.raises(OSError):
        os.fstat(read_fd)


async def test_forgetting_a_waiter_leaves_the_next_one_alone() -> None:
    profile = _Profile()
    first = profile.expect(DEVICE)
    second = profile.expect(DEVICE)
    profile.forget(DEVICE, first)

    profile.NewConnection(DEVICE, 7, {})

    assert not first.done()
    assert await second == 7


# --------------------------------------------------------------------------
# Sharing one registration
#
# BlueZ permits a UUID to be registered once, system wide, so registering per
# connection attempt fails the moment two run at once. This is the bug these
# tests exist for.
# --------------------------------------------------------------------------


class _FakeBus:
    def __init__(self) -> None:
        self.exported: list[str] = []

    def export(self, path: str, interface: object) -> None:
        self.exported.append(path)

    def unexport(self, path: str) -> None:
        self.exported.remove(path)


class _FakeManager:
    def __init__(self, fails: Exception | None = None) -> None:
        self.registered: list[str] = []
        self.registrations = 0
        self._fails = fails

    async def call_register_profile(
        self, profile: str, uuid: str, options: object
    ) -> None:
        if self._fails is not None:
            raise self._fails
        self.registrations += 1
        self.registered.append(profile)

    async def call_unregister_profile(self, profile: str) -> None:
        self.registered.remove(profile)


def _asking(manager: _FakeManager) -> Callable[[], Awaitable[_FakeManager]]:
    async def get() -> _FakeManager:
        return manager

    return get


def _profiles() -> tuple[Profiles, _FakeBus, _FakeManager]:
    bus, manager = _FakeBus(), _FakeManager()
    return Profiles(bus, _asking(manager)), bus, manager  # type: ignore[arg-type]


async def test_concurrent_users_share_one_registration() -> None:
    profiles, _bus, manager = _profiles()

    # Nested deliberately: two users overlapping is the case that used to
    # fail, so they must not be collapsed into one statement.
    async with profiles.acquire(SERVICE) as first:  # noqa: SIM117
        async with profiles.acquire(SERVICE) as second:
            assert first is second
            assert manager.registrations == 1


async def test_the_registration_outlives_the_first_user() -> None:
    profiles, _bus, manager = _profiles()

    async with profiles.acquire(SERVICE):
        async with profiles.acquire(SERVICE):
            pass
        # the inner user is done, but the outer one still needs it
        assert manager.registered

    assert not manager.registered


async def test_different_services_register_separately() -> None:
    profiles, _bus, manager = _profiles()
    other = "00001101-0000-1000-8000-00805f9b34fb"

    async with profiles.acquire(SERVICE):  # noqa: SIM117
        async with profiles.acquire(other):
            assert manager.registrations == 2


async def test_a_failed_registration_leaves_nothing_exported() -> None:
    bus = _FakeBus()
    manager = _FakeManager(
        fails=DBusError("org.bluez.Error.NotPermitted", "UUID already registered")
    )
    profiles = Profiles(bus, _asking(manager))  # type: ignore[arg-type]

    with pytest.raises(ConnectionFailedError, match="another program"):
        async with profiles.acquire(SERVICE):
            pass

    assert not bus.exported


async def test_a_service_can_be_registered_again_after_release() -> None:
    profiles, _bus, manager = _profiles()

    async with profiles.acquire(SERVICE):
        pass
    async with profiles.acquire(SERVICE):
        assert manager.registrations == 2


# --------------------------------------------------------------------------
# Refusing what the platform cannot do
# --------------------------------------------------------------------------


def _bluetooth(address: str | None = "00:11:22:33:44:55") -> BlueZBluetooth:
    bluetooth = BlueZBluetooth(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    bluetooth.path = "/org/bluez/hci0"
    bluetooth.address = address
    return bluetooth


async def test_channel_numbers_need_bluetooth_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A Python built without Bluetooth support cannot connect to a bare channel
    number, and BlueZ offers no way to do it over D-Bus, so the refusal has to
    explain itself.
    """
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)

    with pytest.raises(UnsupportedOperationError, match="libbluetooth"):
        async with _bluetooth().open_channel(RfcommDeviceInfo("00:11:22:33:44:55"), 1):
            pass


def test_a_refused_channel_means_nothing_is_listening() -> None:
    error = translate_socket_connect(
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        "00:11:22:33:44:55",
        12,
    )
    assert isinstance(error, ServiceNotFoundError)


def test_a_device_that_is_not_there_is_not_found() -> None:
    error = translate_socket_connect(
        OSError(errno.EHOSTDOWN, "Host is down"), "00:11:22:33:44:55", 12
    )
    assert isinstance(error, DeviceNotFoundError)


def test_other_socket_failures_are_failed_connections() -> None:
    error = translate_socket_connect(
        OSError(errno.EIO, "Input/output error"), "00:11:22:33:44:55", 12
    )
    assert isinstance(error, ConnectionFailedError)


# --------------------------------------------------------------------------
# Choosing a channel to serve on
# --------------------------------------------------------------------------


def test_choosing_a_channel_needs_a_bluetooth_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    BlueZ neither picks a channel for a server nor says which are free, and a
    registration that lands on a taken one is published as nothing at all. So
    with no socket to probe with, refusing beats guessing: a wrong guess would
    hand back a service that never appears.
    """
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)
    with pytest.raises(UnsupportedOperationError, match="libbluetooth"):
        _bluetooth()._reserve_channel(None)


def test_an_asked_for_channel_is_kept_when_it_cannot_be_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)
    assert _bluetooth()._reserve_channel(7) == 7


def test_an_unprobed_channel_this_process_serves_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)
    bluetooth = _bluetooth()
    bluetooth._claimed.add(9)
    with pytest.raises(ChannelInUseError, match="channel 9"):
        bluetooth._reserve_channel(9)


def test_an_adapter_with_no_address_cannot_probe_either() -> None:
    """
    The probe binds the adapter's own address, because binding BDADDR_ANY
    succeeds on a channel a profile already holds and so would report every
    channel free. With no address to bind, there is nothing to probe with.
    """
    with pytest.raises(UnsupportedOperationError, match="libbluetooth"):
        _bluetooth(address=None)._reserve_channel(None)


# --------------------------------------------------------------------------
# Whether the radio is usable
# --------------------------------------------------------------------------


def _adapter_with(powered: bool, power_state: str | None) -> _Adapter:
    return _Adapter(
        "/org/bluez/hci0", "00:1A:7D:DA:71:0C", "freyr", powered, power_state
    )


@pytest.mark.parametrize(
    ("powered", "power_state", "availability"),
    [
        (True, "on", Availability.USABLE),
        # Measured on BlueZ 5.85: switching off reads like this for about 50
        # ms, while the radio's links are already dropping.
        (True, "on-disabling", Availability.SWITCHED_OFF),
        (False, "off", Availability.SWITCHED_OFF),
        (False, "off-enabling", Availability.SWITCHED_OFF),
        (False, "off-blocked", Availability.SWITCHED_OFF),
        # A BlueZ without PowerState has only Powered to go on.
        (True, None, Availability.USABLE),
        (False, None, Availability.SWITCHED_OFF),
    ],
)
def test_only_a_radio_that_is_on_is_usable(
    powered: bool, power_state: str | None, availability: Availability
) -> None:
    assert _powered(_adapter_with(powered, power_state))[0] is availability


def test_a_blocked_radio_says_how_to_unblock_it() -> None:
    _, detail = _powered(_adapter_with(False, "off-blocked"))
    assert detail is not None
    assert "rfkill" in detail
