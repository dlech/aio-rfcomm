# Connecting

To connect to a device, you first need an
[`RfcommDeviceInfo`][aio_rfcomm.RfcommDeviceInfo] for it. There are two ways to
get one. If you already know the device's Bluetooth address, make one from it
yourself, as in [If you know the address](#if-you-know-the-address).
Otherwise, look the device up among the ones the system already knows, as in
[Looking up known devices](#looking-up-known-devices). Then open a channel to
it.

## If you know the address

Make one from the device's Bluetooth address:

```python
DEVICE = aio_rfcomm.RfcommDeviceInfo("00:16:53:11:C4:9A")
```

The address is six pairs of hex digits, separated by colons or by dashes.
Anything else raises `ValueError`. Making one does not contact the device.

## Looking up known devices

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

!!! warning "To find a particular device, use its address"

    Look for a device by its `address`, not by its
    [`name`][aio_rfcomm.RfcommDeviceInfo.name]. The name is whatever the
    system has stored, and it can be out of date, missing, or wrong. The
    address does not change.

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

The system keeps a stored copy of each device's services, which can be out of
date. [Platforms](platforms.md) says how to refresh it on each system, and
which Python builds on Linux cannot connect by number.

**Next:** [Channels](channels.md), to send and receive once connected.
