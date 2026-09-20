import asyncio
import contextlib
import ctypes
import sys
import termios
import tty
from typing import TYPE_CHECKING, Any, cast

from rubicon.objc.api import ObjCInstance, objc_method
from rubicon.objc.eventloop import RubiconEventLoop
from rubicon.objc.runtime import SEL, libc, objc_id
from typed_rubicon_objc.Foundation import NSObject
from typed_rubicon_objc.IOBluetooth import (
    IOBluetoothDevice,
    IOBluetoothRFCOMMChannel,
    IOBluetoothRFCOMMChannelDelegate,
    IOBluetoothSDPUUID,
)

_futures = set[Any]()

if TYPE_CHECKING:

    def _mach_error_string(code: int) -> bytes: ...
else:
    _mach_error_string = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_int)(
        ("mach_error_string", libc), ((1, "code"),)
    )


class MachError(Exception):
    def __init__(self, code: int) -> None:
        message = _mach_error_string(code).decode()
        super().__init__(code, message)

    @property
    def code(self) -> int:
        return self.args[0]

    @property
    def message(self) -> str:
        return self.args[1]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.code})"

    def __str__(self) -> str:
        return f"[mach error {self.code}] {self.message}"


class RFCOMMChannelDelegate(NSObject, protocols=[IOBluetoothRFCOMMChannelDelegate]):
    open_future: asyncio.Future[IOBluetoothRFCOMMChannel]
    closed_event: asyncio.Event
    data_queue: asyncio.Queue[bytes]

    @objc_method
    def init(self):
        self.open_future = asyncio.get_running_loop().create_future()
        self.closed_event = asyncio.Event()
        self.data_queue = asyncio.Queue()

        return self

    @objc_method
    def rfcommChannelClosed_(self, _channel: ObjCInstance) -> None:
        self.closed_event.set()

    # def rfcommChannelControlSignalsChanged_(self, channel):
    #     print("rfcommChannelControlSignalsChanged_", channel)

    @objc_method
    def rfcommChannelData_data_length_(
        self, _channel: ObjCInstance, _data: ctypes.c_void_p, _length: ctypes.c_size_t
    ) -> None:
        data = cast(int, _data)
        length = cast(int, _length)
        self.data_queue.put_nowait(ctypes.string_at(data, length))

    # def rfcommChannelFlowControlChanged_(self, channel):
    #     print("rfcommChannelFlowControlChanged_", channel)

    @objc_method
    def rfcommChannelOpenComplete_status_(
        self, _channel: ObjCInstance, status: int
    ) -> None:
        channel = cast(IOBluetoothRFCOMMChannel, _channel)

        if status:
            self.open_future.set_exception(MachError(status))
        else:
            self.open_future.set_result(channel)

    # def rfcommChannelQueueSpaceAvailable_(self, channel: IOBluetoothRFCOMMChannel):
    #     print(f"rfcommChannelQueueSpaceAvailable_ {channel}\r")

    # def rfcommChannelWriteComplete_refcon_status_(
    #     self, channel: IOBluetoothRFCOMMChannel, refcon: int, status: int
    # ):
    #     print("rfcommChannelWriteComplete_refcon_status_", refcon, status)

    @objc_method
    def rfcommChannelWriteComplete_refcon_status_bytesWritten_(
        self,
        _channel: ObjCInstance,
        _refcon: ctypes.c_void_p,
        status: int,
        _length: ctypes.c_size_t,
    ) -> None:
        refcon = cast(int, _refcon)
        length = cast(int, _length)

        obj = ctypes.py_object.from_address(refcon)
        future = cast(asyncio.Future[int], obj.value)
        _futures.remove(future)

        if status:
            future.set_exception(MachError(status))
        else:
            future.set_result(length)


def _check_error(code: int):
    if code:
        raise MachError(code)


class ConnectionComplete(NSObject):
    future: asyncio.Future[IOBluetoothDevice]

    @objc_method
    def init(self):
        self.future = asyncio.get_running_loop().create_future()

        return self

    @objc_method
    def connectionComplete_status_(self, _device: ObjCInstance, status: int) -> None:
        device = cast(IOBluetoothDevice, _device)

        if self.future.done():
            return

        if status:
            # HACK: for some reason, on timeout status is 10
            if status == 10:
                # replace it with I/O timeout error which is more descriptive
                status = -536870186
            self.future.set_exception(MachError(status))
        else:
            self.future.set_result(device)


async def connect(device: IOBluetoothDevice):
    complete = ConnectionComplete.alloc().init()
    assert complete
    _check_error(device.openConnection(complete))
    # _check_error(
    #     device.openConnection_withPageTimeout_authenticationRequired_(
    #         complete, kDefaultPageTimeout, False
    #     )
    # )
    return await complete.future


