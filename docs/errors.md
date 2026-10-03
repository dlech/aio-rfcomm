# Errors

Every error this library raises derives from
[`RfcommError`][aio_rfcomm.errors.RfcommError], so one `except` catches the lot
if that is all you need:

```python
try:
    ...
except aio_rfcomm.errors.RfcommError as error:
    print(f"{type(error).__name__}: {error}")
```

They come from `aio_rfcomm.errors`, and divide into four groups.

## Getting started

| Error | Means |
| --- | --- |
| [`UnsupportedPlatformError`][aio_rfcomm.errors.UnsupportedPlatformError] | This *operating system* has no backend — it is not Linux, macOS or Windows. It says nothing about the machine's Bluetooth: a supported OS that cannot do some particular thing raises [`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] instead, and one with no usable radio raises `AdapterNotFoundError` or `AdapterOffError`. |
| [`PermissionDeniedError`][aio_rfcomm.errors.PermissionDeniedError] | The OS refused access to Bluetooth. On macOS, the user said no — or the program never declared why it wants Bluetooth. |
| [`AdapterNotFoundError`][aio_rfcomm.errors.AdapterNotFoundError] | No adapter matched, or the machine has none. |
| [`AdapterOffError`][aio_rfcomm.errors.AdapterOffError] | There is an adapter, and it is switched off. |

The last two are deliberately distinct, as are `AdapterOffError` and
`PermissionDeniedError`: "turn Bluetooth on" and "allow this program to use
Bluetooth" are different things to tell a user, and a program that cannot tell
them apart tells them the wrong one.

## Connecting

| Error | Means |
| --- | --- |
| [`DeviceNotFoundError`][aio_rfcomm.errors.DeviceNotFoundError] | The device did not answer. Switched off, out of range, or never there. |
| [`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] | The device does not offer that service — or its record is not cached. See [Platforms](platforms.md). |
| [`ConnectionFailedError`][aio_rfcomm.errors.ConnectionFailedError] | The channel could not be opened, after any retries. |

## Using a channel

[`ChannelClosedError`][aio_rfcomm.errors.ChannelClosedError] carries a
[`reason`][aio_rfcomm.errors.CloseReason]: `PEER_CLOSED`, `LINK_LOST` or
`ADAPTER_OFF`.

```python
except ChannelClosedError as error:
    if error.reason is CloseReason.PEER_CLOSED:
        print("the other end hung up")
    else:
        print(f"lost the channel: {error}")
```

There is an asymmetry worth knowing:

- [`receive`][aio_rfcomm.RfcommChannel.receive] raises it only for the
  *abnormal* reasons. An orderly hang-up by the peer is not an error there —
  the stream simply ends and it returns `b""`.
- [`fail_when_gone`][aio_rfcomm.RfcommChannel.fail_when_gone] raises it for
  every reason, because a caller who asked to stop when the channel closes
  meant all of them.

[`ChannelBrokenError`][aio_rfcomm.errors.ChannelBrokenError] follows a
cancelled `send`; see [Lifetimes](lifetimes.md).
[`ChannelBusyError`][aio_rfcomm.errors.ChannelBusyError] means two tasks used
the same direction of one channel at once, which is reported as your bug rather
than tolerated as a race.

## Misuse and limits

| Error | Means |
| --- | --- |
| [`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError] | The RFCOMM channel you asked to serve on is already taken. |
| [`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] | This platform has a backend but cannot do that particular thing. The message says what would make it work. |
| [`ScopeClosedError`][aio_rfcomm.errors.ScopeClosedError] | A handle was used after the block that produced it ended. |
