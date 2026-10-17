# aio-rfcomm

Talk to Bluetooth Classic serial devices from asyncio — the same way on Linux,
macOS and Windows.

```python
async with (
    aio_rfcomm.open_bluetooth() as bluetooth,
    bluetooth.open_service(DEVICE, MY_SERVICE) as channel,
):
    await channel.send(b"hello\n")

    while True:  # until the connection ends, raising ChannelClosedError
        print((await channel.receive()).decode(), end="")
```

That is the whole idea. RFCOMM is how many Bluetooth gadgets that predate BLE
still talk — printers, GPS units, robots, serial adapters, a great many
embedded boards — and reaching one from Python has meant a different library,
a different API and a different set of surprises on every platform.

This is one API for all three, running in your own event loop, with every
connection owned by an `async with` block that cleans up after itself.

## Install

```console
$ uv add aio-rfcomm
```

It needs Python 3.11 or newer. The documentation is at
<https://dlech.github.io/aio-rfcomm/>.

## Serving, too

Publish a service and let devices connect to you:

```python
async with bluetooth.serve(MY_SERVICE, name="my thing") as service:
    async for request in service.connection_requests():
        ...  # accept it, in a task of your own
```
