# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The parts of the Linux backend that need neither D-Bus nor a radio.

Routing a callback to the right waiter, sharing one profile registration
between concurrent connects, and turning BlueZ's error strings into ours are
all ordinary logic. They are also where the bugs have actually been, so they
are worth pinning down here rather than leaving to a machine with Bluetooth.
"""

import os
import socket
import uuid

import pytest

pytest.importorskip("dbus_fast", reason="the Linux backend needs dbus-fast")

from aio_rfcomm.backend.bluez import (
    BlueZAdapter,
    _Profile,
    _Profiles,
    _translate_connect,
    _translate_registration,
    _wanted,
)
from aio_rfcomm.discovery import RfcommAdapterInfo
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
    error = _translate_connect(
        Exception("br-connection-profile-unavailable"), "00:11", SERVICE
    )
    assert isinstance(error, ServiceNotFoundError)


def test_page_timeout_means_the_device_did_not_answer() -> None:
    error = _translate_connect(
        Exception("br-connection-page-timeout"), "00:11", SERVICE
    )
    assert isinstance(error, DeviceNotFoundError)


def test_anything_else_is_a_connection_failure() -> None:
    error = _translate_connect(Exception("br-connection-busy"), "00:11", SERVICE)
    assert isinstance(error, ConnectionFailedError)
    assert "busy" in str(error)


def test_a_uuid_held_elsewhere_says_so() -> None:
    error = _translate_registration(Exception("UUID already registered"), SERVICE)
    assert isinstance(error, ConnectionFailedError)
    assert "another program" in str(error)
    assert SERVICE in str(error)


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
    An attempt cancelled just before BlueZ answers leaves a descriptor that
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


def _profiles() -> tuple[_Profiles, _FakeBus, _FakeManager]:
    bus, manager = _FakeBus(), _FakeManager()
    return _Profiles(bus, manager), bus, manager  # type: ignore[arg-type]


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
    manager = _FakeManager(fails=Exception("UUID already registered"))
    profiles = _Profiles(bus, manager)  # type: ignore[arg-type]

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


async def test_channel_numbers_need_bluetooth_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A Python built without Bluetooth support cannot connect to a bare channel
    number, and BlueZ offers no way to do it over D-Bus, so the refusal has to
    explain itself.
    """
    from aio_rfcomm.backend.bluez import BlueZAdapter
    from aio_rfcomm.discovery import RfcommAdapterInfo
    from aio_rfcomm.errors import UnsupportedOperationError

    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)
    adapter = BlueZAdapter(
        None,  # type: ignore[arg-type]
        RfcommAdapterInfo("/org/bluez/hci0", "00:11:22:33:44:55", "test"),
        None,  # type: ignore[arg-type]
    )

    with pytest.raises(UnsupportedOperationError, match="libbluetooth"):
        async with adapter.open_channel("00:11:22:33:44:55", 1):
            pass


# --------------------------------------------------------------------------
# Choosing a channel to serve on
# --------------------------------------------------------------------------


def _adapter(address: str | None = "00:11:22:33:44:55") -> BlueZAdapter:
    return BlueZAdapter(
        None,  # type: ignore[arg-type]
        RfcommAdapterInfo("/org/bluez/hci0", address, "test"),
        None,  # type: ignore[arg-type]
    )


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
        _adapter()._reserve_channel(None)


def test_an_asked_for_channel_is_kept_when_it_cannot_be_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)
    assert _adapter()._reserve_channel(7) == 7


def test_an_unprobed_channel_this_process_serves_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(socket, "AF_BLUETOOTH", raising=False)
    adapter = _adapter()
    adapter._claimed.add(9)
    with pytest.raises(ChannelInUseError, match="channel 9"):
        adapter._reserve_channel(9)


def test_an_adapter_with_no_address_cannot_probe_either() -> None:
    """
    The probe binds the adapter's own address, because binding BDADDR_ANY
    succeeds on a channel a profile already holds and so would report every
    channel free. With no address to bind, there is nothing to probe with.
    """
    with pytest.raises(UnsupportedOperationError, match="libbluetooth"):
        _adapter(address=None)._reserve_channel(None)
