# aio-rfcomm

Talk to Bluetooth Classic serial devices from asyncio — the same way on Linux,
macOS and Windows.

```python
async with aio_rfcomm.open_adapter() as adapter:
    device = adapter.use_device("00:16:53:11:C4:9A")

    async with device.open_service(MY_SERVICE) as channel:
        await channel.send(b"hello\n")

        while data := await channel.receive():
            print(data.decode(), end="")
```

That is the whole idea. RFCOMM is how most Bluetooth gadgets that predate BLE
still talk — printers, GPS units, robots, serial adapters, a great many
embedded boards — and reaching one from Python has meant a different library,
a different API and a different set of surprises on every platform.

This is one API for all three, running in your own event loop, with every
connection owned by an `async with` block that cleans up after itself.

## Install

```console
$ uv add aio-rfcomm
```

## Serving, too

Publish a service and let devices connect to you (Linux and Windows):

```python
async with adapter.serve(MY_SERVICE, handler, name="my thing") as service:
    print(f"listening on channel {service.channel}")
```
