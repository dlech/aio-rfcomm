# Bluetooth

## The Bluetooth object

Everything your program does with Bluetooth goes through one object. Get it
from [`open_bluetooth`][aio_rfcomm.open_bluetooth], then use it to connect to
devices or to serve. When the `async with` block ends, everything you opened
through it is closed.

```python
async with aio_rfcomm.open_bluetooth() as bluetooth:
    ...
```

`open_bluetooth` succeeds even when Bluetooth is turned off or missing. To find
out whether it can be used, see the next section.

Call `open_bluetooth` once, near the start of your program, and pass the
`bluetooth` object to the functions that need it. Calling it again while the
first block is still open raises
[`BluetoothAlreadyOpenError`][aio_rfcomm.errors.BluetoothAlreadyOpenError].

## Checking that Bluetooth can be used

[`watch_availability`][aio_rfcomm.Bluetooth.watch_availability] gives the
current [`Availability`][aio_rfcomm.Availability] straight away, and then a new
value each time it changes.

```python
async for availability in bluetooth.watch_availability():
    match availability:
        case Availability.USABLE:
            break
        case Availability.UNSUPPORTED:
            print("this system has no Bluetooth")
            return
        case _:
            print(f"waiting: {availability.name}")
```

| Availability | Means | What the user can do |
| --- | --- | --- |
| `USABLE` | Bluetooth can be used. | Go ahead. |
| `SWITCHED_OFF` | Bluetooth is turned off. | Turn it on. |
| `NO_ADAPTER` | The computer has no Bluetooth adapter. | Plug one in. |
| `NOT_ALLOWED` | This program does not have permission to use Bluetooth. | Give it permission in the system settings. |
| `AWAITING_PERMISSION` | The system is asking the user whether this program may use Bluetooth. | Answer the permission dialog. |
| `SERVICE_UNAVAILABLE` | The system's Bluetooth service is not running. | Wait a moment. It usually restarts by itself. |
| `UNSUPPORTED` | This system cannot use Bluetooth at all. | Use a different computer. |

A watcher always gives the latest value. If the availability changes and then
changes back before you ask for the next value, you do not see the change.

Each call to `watch_availability` makes a separate watcher. If two tasks need
to watch, give each one its own.

## When Bluetooth cannot be used

While Bluetooth cannot be used, opening a channel, serving or listing devices
raises
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError]. Its
[`reason`][aio_rfcomm.errors.BluetoothUnavailableError.reason] is the same
`Availability` that the watcher reports.

If Bluetooth stops being usable while you are using it, everything opened
through it ends with `BluetoothUnavailableError`: open channels, channels
still being opened, and services being served. Bytes that arrived before then
can still be received first. When Bluetooth is usable again, open your
channels and services again.

A program that should stay connected is therefore a loop: wait until Bluetooth
is usable, connect, talk, and start again when the connection ends. The
[`reconnect.py`](https://github.com/dlech/aio-rfcomm/blob/main/examples/reconnect.py)
example does exactly this.

**Next:** [Connecting](connecting.md), to connect to a device, or
[Serving](serving.md), to let devices connect to you.
