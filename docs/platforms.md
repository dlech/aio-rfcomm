# Platforms

The API is the same on Linux, macOS and Windows. This page covers the few
places where the platforms still differ.

## macOS

### Permission

macOS asks the user before a program can use Bluetooth, and remembers the
answer under the program's name.

A bundled application is asked about under its own name. Put
`NSBluetoothAlwaysUsageDescription` in the application's `Info.plist`, with
the reason to show in the dialog. Without it, macOS can end the program
instead of asking.

A script run from a terminal has no name of its own, so give it one before
opening Bluetooth:

```python
import aio_rfcomm.macos

aio_rfcomm.macos.prompt_under_name(name="my program", reason="to talk to the robot")
```

The user sees both strings in the permission dialog. The call does nothing on
Linux and Windows, so it can run on every platform.

!!! warning "Keep the name the same"

    macOS remembers the answer under the name you pass. The first time, and
    after any change of name, macOS asks, and `open_bluetooth` waits until the
    user answers, however long that takes.

If the user says no, or a script did not give a name, the availability is
`NOT_ALLOWED`. After the user allows it in the system settings, start the
program again.

### One channel per device at a time

macOS cannot keep two channels open to the same device. It may close the
other channel instead of the one you asked for, and the data on both is lost.
So opening a second channel to a
device that already has one open raises
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError].
Close the first one before opening the next. Channels to different devices at
the same time work.

### Each channel is served once

On macOS, a channel can be served only once until Bluetooth is turned off and
on again. After serving ends, macOS keeps the service published, and a new
service on the same channel never receives connections. So the library serves
only on channels that have not been used since Bluetooth was turned on, and
asking for a used one raises
[`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError].

A program that keeps serving is not affected, and accepts connection after
connection. A program that serves, stops and serves again gets a new channel
each time. Devices that find the service by its UUID follow it to the new
channel. A device set up with a fixed channel number does not.

If `serve` runs out of channels, or connections to every service on this Mac
wait until they time out, turn Bluetooth off and on again.

### When a device's services change

macOS keeps a stored copy of each device's services, and does not always
refresh it. Most devices never change their services, so this rarely matters.
It matters when a device's services change after pairing, for example when the
device is running an aio-rfcomm server of its own. Then
[`open_service`][aio_rfcomm.Bluetooth.open_service] can raise
[`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] even though
the service is there.

To refresh it, remove the device in the system settings and pair it again.

## Linux

### Permission

The library talks to BlueZ over the system D-Bus, so the program needs
permission to send messages to `org.bluez`. BlueZ's standard D-Bus policy
gives that to every user. A sandboxed program, such as a Snap or a Flatpak,
has to be granted it.

### Bluetooth sockets

Two things need a Python built with Bluetooth socket support: serving without
naming a channel, and connecting by channel number. uv's own Python builds do
not have it. The Python packaged by most Linux distributions does. To check
yours, run `python -c "import socket; socket.AF_BLUETOOTH"`, which fails
without it. Without it, these raise
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError].
Connecting by service UUID works on every Python.

When you serve on a channel number of your choosing without that support,
the library cannot check that the channel is free. If it is not, BlueZ
publishes nothing and gives no error. Channel 9, for example, is often taken
by Object Push.

### When a device's services change

BlueZ keeps a stored copy of each device's services, and does not always
refresh it. Most devices never change their services, so this rarely matters.
It matters when a device's services change after pairing, for example when the
device is running an aio-rfcomm server of its own. Then
[`open_service`][aio_rfcomm.Bluetooth.open_service] can raise
[`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] even though
the service is there.

To refresh it, connect the device, and then try again:

```sh
bluetoothctl connect 00:1A:7D:DA:71:0C
```

**Next:** [API reference](reference/index.md), for every public name.
