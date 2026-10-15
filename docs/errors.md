# Errors

Every error that Bluetooth or the peer causes derives from
[`RfcommError`][aio_rfcomm.errors.RfcommError], so one `except` catches the lot
if that is all you need:

```python
try:
    ...
except aio_rfcomm.errors.RfcommError as error:
    print(f"{type(error).__name__}: {error}")
```

Mistakes in your own code are not `RfcommError`: they are `RuntimeError`
subclasses (see the last group), so catching `RfcommError` never hides a bug.

They come from `aio_rfcomm.errors`, and divide into five groups.

## Bluetooth itself

[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError]
means Bluetooth cannot be used, and its
[`reason`][aio_rfcomm.errors.BluetoothUnavailableError.reason] says why, as an
[`Availability`][aio_rfcomm.Availability]:

| Reason | Means |
| --- | --- |
| `SWITCHED_OFF` | Bluetooth is switched off. |
| `NO_ADAPTER` | The machine has no Bluetooth adapter. |
| `NOT_ALLOWED` | This program may not use Bluetooth. On macOS, the user said no — or the program never declared why it wants Bluetooth. |
| `SERVICE_UNAVAILABLE` | The system's Bluetooth service is not running. |
| `UNSUPPORTED` | This system has no Bluetooth at all. |

One error with a reason, rather than one class per reason, so that a `match`
over the reasons ending in `assert_never` is checked as exhaustive.
`SWITCHED_OFF` and `NOT_ALLOWED` are deliberately distinct: "turn Bluetooth
on" and "allow this program to use Bluetooth" are different things to tell a
user, and a program that cannot tell them apart tells them the wrong one.

## Connecting

| Error | Means |
| --- | --- |
| [`DeviceNotFoundError`][aio_rfcomm.errors.DeviceNotFoundError] | The device did not answer. Switched off, out of range, or never there. |
| [`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] | The device does not offer that service — or its record is not cached. See [Platforms](platforms.md). |
| [`ConnectionFailedError`][aio_rfcomm.errors.ConnectionFailedError] | The channel could not be opened, after any retries. |

## Using a channel

[`ChannelClosedError`][aio_rfcomm.errors.ChannelClosedError] carries a
[`reason`][aio_rfcomm.errors.CloseReason]: `PEER_CLOSED` or `LINK_LOST`.

```python
except ChannelClosedError as error:
    match error.reason:
        case CloseReason.PEER_CLOSED:
            print("the other end hung up")
        case CloseReason.LINK_LOST:
            print("lost the connection")
        case _:
            assert_never(error.reason)
```

Every ending is raised, the peer hanging up included. Once a channel is over,
[`receive`][aio_rfcomm.RfcommChannel.receive] — after handing over whatever
arrived before the end — and [`send`][aio_rfcomm.RfcommChannel.send] raise it,
with the same reason every time, so a reader and a writer that both notice
agree on what happened.

A channel that Bluetooth going away ended raises
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError]
instead, in the same way: the connection did not fail, Bluetooth did.

## Limits

| Error | Means |
| --- | --- |
| [`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError] | The RFCOMM channel you asked to serve on is already taken. |
| [`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] | This platform cannot do that particular thing. The message says what would make it work. |

## Mistakes in the calling code

These are `RuntimeError`, not `RfcommError`.

| Error | Means |
| --- | --- |
| [`ChannelBusyError`][aio_rfcomm.errors.ChannelBusyError] | Two tasks used the same direction of one channel at once, which is reported as your bug rather than tolerated as a race. |
| [`ChannelBrokenError`][aio_rfcomm.errors.ChannelBrokenError] | A channel was sent on after an earlier send on it was cancelled; see [Lifetimes](lifetimes.md). |
| [`ScopeClosedError`][aio_rfcomm.errors.ScopeClosedError] | A handle was used after the block that produced it ended. |
| [`BluetoothAlreadyOpenError`][aio_rfcomm.errors.BluetoothAlreadyOpenError] | Bluetooth was opened while it was already open in this process. |

A channel number outside 1 to 30, or a string that is not a Bluetooth address,
raises `ValueError` where it is passed in.
