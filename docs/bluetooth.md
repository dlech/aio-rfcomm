# Bluetooth

## Opening it

Everything goes through one object that stands for Bluetooth on this
machine. [`open_bluetooth`][aio_rfcomm.open_bluetooth] opens it, and leaving
the block closes everything opened through it:

```python
async with aio_rfcomm.open_bluetooth() as bluetooth:
    ...
```

It waits until the operating system has said whether Bluetooth can be used —
on macOS that includes starting a helper process — and then succeeds, whatever
the answer. Bluetooth may be switched off, or missing altogether; that is
something to report, not a reason the program cannot start. Only the library
itself failing to start raises here: on macOS, a helper process that cannot be
started at all.

There is one per process. Code that needs Bluetooth takes the object as an
argument rather than opening its own, and opening a second while the first is
open raises
[`BluetoothAlreadyOpenError`][aio_rfcomm.errors.BluetoothAlreadyOpenError].

The object lasts for as long as its block does. It outlives Bluetooth being
switched off and on and the system's Bluetooth service restarting; what was
opened through it does not, and each of those says why it ended.

## Is it usable?

[`watch_availability`][aio_rfcomm.Bluetooth.watch_availability] says whether
Bluetooth can be used, and if not why not, as an
[`Availability`][aio_rfcomm.Availability]: now, and again each time that
changes.

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
| `USABLE` | Bluetooth can be used. | — |
| `SWITCHED_OFF` | Bluetooth is switched off. | Turn it on; on Linux, possibly unblock it (rfkill, airplane mode). |
| `NO_ADAPTER` | The machine has no Bluetooth adapter. | Plug one in. |
| `NOT_ALLOWED` | This program may not use Bluetooth. | Usually, allow it in the system's settings. |
| `SERVICE_UNAVAILABLE` | The system's Bluetooth service is not running. | Usually wait. |
| `UNSUPPORTED` | This system has no Bluetooth at all. | Nothing. |

The first value comes at once. After that, each value is the availability at
the time, whenever it differs from the last one that watcher returned; a
change and back between two reads returns nothing, because whatever that
change ended raised its own error. On Windows, availability is so far read only
when Bluetooth is opened; see [Platforms](platforms.md). Each call makes an independent watcher, so
two tasks each watching both see every change — give each task its own.

The watcher is an ordinary iterator object, not an async generator, so leaving
a loop over it early leaves nothing behind.

## When it is not

Anything attempted while Bluetooth cannot be used — opening a channel,
serving, listing devices — raises
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError] at
once, with the same [`reason`][aio_rfcomm.errors.BluetoothUnavailableError.reason]
the watcher would report.

When Bluetooth stops being usable, for whatever reason, everything opened
through it ends with that error: every open channel, every open in progress,
and every published service. A channel still hands over whatever arrived
before the end first. Bluetooth coming back does not bring any of them back;
open them again.

So the shape of a program that stays connected is a loop: wait until
Bluetooth is usable, open, talk, and go round again when it ends. The
`reconnect.py` example in the repository is exactly that.
