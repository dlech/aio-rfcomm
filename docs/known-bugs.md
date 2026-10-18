# Known bugs

These are bugs in the operating systems that the library cannot work around.
Each one names the versions it was seen on. [Platforms](platforms.md) covers
the limits the library already handles for you.

## macOS

### Switching Bluetooth off can restart the Bluetooth service

Sometimes, when Bluetooth is switched off, macOS's Bluetooth service waits for
a device to disconnect, gives up after 10 s, and restarts. For those 10 s,
Bluetooth still reads as usable, and open channels keep working. Then every
open channel ends with `SERVICE_UNAVAILABLE`.

A few seconds later, Bluetooth comes back on or off, as the switch was last
set. Switching it back on during the 10 s works. The switch flickers as the
service restarts.

It happens when macOS is still waiting for a connected device to answer
macOS's own connection to one of its profiles, such as Hands-Free. The
Bluetooth log then shows the profile "waiting for device ... to disconnect"
and, 10 s later, `Bluetooth error - restarting`. Once that wait is over,
switching off works cleanly. Seen on macOS 26.6.2, with a peer running BlueZ
5.85.

### Asking a device for its services does not ask it

IOBluetooth's `performSDPQuery:` answers from macOS's stored copy of the
device's services, without sending anything to the device. macOS reads the
services again only when it connects to the device. So a service that moves to
another channel while the device stays connected stays stored on the old
channel, and opening it fails. [Platforms](platforms.md) says what to do. Seen
on macOS 26.6.2, with a peer running BlueZ 5.85.

When the device is not connected, `performSDPQuery:` connects to it instead,
and never calls back. Seen on macOS 26.6.2.

### A paired device can have no stored services

When macOS's Bluetooth service restarts, it forgets the stored services of
every paired device. Switching Bluetooth off can restart it, as
[above](#switching-bluetooth-off-can-restart-the-bluetooth-service). It reads a device's services again the next time it
connects to it. Until then, `list_known_devices` with a `service` leaves the
device out.

To get them back, open a channel to the device, with `open_service` or
`open_channel`. The library connects to the device first, and macOS reads its
services as it connects. Clicking Connect for the device in System Settings >
Bluetooth also works, even for a device that disconnects again a few seconds
later. There is no need to pair the device again.

The Bluetooth log says `sdp attributes is empty` for such a device. Seen on
macOS 26.6.2 after each of eight restarts, with a Pybricks EV3 and a peer
running BlueZ 5.85.

## Windows

### After a refusal, the next open waits

When a device refuses a channel, the next open of a channel to that device
can take about a minute. It happened each time the refused open had to
connect to the device first, and not when Windows was already connected to
it. Seen on Windows 10.0.26200.9457 with a Mac, and on 10.0.26200.9711 with a
Linux computer.

### A channel can stay half open after a program ends

If a program ends while it is opening a channel, the device can be left with
that channel half open. Every later open of the channel then waits about four
minutes and fails with `DeviceNotFoundError`, in new programs too. Other
channels to the same device still open, and other computers can still open
that channel. Disconnecting the device ends it.

Windows closes the connection when the program ends. If the device accepts
the channel after that, Windows never hangs up. The device keeps the channel
open and does not answer later requests to open it. It happened in 1 of 20
tries, with a device that was busy and slow to answer. Seen on Windows
10.0.26200.9711, with a Linux computer running BlueZ 5.85.

Canceling an open does not cause this. The library lets the open finish and
then hangs up.

**Next:** [API reference](reference/index.md), for every public name.