async def open_rfcomm_channel(device: IOBluetoothDevice, channel_id: int):
    delegate = RFCOMMChannelDelegate.alloc().init()
    chan_ptr = objc_id()

    ret = device.openRFCOMMChannelAsync(
        ctypes.byref(chan_ptr), withChannelID=channel_id, delegate=delegate
    )
    _check_error(ret)

    chan = IOBluetoothRFCOMMChannel(chan_ptr)
    assert chan is not None
    print(chan, chan.isOpen())

    try:
        return Channel(await delegate.open_future)
    except BaseException:
        chan.closeChannel()
        raise


class Channel:
    def __init__(self, channel: IOBluetoothRFCOMMChannel):
        self._channel = channel
        # channel does not retain delegate, so we need to keep a reference
        self._delegate = cast(RFCOMMChannelDelegate, channel.delegate)

    async def write(self, data: bytes):
        future = asyncio.get_running_loop().create_future()
        refcon = ctypes.py_object(future)

        _check_error(
            self._channel.writeAsync(
                data, length=len(data), refcon=ctypes.addressof(refcon)
            )
        )
        # keep future alive for refcon even when canceled
        _futures.add(future)

        return await future

    async def close(self) -> None:
        self._channel.closeChannel()

    @property
    def data(self) -> asyncio.Queue[bytes]:
        return self._delegate.data_queue

    @property
    def closed(self) -> asyncio.Event:
        return self._delegate.closed_event


class SDPQueryComplete(NSObject):
    future: asyncio.Future[None]

    @objc_method
    def init(self):
        self.future = asyncio.get_running_loop().create_future()

        return self

    @objc_method
    def sdpQueryComplete_status_(self, _device: ObjCInstance, status: int) -> None:
        print(f"sdpQueryComplete_status_ status={status}")
        if self.future.done():
            return

        if status:
            self.future.set_exception(MachError(status))
        else:
            self.future.set_result(None)


# protocols=[IOBluetoothDeviceInquiryDelegate]
class InquiryDelegate(NSObject):
    devices: asyncio.Queue[IOBluetoothDevice]
    inquiry_future: asyncio.Future[bool]

    @objc_method
    def init(self):
        self.devices = asyncio.Queue()
        self.inquiry_future = asyncio.get_running_loop().create_future()

        return self

    @objc_method
    def deviceInquiryComplete_error_aborted_(
        self, _sender: ObjCInstance, _error: ctypes.c_int, _aborted: ctypes.c_bool
    ) -> None:
        error = cast(int, _error)
        aborted = cast(bool, _aborted)

        if self.inquiry_future.done():
            return

        if error:
            self.inquiry_future.set_exception(MachError(error))
        else:
            self.inquiry_future.set_result(aborted)

    @objc_method
    def deviceInquiryDeviceFound_device_(
        self, _sender: ObjCInstance, _device: ObjCInstance
    ) -> None:
        device = cast(IOBluetoothDevice, _device)
        print(
            f"deviceInquiryDeviceFound_device_: {device.name} ({device.addressString})"
        )
        self.devices.put_nowait(device)

    @objc_method
    def deviceInquiryDeviceNameUpdated_device_devicesRemaining_(
        self,
        _sender: ObjCInstance,
        _device: ObjCInstance,
        devicesRemaining: int,
    ) -> None:
        device = cast(IOBluetoothDevice, _device)
        print(
            f"deviceInquiryDeviceNameUpdated_device_devicesRemaining: {device.name} ({devicesRemaining} remaining)"
        )

    @objc_method
    def deviceInquiryStarted_(self, _sender: ObjCInstance) -> None:
        print("deviceInquiryStarted_")

    @objc_method
    def deviceInquiryUpdatingDeviceNamesStarted_devicesRemaining_(
        self, _sender: ObjCInstance, devicesRemaining: int
    ) -> None:
        print(
            f"deviceInquiryUpdatingDeviceNamesStarted_devicesRemaining: {devicesRemaining}"
        )


CUSTOM_UUID = IOBluetoothSDPUUID.uuidWithData(
    bytes.fromhex("53 9f 44 f8 e6 29 47 23 bd 40 9b d0 d2 80 70 56")
)


class ConnectionWatcher(NSObject):
    @objc_method
    def didConnect_device_(self, _noti: ObjCInstance, _device: ObjCInstance) -> None:
        device = cast(IOBluetoothDevice, _device)
        print(f"device {device.name} connected")


class DisconnectWatcher(NSObject):
    @objc_method
    def didDisconnect_device_(self, _noti: ObjCInstance, _device: ObjCInstance) -> None:
        device = cast(IOBluetoothDevice, _device)
        print(f"device {device.name} disconnected")


