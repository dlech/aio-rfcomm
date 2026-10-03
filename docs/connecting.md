# Connecting

## Finding a device

This library does not pair devices yet, so for now pairing happens in the
operating system's own settings. By the time your program runs the device is
normally already paired, and
[`list_known_devices`][aio_rfcomm.RfcommAdapter.list_known_devices] shows what
the OS knows:

```python
async with aio_rfcomm.open_adapter() as adapter:
    for device in await adapter.list_known_devices():
        print(device.address, device.name)
```

Pass a service UUID to narrow the list to devices that offer it:

```python
bricks = await adapter.list_known_devices(service=SERIAL_PORT)
```

!!! warning "Match on the address, not the name"

    [`RfcommDeviceInfo.name`][aio_rfcomm.RfcommDeviceInfo] is whatever the OS
    has cached, which may be stale, may be missing, and on some devices is
    simply wrong. The address is the identity.

If you already know the address you do not need to list anything at all.
[`use_device`][aio_rfcomm.RfcommAdapter.use_device] contacts nothing, so it
succeeds for a device that is switched off, out of range, or not even paired.
The address is only resolved when you open a channel, which is where those
failures turn up:

```python
device = adapter.use_device("00:16:53:11:C4:9A")
```

## Opening a channel

There are two ways in, and which one you want depends on what the device
publishes.

**By service UUID** is the usual choice. The library looks up the UUID in the
device's service record, learns which RFCOMM channel it lives on, and connects
there:

```python
async with device.open_service(SERIAL_PORT) as channel:
    ...
```

**By channel number** skips the lookup entirely, for devices whose channel is
fixed and documented. The LEGO MINDSTORMS NXT is on channel 1, for instance:

```python
async with device.open_channel(1) as channel:
    ...
```

Skipping the lookup is also how you get past a stale service record, which
both macOS and Linux can hand you — but only when the channel is fixed and you
know it. A server that lets the library pick a free channel can land on a
different one each time it starts, and then there is nothing to hard-code. See
[Platforms](platforms.md).

Once a channel is open, see [Channels](channels.md).
