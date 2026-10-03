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

aio_rfcomm.macos.prompt_under_own_name(name="my program", reason="to talk to the robot")
```

The call does nothing on Linux and Windows, so it needs no `sys.platform`
guard.

!!! warning "The name is the identity"

    macOS remembers the answer against the `name` you pass. **Changing it asks
    the user again** — and the library waits, indefinitely and by design, for
    an answer that may be sitting behind other windows. Pick a name and keep
    it.

A refusal raises
[`PermissionDeniedError`][aio_rfcomm.errors.PermissionDeniedError], which is
distinct from [`AdapterOffError`][aio_rfcomm.errors.AdapterOffError] — the
library reads the authorization state directly rather than inferring it, so
"you said no" and "the radio is off" never get confused for one another.

### Serving does not work

Publishing a service record succeeds, and a peer connecting to it *is* accepted
at the Bluetooth level — but the channel-open notification never reaches the
program, filtered or not. IOBluetooth is now a shim over CoreBluetooth, and the
classic server path appears not to have survived that.

[`serve`][aio_rfcomm.RfcommAdapter.serve] therefore raises
[`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] on
macOS rather than publishing a record that silently never delivers a
connection. Connecting is unaffected.

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
