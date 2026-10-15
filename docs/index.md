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

<div class="grid cards" markdown>

- **[Bluetooth](bluetooth.md)**: open it, and check that it can be used.
- **[Connecting](connecting.md)**: find a device and open a channel to it.
- **[Channels](channels.md)**: send and receive bytes, and read messages.
- **[Serving](serving.md)**: let devices connect to you.
- **[Lifetimes](lifetimes.md)**: notice when a connection ends, and cancel
  safely.
- **[Errors](errors.md)**: what each error means.
- **[Platforms](platforms.md)**: where Linux, macOS and Windows differ, and
  the permission macOS asks for.
- **[API reference](reference.md)**: every public name.

</div>
