# Connecting

## Naming a device

Every open takes an [`RfcommDeviceInfo`][aio_rfcomm.RfcommDeviceInfo]. A
program that starts from an address makes one from it, which checks the
address as it goes — six two-digit hex groups, separated all by colons or all
by dashes, in either case — and raises `ValueError` for anything else:

```python
DEVICE = aio_rfcomm.RfcommDeviceInfo("00:16:53:11:C4:9A")
```

Nothing is contacted to make one, so it works for a device that is switched
off, out of range, or not even paired. Those failures turn up when a channel
is opened.

## Finding a device

This library does not pair devices yet, so for now pairing happens in the
operating system's own settings. By the time your program runs the device is
normally already paired, and
[`list_known_devices`][aio_rfcomm.Bluetooth.list_known_devices] shows what the
OS knows:

```python
async with aio_rfcomm.open_bluetooth() as bluetooth:
    for device in await bluetooth.list_known_devices():
        print(device.address, device.name)
```

Pass a service UUID to narrow the list to devices that offer it:

```python
bricks = await bluetooth.list_known_devices(service=SERIAL_PORT)
```

!!! warning "Match on the address, not the name"

    [`RfcommDeviceInfo.name`][aio_rfcomm.RfcommDeviceInfo] is whatever the OS
    has cached, which may be stale, may be missing, and on some devices is
    simply wrong. The address is the identity.

## Opening a channel

There are two ways in, and which one you want depends on what the device
publishes.

**By service UUID** is the usual choice. The library looks up the UUID in the
device's service record, learns which RFCOMM channel it lives on, and connects
there:

```python
async with bluetooth.open_service(DEVICE, SERIAL_PORT) as channel:
    ...
```

**By channel number** skips the lookup entirely, for devices whose channel is
fixed and documented. The LEGO MINDSTORMS NXT is on channel 1, for instance:

```python
async with bluetooth.open_channel(DEVICE, 1) as channel:
    ...
```

A number outside 1 to 30 is not an RFCOMM channel, and raises `ValueError`
before anything is contacted. A device with nothing listening on the channel
refuses it, which on macOS and Linux raises
[`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError], as asking for
a service it does not offer would; Windows does not tell a refusal apart yet
(see [Platforms](platforms.md#windows)).

Skipping the lookup is also how you get past a stale service record, which
both macOS and Linux can hand you — but only when the channel is fixed and you
know it. A server that lets the library pick a free channel can land on a
different one each time it starts, and then there is nothing to hard-code. See
[Platforms](platforms.md).

Opening, like everything else, refuses at once with
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError]
while Bluetooth cannot be used, and an open in progress ends with it if
Bluetooth goes away; see [Bluetooth](bluetooth.md).

Once a channel is open, see [Channels](channels.md).
