# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
A two-way chat over RFCOMM, as a worked example of the whole library.

Run it on one machine as a server and on another as a client::

    python examples/chat.py serve
    python examples/chat.py connect AA:BB:CC:DD:EE:FF

Both ends print what the other types, and Ctrl-D or Ctrl-C hangs up. Ctrl-C
again stops the server. The server talks to one peer at a time, turning away
anyone else who connects meanwhile, and keeps serving through Bluetooth going
off and on.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Generator
from typing import Literal, NoReturn, assert_never
from uuid import UUID

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

import aio_rfcomm
import aio_rfcomm.macos
from aio_rfcomm import Availability, UnavailableReason
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    DelimiterNotFoundError,
    RfcommError,
)

# Any UUID both ends agree on. Generate your own for a real application with
# "python -m uuid", or uuid.uuid4() on Python 3.11, which has no such module.
CHAT = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")

SERVICE_NAME = "aio-rfcomm chat"

# The longest line shown whole.
MAX_LINE = 4096


# --------------------------------------------------------------------------
# Bluetooth
# --------------------------------------------------------------------------


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
            return "Bluetooth is switched off. Turn it on"
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
                print(explain(availability))
                return False
            case _:
                print(explain(availability))

    assert False, "watch_availability() does not end by itself"


# --------------------------------------------------------------------------
# Talking
# --------------------------------------------------------------------------


async def show(channel: aio_rfcomm.RfcommChannel, who: str) -> NoReturn:
    """
    Print what the peer says, a line at a time, until the channel ends.

    Args:
        channel: The open channel.
        who: What to label the peer's lines with.
    """
    while True:
        try:
            line = await channel.receive_until(b"\n", max_bytes=MAX_LINE + 1)
        except DelimiterNotFoundError:
            # An overlong line is shown in pieces rather than dropped.
            line = await channel.receive(MAX_LINE)

        text = line.decode("utf-8", errors="replace").rstrip("\r")
        print(f"{who}: {text}")


async def send_typing(channel: aio_rfcomm.RfcommChannel) -> None:
    """
    Send whatever is typed, a line at a time, until the user hangs up.

    Args:
        channel: The open channel.
    """
    session: PromptSession[str] = PromptSession("> ")

    while True:
        try:
            line = await session.prompt_async()
        except (EOFError, KeyboardInterrupt):
            return

        await channel.send(line.encode() + b"\n")


@contextlib.contextmanager
def unwrap_ending() -> Generator[None, None, None]:
    """
    Raise a channel's ending plainly from a task group that shares it.

    Every task using a channel sees the same ending, so a group made only of
    endings holds one fact, possibly several times over. Anything else in the
    group is a bug and goes on up untouched.
    """
    try:
        yield
    except ExceptionGroup as failed:
        match failed.split((ChannelClosedError, BluetoothUnavailableError)):
            case (ExceptionGroup() as endings, None):
                # split() keeps any nesting, so dig down to an exception.
                ending = endings.exceptions[0]

                while isinstance(ending, ExceptionGroup):
                    ending = ending.exceptions[0]

                raise ending from None
            case _:
                raise


async def converse(channel: aio_rfcomm.RfcommChannel, who: str) -> None:
    """
    Talk until the user hangs up, or raise however the channel ended.

    Reading runs as a task beside the typing. Any ending is raised by its
    receive(), which also cancels the typing. The user hanging up returns
    from the typing, which then stops the reading. The task group is only how
    this function does two things at once, so its callers see the ending
    itself, not a group.

    Args:
        channel: The open channel.
        who: What to label the peer's lines with.
    """
    print(f"connected to {who}. Ctrl-D or Ctrl-C to hang up.")

    with patch_stdout(), unwrap_ending():
        async with asyncio.TaskGroup() as group:
            reading = group.create_task(show(channel, who))
            await send_typing(channel)
            reading.cancel()


# --------------------------------------------------------------------------
# The two ends
# --------------------------------------------------------------------------


async def answer(request: aio_rfcomm.RfcommConnectionRequest) -> None:
    """
    Accept one connection and talk over it.

    A conversation ending is not the server's problem, so it is reported here
    and goes no further. Bluetooth going away is, so that goes on up.

    Args:
        request: Someone asking to connect.
    """
    try:
        async with request.accept() as channel:
            await converse(channel, request.info.name or request.info.address)
    except ChannelClosedError:
        print("the connection ended")


