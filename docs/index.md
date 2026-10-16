# aio-rfcomm

Talk to Bluetooth Classic serial devices from asyncio — the same way on Linux,
macOS and Windows.

```python
import asyncio
from uuid import UUID

import aio_rfcomm
from aio_rfcomm.errors import ChannelClosedError

SERIAL_PORT = UUID("00001101-0000-1000-8000-00805f9b34fb")
DEVICE = aio_rfcomm.RfcommDeviceInfo("00:16:53:11:C4:9A")


async def main() -> None:
    async with (
        aio_rfcomm.open_bluetooth() as bluetooth,
        bluetooth.open_service(DEVICE, SERIAL_PORT) as channel,
    ):
        await channel.send(b"hello\n")

        try:
            while True:
                print((await channel.receive()).decode(), end="")
        except ChannelClosedError:
            print("the other end hung up")


asyncio.run(main())
```

Many Bluetooth devices that are older than Bluetooth Low Energy talk over
RFCOMM: printers, GPS units, robots, serial adapters and many embedded boards.
Until now, reaching one from Python meant a different library and a different
API on every platform. This is one API for all three.

## Install

```console
$ uv add aio-rfcomm
```

## Where to go next

- **[Bluetooth](bluetooth.md)**: start here. Get the object everything else
  goes through, and check that Bluetooth can be used.
- **[Connecting](connecting.md)**: connect to a device.
- **[Serving](serving.md)**: let devices connect to you.

Everything else is in the menu.
