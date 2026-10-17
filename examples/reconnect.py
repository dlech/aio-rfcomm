# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Stay connected to a chat server through whatever happens to Bluetooth.

Run the other end with ``python examples/chat.py serve``, then::

    python examples/reconnect.py AA:BB:CC:DD:EE:FF

Switch Bluetooth off and on, or restart the Bluetooth service: it says what
it is waiting for, and connects again by itself once it can. When the
connection ends, it asks before connecting again: a server that hung up or
turned it away would only turn it away again.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from typing import NoReturn, assert_never
from uuid import UUID

from prompt_toolkit import PromptSession

import aio_rfcomm
import aio_rfcomm.macos
from aio_rfcomm import Availability, UnavailableReason
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
)

# The chat example's service.
CHAT = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")


def explain(reason: UnavailableReason) -> str:
    """
    Say what the user can do about Bluetooth being unusable.

    Args:
        reason: Why Bluetooth cannot be used.

    Returns:
        What to tell the user.
    """
    # A match rather than a dict, so that a new reason is a type error here.
    match reason:
        case Availability.SWITCHED_OFF:
            return "Bluetooth is switched off. Turn it on to connect"
        case Availability.NO_ADAPTER:
            return "this computer has no Bluetooth adapter. Plug one in"
        case Availability.NOT_ALLOWED:
            return "this program may not use Bluetooth. Allow it in settings"
        case Availability.SERVICE_UNAVAILABLE:
            return "the Bluetooth service is restarting. Waiting for it"
        case Availability.UNSUPPORTED:
            return "this system has no Bluetooth"
        case _:
            assert_never(reason)


async def wait_until_usable(bluetooth: aio_rfcomm.Bluetooth) -> bool:
    """
    Wait for Bluetooth to be usable, saying what is being waited for.

    Leaving the loop early is safe: the iterator holds nothing that needs
    closing.

    Args:
        bluetooth: The open Bluetooth object.

    Returns:
        True once Bluetooth is usable. False if it never will be, since
        nobody can do anything about a system with no Bluetooth.
    """
    async for availability in bluetooth.watch_availability():
        match availability:
            case Availability.USABLE:
                return True
            case Availability.UNSUPPORTED:
                print(f"[bluetooth] {explain(availability)}")
                return False
            case _:
                print(f"[bluetooth] {explain(availability)}")

    assert False, "watch_availability() does not end by itself"


async def talk(channel: aio_rfcomm.RfcommChannel) -> NoReturn:
    """
    Print whatever the server says, until the channel ends.

    Never returns: every ending, the server hanging up included, is raised as
    ChannelClosedError, so the caller handles them all in one place.

    Args:
        channel: The open channel.
    """
    while True:
        arrived = await channel.receive()
        print(f"[chat] {arrived.decode(errors='replace').rstrip()}")


async def wait_for_retry() -> bool:
    """
    Let the user say when to try again.

    prompt_async can be canceled, unlike a blocking read handed to a thread,
    which would keep the process alive until someone pressed Enter.

    Returns:
        True to try again. False if the user would rather stop.
    """
    try:
        await PromptSession[str]().prompt_async("press Enter to try again ")
    except (EOFError, KeyboardInterrupt):
        return False

    return True


async def stay_connected(
    bluetooth: aio_rfcomm.Bluetooth, server: aio_rfcomm.RfcommDeviceInfo
) -> int:
    """
    Connect, talk, and connect again after the connection ends.

    Args:
        bluetooth: The open Bluetooth object.
        server: The chat server.

    Returns:
        The exit status: 0 if the user stopped it, 1 once Bluetooth will
        never be usable here.
    """
    while await wait_until_usable(bluetooth):
        try:
            async with bluetooth.open_service(server, CHAT) as channel:
                print(f"[chat] connected to {server.name or server.address}")
                await talk(channel)
        except ChannelClosedError:
            print("[chat] the connection ended")

            if not await wait_for_retry():
                return 0
        except BluetoothUnavailableError:
            # Whether it went before the open or mid-conversation, the wait
            # at the top says why, for as long as it lasts.
            print("[chat] Bluetooth went away")
        except (DeviceNotFoundError, ServiceNotFoundError, ConnectionFailedError):
            # Bluetooth is fine, but the server is not there, not serving, or
            # would not open a channel.
            print("[chat] could not reach the server")

            if not await wait_for_retry():
                return 0

    return 1


async def main(server: aio_rfcomm.RfcommDeviceInfo) -> int:
    """
    Keep the connection going until interrupted.

    Args:
        server: The chat server.

    Returns:
        The process exit status.
    """
    aio_rfcomm.macos.prompt_under_name(
        name="aio-rfcomm reconnect", reason="to stay connected over Bluetooth"
    )

    try:
        # Opens whatever Bluetooth is like right now, usable or not. The
        # wait in stay_connected is what reports which.
        async with aio_rfcomm.open_bluetooth() as bluetooth:
            return await stay_connected(bluetooth, server)
    except RfcommError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Stay connected to a chat server over RFCOMM."
    )
    # A type that raises ValueError gets argparse's own error message.
    parser.add_argument(
        "address",
        type=aio_rfcomm.RfcommDeviceInfo,
        help="the chat server's Bluetooth address",
    )
    server: aio_rfcomm.RfcommDeviceInfo = parser.parse_args().address

    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main(server)))