async def serve(bluetooth: aio_rfcomm.Bluetooth, channel: int | None) -> int:
    """
    Publish the chat service and talk to whoever connects, one at a time,
    for as long as Bluetooth is usable, and again each time it comes back.

    Only one conversation can have the terminal, so anyone who connects
    during one is turned away rather than left waiting on a channel that
    nobody answers.

    Args:
        bluetooth: The open Bluetooth object.
        channel: The RFCOMM channel to listen on, or ``None`` to be given one.

    Returns:
        The exit status, once Bluetooth will never be usable here. Until then
        it serves.
    """
    while await wait_until_usable(bluetooth):
        try:
            async with bluetooth.serve(
                CHAT, name=SERVICE_NAME, channel=channel
            ) as service:
                print(f"serving {SERVICE_NAME} on channel {service.channel}")

                # The conversation runs as a task so that requests keep
                # arriving during it. When Bluetooth goes, the requests and
                # the conversation raise the same ending.
                with unwrap_ending():
                    async with asyncio.TaskGroup() as group:
                        talking: asyncio.Task[None] | None = None

                        async for request in service.connection_requests():
                            if not talking or talking.done():
                                talking = group.create_task(answer(request))
                            else:
                                who = request.info.name or request.info.address
                                print(f"turned away {who}: already talking")
                                await request.decline()
        except BluetoothUnavailableError:
            # The wait at the top says why, for as long as it lasts.
            print("stopped serving: Bluetooth went away")

    return 1


async def connect(
    bluetooth: aio_rfcomm.Bluetooth,
    server: aio_rfcomm.RfcommDeviceInfo,
    channel: int | None,
) -> int:
    """
    Open a channel to someone else's chat service and talk to it.

    Args:
        bluetooth: The open Bluetooth object.
        server: The machine serving the chat.
        channel: The channel to use, or ``None`` to look the service up.

    Returns:
        The exit status: 1 if Bluetooth could not be used or went away, else 0.
    """
    if not await wait_until_usable(bluetooth):
        return 1

    connection = (
        bluetooth.open_service(server, CHAT)
        if channel is None
        else bluetooth.open_channel(server, channel)
    )

    try:
        async with connection as chan:
            await converse(chan, server.name or server.address)
    except ChannelClosedError:
        print("the connection ended")
    except BluetoothUnavailableError as gone:
        print(explain(gone.reason))
        return 1

    return 0


async def list_devices(bluetooth: aio_rfcomm.Bluetooth) -> int:
    """
    List the devices this machine knows that offer the chat service.

    Args:
        bluetooth: The open Bluetooth object.

    Returns:
        The exit status: 1 if Bluetooth could not be used, else 0.
    """
    if not await wait_until_usable(bluetooth):
        return 1

    known = await bluetooth.list_known_devices(service=CHAT)

    if not known:
        print("no known device offers the chat service.")
        print("pair the other machine first, and start its server.")

    for device in known:
        print(f"{device.address}  {device.name or ''}")

    return 0


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


class Arguments(argparse.Namespace):
    """
    What the command line can say, with types on it.
    """

    command: Literal["list", "serve", "connect"]
    channel: int | None
    address: aio_rfcomm.RfcommDeviceInfo


async def main(arguments: Arguments) -> int:
    """
    Run whichever end was asked for.

    Args:
        arguments: The parsed command line.

    Returns:
        The process exit status.
    """
    # On macOS this is the name the Bluetooth permission dialog appears
    # under, and the name the answer is remembered against. Everywhere else
    # the call does nothing.
    aio_rfcomm.macos.prompt_under_name(
        name="aio-rfcomm chat", reason="to chat over Bluetooth"
    )

    try:
        async with aio_rfcomm.open_bluetooth() as bluetooth:
            match arguments.command:
                case "list":
                    return await list_devices(bluetooth)
                case "serve":
                    return await serve(bluetooth, arguments.channel)
                case "connect":
                    return await connect(
                        bluetooth, arguments.address, arguments.channel
                    )
                case _:
                    assert_never(arguments.command)
    except RfcommError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


def parse() -> Arguments:
    """
    Read the command line.

    Returns:
        What was asked for.
    """
    parser = argparse.ArgumentParser(description="A two-way chat over RFCOMM.")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="show devices offering the service")
    listing.set_defaults(channel=None)

    serving = commands.add_parser("serve", help="wait for someone to connect")
    serving.add_argument(
        "--channel",
        type=int,
        default=None,
        help="RFCOMM channel to listen on (1-30). Chosen for you if omitted",
    )

    connecting = commands.add_parser("connect", help="connect to someone serving")
    # A type that raises ValueError gets argparse's own error message.
    connecting.add_argument(
        "address",
        type=aio_rfcomm.RfcommDeviceInfo,
        help="the peer's Bluetooth address",
    )
    connecting.add_argument(
        "--channel",
        type=int,
        default=None,
        help="skip service discovery and use this channel",
    )

    return parser.parse_args(namespace=Arguments())


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main(parse())))
