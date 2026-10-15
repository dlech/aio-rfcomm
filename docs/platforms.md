# Platforms

One API, three very different operating systems. Most of the time the
differences do not reach you. These are the places where they do.

| | Connecting | Serving |
| --- | :---: | :---: |
| **Linux** (BlueZ) | ✅ | ✅ |
| **Windows** | ✅ | ✅ |
| **macOS** | ✅ | ✅ |

## macOS

### Permission

macOS asks the user before any program may use Bluetooth, and the answer is
remembered against the program's identity. A bundled application has one; a
Python script run from a terminal does not, so the library cannot invent one on
its caller's behalf. Declare it yourself, before opening Bluetooth:

```python
import aio_rfcomm.macos

aio_rfcomm.macos.prompt_under_name(name="my program", reason="to talk to the robot")
```

The call does nothing on Linux and Windows, so it needs no `sys.platform`
guard.

!!! warning "The name is the identity"

    macOS remembers the answer against the `name` you pass. **Changing it asks
    the user again** — and the library waits, indefinitely and by design, for
    an answer that may be sitting behind other windows. Pick a name and keep
    it.

A refusal is reported as the availability `NOT_ALLOWED`, and anything
attempted raises
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError] with
that reason. It is distinct from `SWITCHED_OFF` — the library reads the
authorization state directly rather than inferring it, so "you said no" and
"the radio is off" never get confused for one another.

When macOS refuses by ending the helper process, as it does for a program with
no declared reason, the library does not start another, since it would be
ended the same way. Allowing Bluetooth then takes effect the next time the
program starts.

### One channel at a time per device

macOS closes a device's RFCOMM channels in the order they were opened rather
than the one it was asked to close. With two open at once, closing either tears
down the wrong one, and writes to the channel still believed open are accepted
and never arrive.

[`open_channel`][aio_rfcomm.Bluetooth.open_channel] and
[`open_service`][aio_rfcomm.Bluetooth.open_service] therefore raise
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] when
the device already has one open, rather than opening a second and losing both.
Channels one after another are fine, and so are channels to different devices at
the same time — it is only two at once to the *same* device that macOS cannot
hold.

### Each service runs in a process of its own

[`serve`][aio_rfcomm.Bluetooth.serve] starts a helper process for the service,
which ends once the service has been withdrawn and the last channel it handed
over has closed. Nothing about this is visible from the API, but the reason is
worth knowing.

macOS's Bluetooth daemon has a fixed number of slots for programs listening on
RFCOMM channels, shared by every program on the Mac, and frees a program's
slot only when that program's process ends. A process that registers to listen
a second time loses the first registration's slot until Bluetooth is next
switched off. So one process per service is the only way to serve repeatedly
without using them up.

If another program has used them up, every peer's connection to this Mac waits
until it gives up, and nothing reports why. Switching Bluetooth off and on
again frees them.

**A channel is served once per Bluetooth session.** macOS refuses every
request to withdraw a published service record, so leaving a `serve` block
leaves its record advertised until Bluetooth is next switched off. And once a
channel has had a connection, macOS's Bluetooth daemon keeps it, and a later
attempt to serve on that channel accepts the old, dead connection instead of
listening for new ones, with nothing to say so. So the library never serves on
a channel that already has a record: left to choose, it picks the highest one
without one (passing over 10 and 15, which the daemon treats specially), and a
channel asked for that has one raises
[`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError]. Switching
Bluetooth off and on again clears both the records and the kept connections,
except for services the system itself publishes.

A long-running server is unaffected. It is a program that serves, exits and
serves again that moves to a new channel each time. A client with a channel
number written into it cannot follow. One that finds the service by UUID did,
in every run measured against BlueZ, even with the earlier records still
advertised; a client that caches records may not.

## Stale service records

Both macOS and Linux cache a device's service record, and neither reliably
re-reads it: a query can report success and hand back the set from the last
time. macOS is the more stubborn of the two, caching at pairing time.

For the great majority of devices this never matters, because what they offer
is fixed in firmware and was cached correctly the first time. It matters when a
record *changes* — most obviously when the peer is itself running an
[aio-rfcomm server](serving.md) and published its service after the two were
first introduced. Then [`open_service`][aio_rfcomm.Bluetooth.open_service]
can raise [`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] for
a service that is demonstrably there.

The way past it is [`open_channel`][aio_rfcomm.Bluetooth.open_channel] with
the channel number, which does no lookup at all. That only helps when the
channel is stable and known, though — it is no use against a peer that lets the
library choose a free channel, since the number can differ every time the
server starts. Pin the channel on the serving side if clients will need to
hard-code it — though on macOS a pinned channel can be served only once
until Bluetooth is switched off and on, as above.

## Linux

The BlueZ backend talks D-Bus and needs no special privileges for the common
case.

**Serving** publishes a BlueZ `Profile1` and needs an RFCOMM channel number to
publish it on. Picking a free one means probing with a Bluetooth socket, since
BlueZ offers no way to ask over D-Bus and a channel collision fails *silently*.
Almost every Python build can open such a socket. On the rare one built without
Bluetooth socket support the library refuses to guess, and
[`serve`][aio_rfcomm.Bluetooth.serve] raises
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError]
asking you to name a channel — see [Serving](serving.md).

**Connecting to a bare channel number** needs the same socket support. Opening
by service UUID goes over D-Bus and always works.

## Windows

Connecting and serving both work through WinRT.

**Availability is read once,** when Bluetooth is opened. Windows reports a
radio's changes and adapters arriving, but those reports have not yet been
measured, so the library does not follow them yet: a radio switched on later
is not noticed until the program opens Bluetooth again, and one switched off
later is noticed by the channels it ends rather than by
[`watch_availability`][aio_rfcomm.Bluetooth.watch_availability].

**A refused channel number is not told apart yet.** Opening a channel nothing
on the device is listening on raises
[`ConnectionFailedError`][aio_rfcomm.errors.ConnectionFailedError] here, after
the usual retries, rather than the
[`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] macOS and
Linux raise, because what Windows reports for it has not been measured.

**Serving accepts connections on its own thread** rather than through asyncio.
Windows' default event loop cannot accept Bluetooth connections: to accept, it
duplicates the listening socket, and it creates the duplicate without naming a
protocol. That is fine for TCP, where the address family implies the protocol,
and fails for Bluetooth, where it does not — Windows rejects the socket. So the
backend runs its own accept loop on a thread and hands each connection to the
event loop. Nothing about this is visible from the API.
