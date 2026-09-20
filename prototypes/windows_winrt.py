import asyncio
import contextlib
import enum
import socket
import struct
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from math import log2
from typing import Any

from winrt.system import unbox_boolean, unbox_string
from winrt.windows.devices.bluetooth import (
    BluetoothCacheMode,
    BluetoothDevice,
    BluetoothError,
)
from winrt.windows.devices.bluetooth.rfcomm import (
    RfcommServiceId,
    RfcommServiceProvider,
)
from winrt.windows.devices.enumeration import (
    DeviceInformation,
    DeviceInformationKind,
    DeviceInformationUpdate,
    DeviceWatcher,
)
from winrt.windows.networking.sockets import (
    SocketProtectionLevel,
    StreamSocket,
    StreamSocketListener,
    StreamSocketListenerConnectionReceivedEventArgs,
)
from winrt.windows.storage.streams import DataReader, DataWriter, InputStreamOptions


async def start_server():
    service_id = RfcommServiceId.from_uuid(
        uuid.UUID("90763280-8fc8-46cb-9323-da17226db6b6")
    )
    # service_id = RfcommServiceId.serial_port
    queue = asyncio.Queue[StreamSocket]()
    provider = await RfcommServiceProvider.create_async(service_id)
    listener = StreamSocketListener()

    loop = asyncio.get_running_loop()

    def on_connection_received(
        sender: StreamSocketListener,
        args: StreamSocketListenerConnectionReceivedEventArgs,
    ) -> None:
        # REVISIT: MS example catches error of property access
        loop.call_soon_threadsafe(queue.put_nowait, args.socket)

    listener.add_connection_received(on_connection_received)

    await listener.bind_service_name_with_protection_level_async(
        provider.service_id.as_string(),
        SocketProtectionLevel.BLUETOOTH_ENCRYPTION_ALLOW_NULL_AUTHENTICATION,
    )

    name = "aio-rfcomm WinRT Service".encode()
    provider.sdp_raw_attributes[0x100] = encode_text_string_data_element(name)
    print("advertising rfcomm service...")
    provider.start_advertising_with_radio_discoverability(listener, True)

    client_socket = await queue.get()
    remote_device = await BluetoothDevice.from_host_name_async(
        client_socket.information.remote_host_name
    )
    print(f"Connected  to {remote_device.name}")

    while True:
        reader = DataReader(client_socket.input_stream)
        writer = DataWriter(client_socket.output_stream)
        await reader.load_async(1)

        try:
            data = reader.read_byte()
        except OSError as ex:
            if ex.winerror != -2147483637:  # E_BOUNDS
                raise

            print("Connection closed.")
            break

        print("Received:", data)
        writer.write_byte(data)
        await writer.store_async()


class DataElementType(enum.IntEnum):
    NIL = 0
    UNSIGNED_INTEGER = 1
    SIGNED_INTEGER = 2
    UUID = 3
    TEXT_STRING = 4
    BOOLEAN = 5
    DATA_ELEMENT_SEQUENCE = 6
    DATA_ELEMENT_ALTERNATIVE = 7
    URL = 8


def encode_data_element_header(
    element_type: DataElementType, element_size: int
) -> bytes:
    if element_size > 0xFFFFFFFF:
        raise ValueError("Element size too large.")

    if element_size > 0xFFFF:
        return struct.pack("<BI", (element_type << 3) | 7, element_size)

    if element_size > 0xFF:
        return struct.pack("<BH", (element_type << 3) | 6, element_size)

    if element_size > 0x10 or element_size > 2 and element_size not in (4, 8, 16):
        return struct.pack("<BB", (element_type << 3) | 5, element_size)

    if element_size in (1, 2, 4, 8, 16):
        return struct.pack("<B", (element_type << 3) | int(log2(element_size)))

    if element_size == 0:
        assert element_type == DataElementType.NIL
        return struct.pack("<B", (element_type << 3) | 0)

    raise ValueError("Invalid element size.")


def encode_nil_data_element() -> bytes:
    return encode_data_element_header(DataElementType.NIL, 0)


def encode_boolean_data_element(value: bool) -> bytes:
    return encode_data_element_header(DataElementType.BOOLEAN, 1) + int(value).to_bytes(
        1, "little"
    )


