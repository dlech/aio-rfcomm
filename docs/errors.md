# Errors

Errors caused by Bluetooth or by the other device all derive from
[`RfcommError`][aio_rfcomm.errors.RfcommError], so one `except` catches them
all:

```python
try:
    ...
except aio_rfcomm.errors.RfcommError as error:
    print(f"{type(error).__name__}: {error}")
```

Errors caused by mistakes in your own code are `RuntimeError` or `ValueError`
instead, so catching `RfcommError` never hides a bug. The library's own errors
are all in `aio_rfcomm.errors`.

## Bluetooth cannot be used

[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError]
means Bluetooth cannot be used right now. Its
[`reason`][aio_rfcomm.errors.BluetoothUnavailableError.reason] says why, as an
[`Availability`][aio_rfcomm.Availability]. See [Bluetooth](bluetooth.md) for
what each reason means and what the user can do about it.

## Connecting

| Error | Means |
| --- | --- |
| [`DeviceNotFoundError`][aio_rfcomm.errors.DeviceNotFoundError] | The device did not answer. It may be switched off or out of range. Its [`address`][aio_rfcomm.errors.DeviceNotFoundError.address] says which device. |
| [`ServiceNotFoundError`][aio_rfcomm.errors.ServiceNotFoundError] | The device does not offer that service, or refused that channel number. See also [Platforms](platforms.md). |
| [`ConnectionFailedError`][aio_rfcomm.errors.ConnectionFailedError] | The channel could not be opened, or the service could not be published. |

## While connected

[`ChannelClosedError`][aio_rfcomm.errors.ChannelClosedError] means the
connection has ended: the other device hung up, or the connection to it was
lost. To find out whether the device is still there, open a channel again.
[`DeviceNotFoundError`][aio_rfcomm.errors.DeviceNotFoundError] then means it
is switched off or out of range.

Once a connection has ended, and any bytes that arrived before the end have
been received, every call to [`receive`][aio_rfcomm.RfcommChannel.receive]
and [`send`][aio_rfcomm.RfcommChannel.send] raises the same error. If the
connection ended because Bluetooth stopped being usable, they raise
`BluetoothUnavailableError` instead.

[`DelimiterNotFoundError`][aio_rfcomm.errors.DelimiterNotFoundError] means
[`receive_until`][aio_rfcomm.RfcommChannel.receive_until] found no delimiter
within `max_bytes`.

## Limits

| Error | Means |
| --- | --- |
| [`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError] | The channel you asked to serve on is already in use. |
| [`UnsupportedOperationError`][aio_rfcomm.errors.UnsupportedOperationError] | This platform cannot do that, or not right now. The message says what to do instead. |

## Mistakes in your code

These are `RuntimeError`, not `RfcommError`.

| Error | Means |
| --- | --- |
| `RuntimeError` | A connection request was accepted or declined twice. |
| [`BusyResourceError`][aio_rfcomm.errors.BusyResourceError] | Two tasks received at the same time on one channel, or two tasks sent. |
| [`ScopeClosedError`][aio_rfcomm.errors.ScopeClosedError] | Something was used after its `async with` block ended. |
| [`BluetoothAlreadyOpenError`][aio_rfcomm.errors.BluetoothAlreadyOpenError] | Bluetooth was opened while it was already open. |

A channel number outside 1 to 30, or a string that is not a Bluetooth address,
raises `ValueError`.

**Next:** [Platforms](platforms.md), for where Linux, macOS and Windows differ.
