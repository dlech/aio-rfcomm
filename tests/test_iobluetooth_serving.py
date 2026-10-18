# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Tests for publishing a service in the macOS native layer, with the
framework stood in for.
"""

import asyncio
import sys
from typing import TYPE_CHECKING
from uuid import UUID

assert not TYPE_CHECKING or sys.platform == "darwin"

import pytest

if sys.platform != "darwin":
    pytest.skip("IOBluetooth is macOS only", allow_module_level=True)


from aio_rfcomm.backend.iobluetooth.codes import CBManagerState
from aio_rfcomm.backend.iobluetooth.helper import native
from aio_rfcomm.errors import (
    ChannelInUseError,
)
from tests.fake_iobluetooth import (
    ADDRESS,
    FakeLocalRecord,
    change_state,
    # An autouse fixture, used by being imported.
    clean_state,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    fake_framework_parts,
    until_observed,
)

# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------

SERVICE = UUID("00eeffc0-1dea-4b1d-9f00-a100c0ffee03")


async def test_a_service_is_published_on_the_channel_it_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with native.serve(SERVICE, name="chat", channel=None) as service:
        [published] = framework.records.published
        record = published.record
        assert record["0100"] == "chat"
        rfcomm = record["0004"][1]
        assert rfcomm[1]["DataElementValue"] == service.channel
    assert published.withdrawn


async def test_one_registration_is_made_and_it_reaches_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on macOS 26.6.2: given a string, the selector stored is its first
    letter, and every channel is silently let go -- so the registration has to
    be one the listener really answers.
    """
    framework = fake_framework_parts(monkeypatch)
    async with native.serve(SERVICE, name="chat", channel=None) as service:
        fake = framework.channels.connect_in(ADDRESS, service.channel)
        async with asyncio.timeout(1):
            channel = await service.accept()
        try:
            assert channel.peer().address == ADDRESS
            fake.arrive(b"hello")
            async with asyncio.timeout(1):
                assert await channel.arrived.get() == b"hello"
            await channel.send(b"echo")
            assert bytes(fake.written) == b"echo"
        finally:
            await channel.close()
    assert len(framework.channels.registrations) == 1
    assert not fake.open


async def test_registering_waits_until_the_manager_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Measured on macOS 26.6.2: made before the manager is ready, a registration
    is never sent.
    """
    framework = fake_framework_parts(monkeypatch, state=CBManagerState.UNKNOWN)
    serving = asyncio.ensure_future(native.serve(SERVICE, name="chat").__aenter__())
    await until_observed(framework.manager)
    assert not serving.done()
    change_state(framework.manager, CBManagerState.POWERED_ON)
    async with asyncio.timeout(1):
        await serving
    assert len(framework.channels.registrations) == 1


@pytest.mark.parametrize(
    ("in_use", "chosen"),
    [
        ([1, 8], 30),
        ([1, 8, 30], 29),
        # Never 15 or 10, which bluetoothd on macOS 26.6.2 does not register
        # with its stack.
        ([*range(16, 31), 11, 12, 13, 14], 9),
    ],
)
async def test_a_channel_is_chosen_away_from_this_mac_s_own(
    monkeypatch: pytest.MonkeyPatch, in_use: list[int], chosen: int
) -> None:
    framework = fake_framework_parts(monkeypatch)
    framework.devices.shared.records = [FakeLocalRecord(n) for n in in_use]
    async with native.serve(SERVICE, name="chat") as service:
        assert service.channel == chosen


async def test_a_channel_this_mac_already_serves_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    with pytest.raises(ChannelInUseError, match="8"):
        async with native.serve(SERVICE, name="chat", channel=8):
            pass


async def test_a_withdrawn_service_lets_new_channels_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    On macOS 26.6.2, unregistering only stops this process listening.
    bluetoothd still takes the channel, and the framework, with nobody to give
    it to, closes it.
    """
    framework = fake_framework_parts(monkeypatch)
    async with native.serve(SERVICE, name="chat") as service:
        number = service.channel
    late = framework.channels.connect_in(ADDRESS, number)
    assert not late.open


async def test_channels_nobody_took_are_closed_on_withdrawal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework = fake_framework_parts(monkeypatch)
    async with native.serve(SERVICE, name="chat") as service:
        waiting = framework.channels.connect_in(ADDRESS, service.channel)
        await asyncio.sleep(0)
    assert not waiting.open


async def test_a_channel_served_before_is_never_chosen_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Its record cannot be withdrawn, and a later registration on it may take a
    connection bluetoothd kept from before instead of listening -- measured on
    macOS 26.6.2.
    """
    framework = fake_framework_parts(monkeypatch)
    async with native.serve(SERVICE, name="chat") as first:
        number = first.channel
    async with native.serve(SERVICE, name="chat") as again:
        assert again.channel != number
    with pytest.raises(ChannelInUseError, match="switched off and on"):
        async with native.serve(SERVICE, name="chat", channel=number):
            pass
    assert len(framework.records.published) == 2


async def test_channels_another_service_here_holds_are_avoided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_framework_parts(monkeypatch)
    async with native.serve(SERVICE, name="chat", avoiding=(30, 29)) as service:
        assert service.channel == 28