def encode_unsigned_integer_data_element(value: int) -> bytes:
    if value < 0:
        raise ValueError("Value must be non-negative.")

    element_data = value.to_bytes((value.bit_length() + 7) // 8, "little")

    return (
        encode_data_element_header(DataElementType.UNSIGNED_INTEGER, len(element_data))
        + element_data
    )


def encode_uuid_data_element(value: uuid.UUID) -> bytes:
    if value.fields[0] <= 0xFFFF and value.fields[1:] == (
        0,
        0,
        0x1000,
        0x8000,
        0x00805F9B34FB,
    ):
        element_data = value.fields[0].to_bytes(2, "big")
    elif value.fields[0] <= 0xFFFFFFFF and value.fields[1:] == (
        0,
        0,
        0x1000,
        0x8000,
        0x00805F9B34FB,
    ):
        element_data = value.fields[0].to_bytes(4, "big")
    else:
        element_data = value.bytes

    return (
        encode_data_element_header(DataElementType.UUID, len(element_data))
        + element_data
    )


def encode_uuid16_data_element(value: int) -> bytes:
    element_data = value.to_bytes(2, "little")
    return (
        encode_data_element_header(DataElementType.UUID, len(element_data))
        + element_data
    )


def encode_text_string_data_element(value: bytes) -> bytes:
    return encode_data_element_header(DataElementType.TEXT_STRING, len(value)) + value


def encode_data_element_sequence(elements: Iterable[bytes]) -> bytes:
    sequence_data = b"".join(elements)
    return (
        encode_data_element_header(
            DataElementType.DATA_ELEMENT_SEQUENCE, len(sequence_data)
        )
        + sequence_data
    )


def encode_url_data_element(value: str) -> bytes:
    url_bytes = value.encode()
    return encode_data_element_header(DataElementType.URL, len(url_bytes)) + url_bytes


def parse_data_element_header(data: bytes) -> tuple[int, bytes, int]:
    header = data[0]
    element_type = header >> 3
    element_size_index = header & 0x07

    if element_size_index == 7:
        element_size = struct.unpack_from("<I", data, 1)[0]
        element_offset = 5
    elif element_size_index == 6:
        element_size = struct.unpack_from("<H", data, 1)[0]
        element_offset = 3
    elif element_size_index == 5:
        element_size = struct.unpack_from("<B", data, 1)[0]
        element_offset = 2
    else:
        element_size = 1 << element_size_index
        element_offset = 1

    element_value = data[element_offset : element_offset + element_size]

    return element_type, element_value, element_offset + element_size


def decode_data_element(element_type: int, element_value: bytes) -> Any:
    match element_type:
        case DataElementType.NIL:
            return None
        case DataElementType.UNSIGNED_INTEGER:
            return int.from_bytes(element_value, "little")
        case DataElementType.SIGNED_INTEGER:
            return int.from_bytes(element_value, "little", signed=True)
        case DataElementType.UUID:
            match len(element_value):
                case 2:
                    return uuid.UUID(
                        f"0000{int.from_bytes(element_value, 'big'):04x}-0000-1000-8000-00805f9b34fb"
                    )
                case 4:
                    return uuid.UUID(
                        f"{int.from_bytes(element_value, 'big'):08x}-0000-1000-8000-00805f9b34fb"
                    )
                case 16:
                    return uuid.UUID(bytes=bytes(element_value))
                case _:
                    raise ValueError(f"Invalid UUID size: {len(element_value)}")
        case DataElementType.TEXT_STRING:
            return element_value
        case DataElementType.BOOLEAN:
            return bool(element_value[0])
        case DataElementType.DATA_ELEMENT_SEQUENCE:
            return parse_data_element_sequence(element_value)
        case DataElementType.DATA_ELEMENT_ALTERNATIVE:
            return parse_data_element_sequence(element_value)
        case DataElementType.URL:
            return element_value.decode()
        case _:
            raise ValueError(f"Unknown data element type: {element_type}")


def parse_data_element(data: bytes) -> Any:
    element_type, element_value, _ = parse_data_element_header(data)
    return decode_data_element(element_type, element_value)


def parse_data_element_sequence(data: bytes) -> list[Any]:
    elements = list[Any]()
    offset = 0

    while offset < len(data):
        element_type, element_value, element_offset = parse_data_element_header(
            data[offset:]
        )
        elements.append(decode_data_element(element_type, element_value))

        offset += element_offset

    return elements


# https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/service_discovery/attribute_ids/universal_attributes.yaml
class UniversalAttribute(enum.IntEnum):
    SERVICE_RECORD_HANDLE = 0x0000
    SERVICE_CLASS_ID_LIST = 0x0001
    SERVICE_RECORD_STATE = 0x0002
    SERVICE_ID = 0x0003
    PROTOCOL_DESCRIPTOR_LIST = 0x0004
    BROWSE_GROUP_LIST = 0x0005
    LANGUAGE_BASE_ATTRIBUTE_ID_LIST = 0x0006
    SERVICE_INFO_TIME_TO_LIVE = 0x0007
    SERVICE_AVAILABILITY = 0x0008
    BLUETOOTH_PROFILE_DESCRIPTOR_LIST = 0x0009
    DOCUMENTATION_URL = 0x000A
    CLIENT_EXECUTABLE_URL = 0x000B
    ICON_URL = 0x000C
    ADDITIONAL_PROTOCOL_DESCRIPTOR_LISTS = 0x000D


# https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/service_discovery/attribute_id_offsets_for_strings.yaml
class OffsetForString(enum.IntEnum):
    SERVICE_NAME = 0x0000
    SERVICE_DESCRIPTION = 0x0001
    PROVIDER_NAME = 0x0002


# https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/protocol_identifiers.yaml
class ProtocolIdentifier(enum.IntEnum):
    SDP = 0x0001
    UDP = 0x0002
    RFCOMM = 0x0003
    TCP = 0x0004
    TCS_BIN = 0x0005
    TCS_AT = 0x0006
    ATT = 0x0007
    OBEX = 0x0008
    IP = 0x0009
    FTP = 0x000A
    HTTP = 0x000C
    WSP = 0x000E
    BNEP = 0x000F
    UPNP = 0x0010
    HIDP = 0x0011
    HCC = 0x0012
    HDC = 0x0014
    HNC = 0x0016
    AVCTP = 0x0017
    AVDTP = 0x0019
    CMTP = 0x001B
    MCAP_CC = 0x001E
    MCAP_DC = 0x001F
    L2CAP = 0x0100


@dataclass(frozen=True)
class ServiceAttribute:
    service_record_handle: int | None = None
    service_class_id_list: list[uuid.UUID] | None = None
    service_record_state: int | None = None
    service_id: uuid.UUID | None = None
    protocol_descriptor_list: list[list[Any]] | list[list[list[Any]]] | None = None
    additional_protocol_descriptor_list: list[Any] | None = None
    browse_group_list: list[uuid.UUID] | None = None
    language_base_attribute_id_list: list[list[Any]] | None = None
    service_info_time_to_live: int | None = None
    service_availability: int | None = None
    bluetooth_profile_descriptor_list: list[list[Any]] | None = None
    documentation_url: bytes | None = None
    client_executable_url: bytes | None = None
    icon_url: bytes | None = None
    service_name: bytes | None = None
    service_description: bytes | None = None
    provider_name: bytes | None = None

    def get_protocol_descriptor_parameters(
        self, protocol: ProtocolIdentifier
    ) -> list[int]:
        if self.protocol_descriptor_list is None:
            raise TypeError("No protocol descriptor list available.")

        for descriptor in self.protocol_descriptor_list:
            if isinstance(descriptor[0], list):
                raise NotImplementedError("Protocol alternatives not supported yet.")

            if not isinstance(descriptor[0], uuid.UUID):
                raise RuntimeError("Invalid protocol descriptor format.")

            if str(descriptor[0]) == f"0000{protocol:04x}-0000-1000-8000-00805f9b34fb":
                return descriptor[1:]  # type: ignore[return-value]

        raise KeyError("Protocol descriptor not found.")


def parse_attrs(attrs: dict[int, bytes]) -> ServiceAttribute:
    parsed: dict[str, Any] = {}

    for attr_id, attr_value in attrs.items():
        data = parse_data_element(attr_value)

        match attr_id:
            case UniversalAttribute.SERVICE_RECORD_HANDLE:
                parsed["service_record_handle"] = data
            case UniversalAttribute.SERVICE_CLASS_ID_LIST:
                parsed["service_class_id_list"] = data
            case UniversalAttribute.SERVICE_RECORD_STATE:
                parsed["service_record_state"] = data
            case UniversalAttribute.SERVICE_ID:
                parsed["service_id"] = data
            case UniversalAttribute.PROTOCOL_DESCRIPTOR_LIST:
                parsed["protocol_descriptor_list"] = data
            case UniversalAttribute.ADDITIONAL_PROTOCOL_DESCRIPTOR_LISTS:
                parsed["additional_protocol_descriptor_list"] = data
            case UniversalAttribute.BROWSE_GROUP_LIST:
                parsed["browse_group_list"] = data
            case UniversalAttribute.LANGUAGE_BASE_ATTRIBUTE_ID_LIST:
                parsed["language_base_attribute_id_list"] = data
            case UniversalAttribute.SERVICE_INFO_TIME_TO_LIVE:
                parsed["service_info_time_to_live"] = data
            case UniversalAttribute.SERVICE_AVAILABILITY:
                parsed["service_availability"] = data
            case UniversalAttribute.BLUETOOTH_PROFILE_DESCRIPTOR_LIST:
                parsed["bluetooth_profile_descriptor_list"] = data
            case UniversalAttribute.DOCUMENTATION_URL:
                parsed["documentation_url"] = data
            case UniversalAttribute.CLIENT_EXECUTABLE_URL:
                parsed["client_executable_url"] = data
            case UniversalAttribute.ICON_URL:
                parsed["icon_url"] = data
            case _:
                if attr_id & 0xFF00 == 0x0100:
                    match attr_id & 0x00FF:
                        case OffsetForString.SERVICE_NAME:
                            parsed["service_name"] = data
                        case OffsetForString.SERVICE_DESCRIPTION:
                            parsed["service_description"] = data
                        case OffsetForString.PROVIDER_NAME:
                            parsed["provider_name"] = data
                        case _:
                            # TODO: throw on strict and/or log unknown attribute
                            pass

                else:
                    # TODO: throw on strict and/or log unknown attribute
                    pass

    return ServiceAttribute(**parsed)


def print_attrs(attrs: dict[int, bytes]) -> None:
    for attr_id, attr_value in attrs.items():
        data = parse_data_element(attr_value)

        match attr_id:
            case UniversalAttribute.SERVICE_RECORD_HANDLE:
                print(f"Service Record Handle: {data:#08x}")
            case UniversalAttribute.SERVICE_CLASS_ID_LIST:
                print("Service Class ID List:")
                print(f"  {data!r}")
            case UniversalAttribute.PROTOCOL_DESCRIPTOR_LIST:
                print("Protocol Descriptor List:")
                for proto in data:
                    match str(proto[0]):
                        case "00000100-0000-1000-8000-00805f9b34fb":
                            print(f"  L2CAP PSM: {len(proto) > 1 and proto[1] or '?'}")
                        case "00000003-0000-1000-8000-00805f9b34fb":
                            print(f"  RFCOMM Channel: {proto[1]}")
                        case "00000004-0000-1000-8000-00805f9b34fb":
                            print(f"  TCP Port: {proto[1]}")
                        case "00000002-0000-1000-8000-00805f9b34fb":
                            print(f"  UDP Port: {proto[1]}")
                        case "0000000f-0000-1000-8000-00805f9b34fb":
                            print(
                                f"  BNEP Version: {proto[1]}, Supported network packet types: {proto[2]}"
                            )
                        case _:
                            print(f"  Protocol: {proto!r}")

            case UniversalAttribute.BROWSE_GROUP_LIST:
                print("Browse Group List:")
                print(f"  {data!r}")
            case _:
                if attr_id & 0xFF00 == 0x0100:
                    match attr_id & 0x00FF:
                        case OffsetForString.SERVICE_NAME:
                            print("Service Name:")
                            print(f"  {data!r}")
                        case OffsetForString.SERVICE_DESCRIPTION:
                            print("Service Description:")
                            print(f"  {data!r}")
                        case OffsetForString.PROVIDER_NAME:
                            print("Provider Name:")
                            print(f"  {data!r}")
                        case _:
                            print(f"Unknown String Attribute ID: {attr_id:#06x}")
                            print(f"  Value: {data!r}")

                print(f"Unknown Attribute ID: {attr_id:#06x}")
                print(f"  Value: {data!r}")


async def start_client() -> None:
    async with contextlib.AsyncExitStack() as stack:
        service_id = RfcommServiceId.from_uuid(
            uuid.UUID("539f44f8-e629-4723-bd40-9bd0d2807056")
        )

        device_queue = asyncio.Queue[DeviceInformation]()

        properties = [
            "System.Devices.Aep.DeviceAddress",
            "System.Devices.Aep.IsConnected",
        ]
        watcher = DeviceInformation.create_watcher_with_kind_aqs_filter_and_additional_properties(
            '(System.Devices.Aep.ProtocolId:="{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}")',
            properties,
            DeviceInformationKind.ASSOCIATION_ENDPOINT,
        )

        loop = asyncio.get_running_loop()

        def on_added(
            sender: DeviceWatcher,
            args: DeviceInformation,
        ) -> None:
            address = unbox_string(args.properties["System.Devices.Aep.DeviceAddress"])
            is_connected = unbox_boolean(
                args.properties["System.Devices.Aep.IsConnected"]
            )
            print(f"Found device: {args.name} ({address}, {is_connected})")

            loop.call_soon_threadsafe(device_queue.put_nowait, args)

        watcher.add_added(on_added)

        def on_updated(
            sender: DeviceWatcher,
            args: DeviceInformationUpdate,
        ) -> None:
            print(f"Updated device: {args.id}, {list(args.properties.keys())}")

        watcher.add_updated(on_updated)

        watcher.add_stopped(lambda sender, args: print("Watcher stopped."))

        watcher.add_enumeration_completed(
            lambda sender, args: print("Watcher enumeration completed.")
        )

        watcher.start()
        stack.callback(watcher.stop)

        # TODO: add timeout
        while True:
            info = await device_queue.get()

            device = await BluetoothDevice.from_id_async(info.id)
            assert device is not None, "Could not find device."

            print(f"Checking services on {device.name}...")

            result = await device.get_rfcomm_services_for_id_with_cache_mode_async(
                service_id, BluetoothCacheMode.UNCACHED
            )

            if result.error != BluetoothError.SUCCESS:
                print(f"Error retrieving services: {result.error}")
                continue

            for service in result.services:
                if service.service_id.uuid == service_id.uuid:
                    break
            else:
                print("Desired service not found on device.")
                continue

            print("found service")

            break

        raw_attrs = {
            k: bytes(v)
            for k, v in (
                await service.get_sdp_raw_attributes_with_cache_mode_async(
                    BluetoothCacheMode.UNCACHED
                )
            ).items()
        }
        print_attrs(raw_attrs)

        attrs = parse_attrs(raw_attrs)

        print(attrs)

        rfcomm_channel = attrs.get_protocol_descriptor_parameters(
            ProtocolIdentifier.RFCOMM
        )[0]

        rfcomm_socket = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
        )

        bdaddr = device.bluetooth_address.to_bytes(6, "big").hex(":")
        print(f"Connecting to {bdaddr} channel {rfcomm_channel}...")

        loop = asyncio.get_running_loop()

        await loop.sock_connect(rfcomm_socket, (bdaddr, rfcomm_channel))

        reader, writer = await asyncio.open_connection(sock=rfcomm_socket)

        # reader = asyncio.StreamReader()
        # protocol = asyncio.StreamReaderProtocol(reader)
        # transport, _ = await loop.connect_accepted_socket(
        #     lambda: protocol, rfcomm_socket
        # )
        # writer = asyncio.StreamWriter(transport, protocol, reader, loop)

        # socket = StreamSocket()
        # await socket.connect_with_protection_level_async(
        #     service.connection_host_name,
        #     service.connection_service_name,
        #     SocketProtectionLevel.BLUETOOTH_ENCRYPTION_ALLOW_NULL_AUTHENTICATION,
        # )

        # writer = DataWriter(socket.output_stream)
        # reader = DataReader(socket.input_stream)

        # reader.input_stream_options = InputStreamOptions.READ_AHEAD

        while True:
            data_to_send = b"hello\n"  # Example byte to send
            # writer.write_bytes(data_to_send)
            writer.write(data_to_send)

            try:
                await writer.drain()
            except ConnectionResetError:
                print("Connection reset (probably by remote host).")
                break

            # try:
            #     await writer.store_async()
            # except ConnectionAbortedError:
            #     print("Connection aborted (probably by remote host).")
            #     break
            # except OSError as ex:
            #     if ex.winerror != -2147483629:  # RO_E_CLOSED
            #         raise

            #     print("Connection closed (probably by remote host).")
            #     break

            print("Sent:", data_to_send)

            while True:
                try:
                    received_data = await reader.readline()
                except ConnectionResetError:
                    print("Connection reset (probably by remote host).")
                    break

                # try:
                #     num_bytes = await reader.load_async(1)
                # except ConnectionAbortedError:
                #     print("Connection aborted (probably by remote host).")
                #     break

                # if num_bytes == 0:
                #     print("Connection closed by remote host.")
                #     break

                # received_data = bytearray(reader.unconsumed_buffer_length)
                # reader.read_bytes(received_data)
                print("Received:", received_data)

            break
