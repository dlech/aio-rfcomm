import asyncio
from dataclasses import dataclass
import os
from typing import cast
from socket import socket

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusObjectPath, DBusUnixFd, DBusDict
from dbus_fast.service import ServiceInterface, dbus_method
from dbus_fast.aio.proxy_object import ProxyInterface


TEST_DBUS_PATH = "/test/profile"
TEST_SERVICE_UUID = "539f44f8-e629-4723-bd40-9bd0d2807056"
TEST_SERVICE_NAME = "test service"

BLUEZ_DBUS_SERVICE = "org.bluez"
BLUEZ_DBUS_PATH = "/org/bluez"
BLUEZ_PROFILE_INTERFACE = "org.bluez.Profile1"
BLUEZ_PROFILE_MANAGER_INTERFACE = "org.bluez.ProfileManager1"

@dataclass(frozen=True)
class Connection:
    path: str
    fd: int
    fd_properties: dict[str, Variant]


class Profile1(ServiceInterface):
    def __init__(self):
        super().__init__(BLUEZ_PROFILE_INTERFACE)
        self._connections = dict[str, Connection]()

    @dbus_method()
    async def Release(self) -> None:
        print("Profile released")
        # TODO: close all connections
        pass

    @dbus_method()
    async def NewConnection(
        self, device: DBusObjectPath, fd: DBusUnixFd, fd_properties: DBusDict
    ) -> None:
        self._connections[device] = Connection(
            path=device,
            fd=fd,
            fd_properties=fd_properties,
        )

        print(
            f"New connection from {device} with fd {fd} and properties {fd_properties}"
        )

        reader, writer = await asyncio.open_connection(sock=socket(fileno=fd))

        async def echo_lines():
            try:
                async for data in reader:
                    print(f"Received data from {device}: {data}")
                    writer.write(data)
                    await writer.drain()
            except ConnectionResetError:
                print(f"Connection reset by peer: {device}")
                return

        self._task = asyncio.create_task(echo_lines())

    @dbus_method()
    async def RequestDisconnection(self, device: DBusObjectPath) -> None:
        # Seems to only be called for clients, not servers
        connection = self._connections.get(device)
        if connection:
            os.close(connection.fd)
            del self._connections[device]
            print(f"Disconnected from {device}")
        else:
            print(f"No active connection found for {device}")


class ProfileManager1(ProxyInterface):
    async def call_register_profile(
        self, profile: str, uuid: str, options: dict[str, Variant]
    ) -> None: ...

    async def call_unregister_profile(self, profile: str) -> None: ...


async def main() -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()

    profile = Profile1()
    bus.export(TEST_DBUS_PATH, profile)

    node = await bus.introspect(BLUEZ_DBUS_SERVICE, BLUEZ_DBUS_PATH)
    bluez = bus.get_proxy_object(BLUEZ_DBUS_SERVICE, BLUEZ_DBUS_PATH, node)
    profile_manager = cast(
        ProfileManager1, bluez.get_interface(BLUEZ_PROFILE_MANAGER_INTERFACE)
    )

    await profile_manager.call_register_profile(
        TEST_DBUS_PATH,
        TEST_SERVICE_UUID,
        {
            "Name": Variant("s", TEST_SERVICE_NAME),
            "Service": Variant("s", TEST_SERVICE_UUID),
            "Role": Variant("s", "server"),
            "Channel": Variant("q", 22),
            # "RequireAuthentication": Variant("b", False),
            # "RequireAuthorization": Variant("b", False),
        },
    )
    print("Profile registered, waiting for connections...")

    await asyncio.Event().wait()  # Keep the program running


if __name__ == "__main__":
    asyncio.run(main())
