# aio-rfcomm

Cross-platform asyncio RFCOMM (Bluetooth Classic serial port) for Linux, macOS
and Windows.

**Status: nothing is implemented yet.** This repository currently holds the
working platform spikes in [`prototypes/`](prototypes/) and the project
skeleton. The intended API and the measurements that constrain it are written
up in the design notes:

> **Design notes:** https://claude.ai/code/artifact/151501a7-da5f-4c70-aa28-f4d900dbc0bb

## Goals

- One RFCOMM API across Linux, macOS and Windows.
- Native asyncio: works in the caller's own event loop, on any thread, with no
  custom event loop and no run-loop integration required of the user.
- Structured concurrency throughout — every resource owned by an `async with`
  scope, cancellation treated as ordinary control flow.

## Intended API

```python
async with aiorfcomm.adapter() as bt:
    devices = await bt.known_devices(service=MY_UUID)

    async with bt.connect(devices[0], service=MY_UUID) as chan:
        await chan.send(b"hello\n")
        data = await chan.receive()   # b"" at end of stream
```

## How each platform is reached

| platform | mechanism |
|---|---|
| Linux | BlueZ over D-Bus; `Profile1.NewConnection` hands back a connected fd, so no `AF_BLUETOOTH` socket is ever constructed |
| macOS | IOBluetooth in a spawned helper process, connected over an `AF_UNIX` socketpair |
| Windows | WinRT for discovery and SDP, then a Winsock `AF_BLUETOOTH` socket |

Every backend yields a file descriptor, so the core is platform-independent.
The design notes explain why each of these is the way it is — in particular why
macOS needs a separate process at all, and why Linux cannot use Bluetooth
sockets.

## License

MIT
