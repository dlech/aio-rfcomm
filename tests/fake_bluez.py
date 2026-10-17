# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
A stand-in for bluetoothd's side of the system bus, and nothing above it.

It runs on a private ``dbus-daemon``, so the backend talks real D-Bus through
real dbus-fast. Name ownership, match rules, signals and file descriptor
passing are all the bus's own. Only the objects BlueZ exports are stood in
for, and what was measured is marked so. The connections handed over are
socket pairs, since CI has no radio. The far end of each stands for the peer,
and a copy of our end is kept open, as bluetoothd keeps one (BlueZ 5.85,
kernel 7.0.0-38).
"""

# Do not add ``from __future__ import annotations`` to this module. dbus-fast
# reads the annotations on D-Bus methods and properties at runtime.

import shutil
import socket
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest
from dbus_fast import DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import (
    DBusBool,
    DBusDict,
    DBusObjectPath,
    DBusSignature,
    DBusStr,
)
from dbus_fast.constants import PropertyAccess
from dbus_fast.introspection import Node
from dbus_fast.service import ServiceInterface, dbus_property, method

DBusStrings = Annotated[list[str], DBusSignature("as")]

# Anyone may own any name and send anything, which is all a test needs.
_CONFIG = """\
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <listen>unix:dir={directory}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@contextmanager
def private_bus(directory: Path) -> Generator[str, None, None]:
    """
    Run a bus of our own for the length of a block.

    Args:
        directory: Where its socket goes.

    Yields:
        The bus's address.
    """
    daemon = shutil.which("dbus-daemon")

    if daemon is None:
        # Every Linux with BlueZ has one, so there it is a broken setup.
        if sys.platform == "linux":
            pytest.fail("the BlueZ tests need dbus-daemon")
        pytest.skip("the BlueZ tests need dbus-daemon")

    config = directory / "bus.conf"
    config.write_text(_CONFIG.format(directory=directory))
    process = subprocess.Popen(
        [daemon, "--config-file", str(config), "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None

    with process.stdout:
        try:
            yield process.stdout.readline().strip()
        finally:
            process.terminate()
            process.wait()


@dataclass
class Link:
    """
    One connection BlueZ handed over.
    """

    peer: socket.socket
    """
    The far end, standing for the peer.
    """

    kept: socket.socket
    """
    BlueZ's own copy of the end it handed over.
    """

    def drop(self) -> None:
        """
        Lose the link, the way a radio going off does.
        """
        self.peer.close()
        self.kept.close()


def _link() -> Link:
    ours, peer = socket.socketpair()
    return Link(peer, ours)


@dataclass
class Registration:
    """
    A Profile1 registered with the profile manager.
    """

    owner: str
    path: str
    uuid: str
    options: dict[str, Variant]


class FakeAdapter(ServiceInterface):
    """
    ``org.bluez.Adapter1``, with only what the backend reads.
    """

    def __init__(self, name: str, address: str, *, power_state: str = "on") -> None:
        super().__init__("org.bluez.Adapter1")
        self.path = f"/org/bluez/{name}"
        self.address = address
        self.alias = name
        self.power_state = power_state
        self.powered = power_state.startswith("on")

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> DBusStr:
        return self.address

    @dbus_property(access=PropertyAccess.READ)
    def Alias(self) -> DBusStr:
        return self.alias

    @dbus_property(access=PropertyAccess.READ)
    def Powered(self) -> DBusBool:
        return self.powered

    @dbus_property(access=PropertyAccess.READ)
    def PowerState(self) -> DBusStr:
        return self.power_state

    def switch(self, power_state: str) -> None:
        """
        Change the power state, and say so the way BlueZ does.

        Args:
            power_state: One of BlueZ's ``PowerState`` values.
        """
        changed: dict[str, object] = {"PowerState": power_state}
        powered = power_state.startswith("on")

        if powered != self.powered:
            changed["Powered"] = powered

        self.power_state = power_state
        self.powered = powered
        self.emit_properties_changed(changed)

    def switch_off(self) -> None:
        """
        Switch the radio off as BlueZ 5.85 reports it: ``on-disabling`` first,
        with ``Powered`` still true, then off.
        """
        self.switch("on-disabling")
        self.switch("off")


class FakeDevice(ServiceInterface):
    """
    ``org.bluez.Device1``, connecting through the profiles registered with
    the stand-in.
    """

    def __init__(
        self,
        bluez: "FakeBlueZ",
        adapter: FakeAdapter,
        address: str,
        alias: str,
        uuids: list[str],
    ) -> None:
        super().__init__("org.bluez.Device1")
        self.path = f"{adapter.path}/dev_{address.replace(':', '_')}"
        self.adapter = adapter
        self.address = address
        self.alias = alias
        self.uuids = uuids
        self.answers = True
        """
        Whether the device is in range and switched on.
        """
        self.links: list[Link] = []
        self._bluez = bluez

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> DBusStr:
        return self.address

    @dbus_property(access=PropertyAccess.READ)
    def Alias(self) -> DBusStr:
        return self.alias

    @dbus_property(access=PropertyAccess.READ)
    def Adapter(self) -> DBusObjectPath:
        return self.adapter.path

    @dbus_property(access=PropertyAccess.READ)
    def UUIDs(self) -> DBusStrings:
        return self.uuids

    def rename(self, alias: str) -> None:
        self.alias = alias
        self.emit_properties_changed({"Alias": alias})

    def offer(self, uuids: list[str]) -> None:
        self.uuids = uuids
        self.emit_properties_changed({"UUIDs": uuids})

    def drop_links(self) -> None:
        for link in self.links:
            link.drop()

        self.links.clear()

    @method()
    async def ConnectProfile(self, uuid: DBusStr) -> None:
        # Both measured on BlueZ 5.85.
        if not self.answers:
            raise DBusError("org.bluez.Error.Failed", "br-connection-page-timeout")

        registration = self._bluez.registrations.get(uuid.lower())

        if uuid.lower() not in self.uuids or registration is None:
            raise DBusError(
                "org.bluez.Error.BREDR.ProfileUnavailable",
                "No more profiles to connect to",
            )

        self.links.append(await self._bluez.hand_over(registration, self.path))


class FakeProfileManager(ServiceInterface):
    """
    ``org.bluez.ProfileManager1``.
    """

    def __init__(self, bluez: "FakeBlueZ") -> None:
        super().__init__("org.bluez.ProfileManager1")
        self._bluez = bluez

    @method()
    def RegisterProfile(
        self, profile: DBusObjectPath, uuid: DBusStr, options: DBusDict
    ) -> None:
        # Measured on BlueZ 5.85.
        if uuid.lower() in self._bluez.registrations:
            raise DBusError("org.bluez.Error.NotPermitted", "UUID already registered")

        self._bluez.registrations[uuid.lower()] = Registration(
            self._bluez.caller, profile, uuid.lower(), options
        )

    @method()
    def UnregisterProfile(self, profile: DBusObjectPath) -> None:
        for uuid, registration in self._bluez.registrations.items():
            if (registration.owner, registration.path) == (self._bluez.caller, profile):
                del self._bluez.registrations[uuid]
                return

        raise DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")


_ROOT = Node.default("/").tostring()


class FakeBlueZ:
    """
    What bluetoothd puts on the system bus.

    Set up adapters and devices before or after starting it. Once started,
    adding or removing one is announced, as BlueZ announces it.
    """

    def __init__(self, address: str) -> None:
        """
        Args:
            address: The bus to appear on.
        """
        self.address = address
        self.adapters: list[FakeAdapter] = []
        self.devices: list[FakeDevice] = []
        self.registrations: dict[str, Registration] = {}
        self.caller = ""
        """
        Who sent the method call being handled.
        """
        self._bus: MessageBus | None = None
        self._manager = FakeProfileManager(self)

    @property
    def bus(self) -> MessageBus:
        assert self._bus is not None, "BlueZ is not running"
        return self._bus

    async def start(self) -> None:
        """
        Start, the way bluetoothd does: take the name, then export adapters.
        """
        self._bus = await MessageBus(
            bus_address=self.address, negotiate_unix_fd=True
        ).connect()
        self._bus.add_message_handler(self._note)
        await self._bus.request_name("org.bluez")
        self._bus.export("/org/bluez", self._manager)

        for adapter in self.adapters:
            self._bus.export(adapter.path, adapter)

        for device in self.devices:
            self._bus.export(device.path, device)

    async def stop(self) -> None:
        """
        Stop, the way bluetoothd exiting does: everything goes at once.
        """
        for device in self.devices:
            device.drop_links()

        self.registrations.clear()
        bus, self._bus = self._bus, None

        if bus is not None:
            bus.disconnect()
            await bus.wait_for_disconnect()

    def add_adapter(self, name: str, address: str, **kwargs: str) -> FakeAdapter:
        adapter = FakeAdapter(name, address, **kwargs)
        self.adapters.append(adapter)

        if self._bus is not None:
            self._bus.export(adapter.path, adapter)

        return adapter

    def remove_adapter(self, adapter: FakeAdapter) -> None:
        for device in [d for d in self.devices if d.adapter is adapter]:
            self.remove_device(device)

        self.adapters.remove(adapter)

        if self._bus is not None:
            self._bus.unexport(adapter.path)

    def add_device(
        self,
        adapter: FakeAdapter,
        address: str,
        alias: str,
        uuids: list[str] | None = None,
    ) -> FakeDevice:
        device = FakeDevice(self, adapter, address, alias, uuids or [])
        self.devices.append(device)

        if self._bus is not None:
            self._bus.export(device.path, device)

        return device

    def remove_device(self, device: FakeDevice) -> None:
        device.drop_links()
        self.devices.remove(device)

        if self._bus is not None:
            self._bus.unexport(device.path)

    def register_elsewhere(self, uuid: str) -> None:
        """
        Have another program on the machine hold a profile for a UUID.
        """
        self.registrations[uuid.lower()] = Registration(
            ":1.999", "/elsewhere", uuid.lower(), {}
        )

    async def connect_from(self, device: FakeDevice, uuid: str) -> socket.socket:
        """
        Have a peer connect to a service served here.

        Returns:
            The peer's end of the connection.
        """
        registration = self.registrations[uuid]
        assert registration.options["Role"].value == "server"
        link = await self.hand_over(registration, device.path)
        device.links.append(link)
        return link.peer

    async def hand_over(self, registration: Registration, device_path: str) -> Link:
        """
        Call a profile's NewConnection with a fresh connection.

        Returns:
            The connection, whose handed-over end now belongs to the profile.
        """
        link = _link()
        reply = await self.bus.call(
            Message(
                destination=registration.owner,
                path=registration.path,
                interface="org.bluez.Profile1",
                member="NewConnection",
                signature="oha{sv}",
                body=[device_path, 0, {}],
                unix_fds=[link.kept.fileno()],
            )
        )
        assert reply is not None
        assert reply.message_type is MessageType.METHOD_RETURN, reply.body
        return link

    async def flush(self) -> None:
        """
        Wait until everything sent so far has reached every other connection.

        A round trip to each comes back only after what was sent before it.
        """
        reply = await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="ListNames",
            )
        )
        assert reply is not None

        for name in reply.body[0]:
            if name.startswith(":") and name != self.bus.unique_name:
                await self.bus.call(
                    Message(
                        destination=name,
                        path="/",
                        interface="org.freedesktop.DBus.Peer",
                        member="Ping",
                    )
                )

    def _note(self, message: Message) -> Message | None:
        if message.message_type is not MessageType.METHOD_CALL:
            return None

        self.caller = message.sender or ""

        # BlueZ's object manager is on the root, which has no object of its
        # own to hang it from here.
        if (message.path, message.member) == ("/", "Introspect"):
            return Message.new_method_return(message, "s", [_ROOT])

        return None