async def main():
    async with contextlib.AsyncExitStack() as stack:
        # inquiry_delegate = InquiryDelegate.alloc().init()
        # assert inquiry_delegate is not None
        # inquiry = IOBluetoothDeviceInquiry.inquiryWithDelegate_(inquiry_delegate)

        # inquiry.setInquiryLength_(5)
        # inquiry.setUpdateNewDeviceNames_(False)
        # inquiry.start()
        # stack.callback(inquiry.stop)

        # aborted = await inquiry_delegate.inquiry_future
        # print(f"inquiry complete (aborted: {aborted})")

        connect_watcher = ConnectionWatcher.alloc().init()
        connect_noti = IOBluetoothDevice.registerForConnectNotifications(
            connect_watcher, selector=SEL("didConnect:device:")
        )
        stack.callback(connect_noti.unregister)

        print("paired devices:")
        devices = IOBluetoothDevice.pairedDevices()

        if not devices:
            print("  (none)")

            return

        for device in devices:
            print(f"  {device.name} (last updated: {device.lastNameUpdate})")
            print(f"    {device.addressString}")
            print(f"    connected: {device.isConnected()} (rssi: {device.rawRSSI()})")

            if device.isConnected():
                completion = SDPQueryComplete.alloc().init()
                ret = device.performSDPQuery(completion)
                if ret:
                    raise MachError(ret)

                await completion.future

            print("    SDP services:")
            services = device.services

            if not services:
                print("      (none)")
            else:
                for service in services:
                    print(f"      {service.getServiceName()} ({service.attributes[1]})")

                    psm = ctypes.c_uint16()
                    ret = service.getL2CAPPSM(ctypes.byref(psm))
                    if not ret:
                        print(f"        L2CAP PSM {psm.value}")

                    rfcomm_id = ctypes.c_char()
                    ret = service.getRFCOMMChannelID(ctypes.byref(rfcomm_id))
                    if not ret:
                        print(f"        RFCOMM channel {rfcomm_id.value[0]}")

                    # print(f"        {service.attributes()}")

            print()

        for device in devices:
            service = device.getServiceRecordForUUID(CUSTOM_UUID)

            if service:
                break
        else:
            print("no device with UUID found")
            return

        disconnect_watcher = DisconnectWatcher.alloc().init()
        disconnect_noti = device.registerForDisconnectNotification(
            disconnect_watcher, selector=SEL("didDisconnect:device:")
        )
        stack.callback(disconnect_noti.unregister)

        if device.isConnected():
            # print("device is already connected, disconnecting...")
            # device.closeConnection()
            # # TODO: use the disconnect watcher instead of sleeping
            # await asyncio.sleep(5)
            # print("reconnecting...")
            # await connect(device)
            pass
        else:
            print("connecting...")
            # TODO: this may never return if Bluetooth is off
            await connect(device)
            # try:
            #     async with asyncio.timeout(5):
            #         await connect(device)
            # except asyncio.TimeoutError:
            #     print("timeout")
            #     await asyncio.sleep(15)
            #     return
            stack.callback(device.closeConnection)

        print("connected.")

        print("querying SDP...")
        completion = SDPQueryComplete.alloc().init()
        assert completion is not None
        ret = device.performSDPQuery(completion)
        if ret:
            raise MachError(ret)

        await completion.future

        service = device.getServiceRecordForUUID(CUSTOM_UUID)
        if not service:
            print("service disappeared")
            return

        channel = ctypes.c_char()
        service.getRFCOMMChannelID(ctypes.byref(channel))

        print(f"opening channel {channel.value[0]} ...")

        async with asyncio.timeout(30):
            chan = await open_rfcomm_channel(device, channel.value[0])

        stack.push_async_callback(chan.close)
        print("opened.")

        # written = await chan.write(b" ")
        # print(f"{written} bytes written")

        loop = asyncio.get_running_loop()

        async def read():
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

            while True:
                ch = await reader.read(1)

                # run until stdin is closed
                if not ch:
                    print("stdin closed, exiting")
                    break

                # Or CTRL+\ is pressed
                if ch == b"\x1c":
                    break

                # translate CR to CRLF for better terminal compatibility
                if ch == b"\r":
                    ch += b"\n"

                await chan.write(ch)

        async def write():
            while True:
                sys.stdout.buffer.write(await chan.data.get())
                sys.stdout.buffer.flush()

        print("starting terminal mode - use CTRL+\\ to exit")
        old = termios.tcgetattr(sys.stdin.fileno())
        stack.callback(termios.tcsetattr, sys.stdin.fileno(), termios.TCSADRAIN, old)

        tty.setraw(sys.stdin.fileno(), termios.TCSADRAIN)

        read_task = loop.create_task(read(), name="read")
        write_task = loop.create_task(write(), name="write")
        closed_task = loop.create_task(chan.closed.wait(), name="closed")

        done, pending = await asyncio.wait(
            {read_task, write_task, closed_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if closed_task in done:
            print("channel closed by remote")

        for task in pending:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=RubiconEventLoop)
