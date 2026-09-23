# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
A two-way chat over RFCOMM, as a worked example of the whole library.

Run it on one machine as a server and on another as a client::

    python examples/chat.py serve
    python examples/chat.py connect AA:BB:CC:DD:EE:FF

Serving needs Linux or Windows; macOS can only connect (see the README).
Both ends print what the other types, and Ctrl-D or Ctrl-C hangs up.

The parts worth copying are the shape of the two ``async with`` blocks, the
line splitting -- RFCOMM is a byte stream and does not keep message
boundaries -- and how each side waits: on an event nothing sets, rather than
in a sleeping loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import AsyncGenerator, Iterator
from uuid import UUID

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

import aio_rfcomm
import aio_rfcomm.macos
from aio_rfcomm.errors import ChannelClosedError, CloseReason, RfcommError

# Any UUID both ends agree on. Generate your own for a real application with
# "python -m uuid", or uuid.uuid4() on Python 3.11, which has no such module.
CHAT = UUID("c0ffee00-1dea-4b1d-9f00-a100c0ffee01")

SERVICE_NAME = "aio-rfcomm chat"


# --------------------------------------------------------------------------
# Talking
# --------------------------------------------------------------------------


def split_lines(buffer: bytearray, arrived: bytes) -> Iterator[str]:
    """
    Take whole lines out of a growing buffer.

    RFCOMM is a byte stream: a peer's single ``send`` may arrive split across
    several ``receive`` calls, or share one with the next message. Anything
    message-shaped has to be framed by the application, and a newline is the
    cheapest framing there is.

    Args:
        buffer: Holds the bytes left over from last time. Modified in place.
        arrived: What just came in.

    Yields:
        Each complete line, decoded, without its newline.
    """
    buffer += arrived
    while (end := buffer.find(b"\n")) >= 0:
        line = bytes(buffer[:end])
        del buffer[: end + 1]
        yield line.decode("utf-8", errors="replace").rstrip("\r")


async def show(channel: aio_rfcomm.RfcommChannel, who: str) -> None:
    """
    Print what the peer says until it stops saying anything.

    Args:
        channel: The open channel.
        who: What to label the peer's lines with.
    """
    buffer = bytearray()
    while True:
        arrived = await channel.receive()
        if not arrived:
            return
        for line in split_lines(buffer, arrived):
            print(f"{who}: {line}")


async def send_typing(channel: aio_rfcomm.RfcommChannel) -> None:
    """
    Send whatever is typed, a line at a time.

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


async def converse(channel: aio_rfcomm.RfcommChannel, who: str) -> None:
    """
    Talk until either end hangs up.

    Reading and typing run as two tasks in one group, so whichever finishes
    first takes the other down with it -- and ``fail_when_gone`` stops both
    if the channel goes away while the typing task sits waiting for a key it
    will never get.

    Args:
        channel: The open channel.
        who: What to label the peer's lines with.
    """
    print(f"connected to {who}. Ctrl-D to hang up.")
    try:
        with patch_stdout():
            async with channel.fail_when_gone(), asyncio.TaskGroup() as group:
                group.create_task(show(channel, who))
                group.create_task(send_typing(channel))
    except ChannelClosedError as error:
        # Not a group: fail_when_gone reports its task group's lone failure as
        # itself, so callers never have to write except* for one error.
        _report(error)


def _report(error: BaseException) -> None:
    """
    Say how the conversation ended.

    Args:
        error: The channel error that ended it.
    """
    reason = getattr(error, "reason", None)
    if reason is CloseReason.PEER_CLOSED:
        print("the other end hung up")
    else:
        print(f"lost the channel: {error}")


# --------------------------------------------------------------------------
# The two ends
# --------------------------------------------------------------------------


async def serve(adapter: aio_rfcomm.RfcommAdapter, channel: int | None) -> None:
    """
    Publish the chat service and talk to whoever connects.

    Args:
        adapter: The open adapter.
        channel: The RFCOMM channel to listen on, or ``None`` to be given one.
    """

    async def handle(
        peer: aio_rfcomm.RfcommDeviceInfo, chan: aio_rfcomm.RfcommChannel
    ) -> None:
        await converse(chan, peer.name or peer.address)

    async with adapter.serve(
        CHAT, handle, name=SERVICE_NAME, channel=channel
    ) as service:
        print(f"serving {SERVICE_NAME} on channel {service.channel}")
        print("waiting for someone to connect; Ctrl-C to stop")
        # Waiting on an event nothing ever sets costs nothing, and is how to
        # wait for something that will not happen. A loop that sleeps and
        # checks would burn wakeups to achieve the same nothing.
        async with service.fail_when_gone():
            await asyncio.Event().wait()


async def connect(
    adapter: aio_rfcomm.RfcommAdapter, address: str, channel: int | None
) -> None:
    """
    Open a channel to someone else's chat service and talk to it.

    Args:
        adapter: The open adapter.
        address: The peer's Bluetooth address.
        channel: The channel to use, or ``None`` to look the service up.
    """
    device = adapter.use_device(address)
    connection = (
        device.open_channel(channel)
        if channel is not None
        else device.open_service(CHAT)
    )
    async with connection as chan:
        await converse(chan, device.name or device.address)


async def show_devices(adapter: aio_rfcomm.RfcommAdapter) -> None:
    """
    List the devices this machine knows that offer the chat service.

    Args:
        adapter: The open adapter.
    """
    known = await adapter.list_known_devices(service=CHAT)
    if not known:
        print("no known device offers the chat service.")
        print("pair the other machine first, and start its server.")
        return
    for device in known:
        print(f"{device.address}  {device.name or ''}")


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def opened() -> AsyncGenerator[aio_rfcomm.RfcommAdapter, None]:
    """
    Open the default adapter, having asked macOS for permission first.

    Yields:
        The open adapter.
    """
    # On macOS this is the name the Bluetooth permission dialog appears
    # under, and the name the answer is remembered against -- so changing it
    # asks the user again. A library cannot pick a name on its caller's
    # behalf, which is why a program that is not a bundled application gives
    # its own here. Everywhere else the call does nothing.
    aio_rfcomm.macos.prompt_under_own_name(
        name="aio-rfcomm chat", reason="to chat over Bluetooth"
    )
    async with aio_rfcomm.open_adapter() as adapter:
        yield adapter


class Arguments(argparse.Namespace):
    """
    What the command line can say, with types on it.

    ``argparse.Namespace`` is otherwise an untyped bag of attributes, and a
    typo in one of these names would go unnoticed until it ran.
    """

    command: str
    channel: int | None
    address: str


async def main(arguments: Arguments) -> int:
    """
    Run whichever end was asked for.

    Args:
        arguments: The parsed command line.

    Returns:
        The process exit status.
    """
    try:
        async with opened() as adapter:
            if arguments.command == "list":
                await show_devices(adapter)
            elif arguments.command == "serve":
                await serve(adapter, arguments.channel)
            else:
                await connect(adapter, arguments.address, arguments.channel)
    except RfcommError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


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
        help="RFCOMM channel to listen on (1-30); chosen for you if omitted",
    )

    connecting = commands.add_parser("connect", help="connect to someone serving")
    connecting.add_argument("address", help="the peer's Bluetooth address")
    connecting.add_argument(
        "--channel",
        type=int,
        default=None,
        help=(
            "skip service discovery and use this channel. Worth having on "
            "macOS, which will not re-read a paired device's services"
        ),
    )
    return parser.parse_args(namespace=Arguments())


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main(parse())))
