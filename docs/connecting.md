# Connecting

## Naming a device

Every connection needs an [`RfcommDeviceInfo`][aio_rfcomm.RfcommDeviceInfo].
Make one from the device's Bluetooth address:

```python
DEVICE = aio_rfcomm.RfcommDeviceInfo("00:16:53:11:C4:9A")
```

The address is six pairs of hex digits, separated by colons or by dashes.
Anything else raises `ValueError`. Making one does not contact the device. To
tell whether two of them are the same device, compare their `address`.

## Finding a device

Pair the device in the operating system's settings before using it. Then
[`list_known_devices`][aio_rfcomm.Bluetooth.list_known_devices] lists the
devices the system knows:

```python
async with aio_rfcomm.open_bluetooth() as bluetooth:
    for device in await bluetooth.list_known_devices():
        print(device.address, device.name)
```

Pass a service UUID to list only the devices that offer that service:

```python
serial_devices = await bluetooth.list_known_devices(service=SERIAL_PORT)
```

!!! warning "Match on the address, not the name"

    The [`name`][aio_rfcomm.RfcommDeviceInfo.name] is whatever the system has
    stored. It can be out of date, missing, or wrong. The address does not
    change.

## Opening a channel

A device offers each of its services on a numbered RFCOMM channel, from 1 to
30. There are two ways to connect.

**By service UUID** is the usual way. The library looks up which RFCOMM
channel the device offers the service on, and connects to it:

```python
async with bluetooth.open_service(DEVICE, SERIAL_PORT) as channel:
    ...
```

**By channel number** skips the lookup. Use it for a device whose channel
number is fixed and documented. For example, the LEGO MINDSTORMS NXT uses
channel 1:

```python
async with bluetooth.open_channel(DEVICE, 1) as channel:
    ...
```

Channel numbers go from 1 to 30. Any other number raises `ValueError`. If
nothing on the device is listening on that channel, the device refuses, and
[`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] is raised.

Connecting by number also helps when the system's stored copy of the device's
services is out of date. See [Platforms](platforms.md), which also covers the
Python builds on Linux that cannot connect by number.

Once a channel is open, see [Channels](channels.md).
