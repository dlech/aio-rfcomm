# aio-rfcomm

Talk to Bluetooth Classic serial devices from asyncio — the same way on Linux,
macOS and Windows.

```python
import asyncio
from uuid import UUID

import aio_rfcomm

SERIAL_PORT = UUID("00001101-0000-1000-8000-00805f9b34fb")


async def main() -> None:
    async with aio_rfcomm.open_adapter() as adapter:
        device = adapter.use_device("00:16:53:11:C4:9A")

        async with device.open_service(SERIAL_PORT) as channel:
            await channel.send(b"hello\n")

            while data := await channel.receive():
                print(data.decode(), end="")


asyncio.run(main())
```

RFCOMM is how most Bluetooth gadgets that predate BLE still talk — printers,
GPS units, robots, serial adapters, a great many embedded boards. Reaching one
from Python has meant a different library, a different API and a different set
of surprises on every platform. This is one API for all three.

## Install

```console
$ uv add aio-rfcomm
```

## Where to go next

<div class="grid cards" markdown>

- **[Connecting](connecting.md)** — find a device, open a channel, and split
  what arrives back into messages. RFCOMM delivers bytes, not messages.
- **[Serving](serving.md)** — publish a service and let devices connect to
  you.
- **[Lifetimes](lifetimes.md)** — why everything is an `async with`, and how a
  channel that dies takes your tasks down with it.
- **[Platforms](platforms.md)** — what each operating system can and cannot
  do, and the permission macOS will ask for.
- **[API reference](reference.md)** — every public name.

</div>
