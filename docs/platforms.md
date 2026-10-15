# Platforms

One API, three very different operating systems. Most of the time the
differences do not reach you. These are the places where they do.

| | Connecting | Serving |
| --- | :---: | :---: |
| **Linux** (BlueZ) | ✅ | ✅ |
| **Windows** | ✅ | ✅ |
| **macOS** | ✅ | ❌ |

## macOS

### Permission

macOS asks the user before any program may use Bluetooth, and the answer is
remembered against the program's identity. A bundled application has one; a
Python script run from a terminal does not, so the library cannot invent one on
its caller's behalf. Declare it yourself, before opening an adapter:

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

A refusal raises
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError] with
the reason `NOT_ALLOWED`, which is distinct from `SWITCHED_OFF` — the library
reads the authorization state directly rather than inferring it, so "you said
no" and "the radio is off" never get confused for one another.

### One channel at a time per device

macOS closes a device's RFCOMM channels in the order they were opened rather
than the one it was asked to close. With two open at once, closing either tears
down the wrong one, and writes to the channel still believed open are accepted
and never arrive.

[`open_channel`][aio_rfcomm.RfcommDevice.open_channel] and
[`open_service`][aio_rfcomm.RfcommDevice.open_service] therefore raise
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] when
the device already has one open, rather than opening a second and losing both.
Channels one after another are fine, and so are channels to different devices at
the same time — it is only two at once to the *same* device that macOS cannot
hold.

### Serving does not work

A service record cannot be published at all. IOBluetooth builds one from a
dictionary of data elements, and the encoder that turns that dictionary into SDP
corrupts every data element sequence: in place of the sequence's contents it
writes out the dictionary's own key names, the literal strings
`DataElementType` and `DataElementValue`. Every useful RFCOMM record needs a
protocol descriptor list, which is a sequence, so the publish fails and the
system logs a parse error.

[`serve`][aio_rfcomm.RfcommAdapter.serve] therefore raises
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] on
macOS. Connecting from this machine is unaffected, within the limit above.

## Stale service records

Both macOS and Linux cache a device's service record, and neither reliably
re-reads it: a query can report success and hand back the set from the last
time. macOS is the more stubborn of the two, caching at pairing time.

For the great majority of devices this never matters, because what they offer
is fixed in firmware and was cached correctly the first time. It matters when a
record *changes* — most obviously when the peer is itself running an
[aio-rfcomm server](serving.md) and published its service after the two were
first introduced. Then [`open_service`][aio_rfcomm.RfcommDevice.open_service]
can raise [`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] for
a service that is demonstrably there.

The way past it is [`open_channel`][aio_rfcomm.RfcommDevice.open_channel] with
the channel number, which does no lookup at all. That only helps when the
channel is stable and known, though — it is no use against a peer that lets the
library choose a free channel, since the number can differ every time the
server starts. Pin the channel on the serving side if clients will need to
hard-code it.

## Linux

The BlueZ backend talks D-Bus and needs no special privileges for the common
case.

**Serving** publishes a BlueZ `Profile1` and needs an RFCOMM channel number to
publish it on. Picking a free one means probing with a Bluetooth socket, since
BlueZ offers no way to ask over D-Bus and a channel collision fails *silently*.
Almost every Python build can open such a socket. On the rare one built without
Bluetooth socket support the library refuses to guess, and
[`serve`][aio_rfcomm.RfcommAdapter.serve] raises
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError]
asking you to name a channel — see [Serving](serving.md).

**Connecting to a bare channel number** needs the same socket support. Opening
by service UUID goes over D-Bus and always works.

## Windows

Connecting and serving both work through WinRT.

**Serving accepts connections on its own thread** rather than through asyncio.
Windows' default event loop cannot accept Bluetooth connections: to accept, it
duplicates the listening socket, and it creates the duplicate without naming a
protocol. That is fine for TCP, where the address family implies the protocol,
and fails for Bluetooth, where it does not — Windows rejects the socket. So the
backend runs its own accept loop on a thread and hands each connection to the
event loop. Nothing about this is visible from the API.
