# Lifetimes

Everything you open comes from an `async with` block, and is closed when the
block ends. Using it after the block has ended raises
[`ScopeClosedError`][aio_rfcomm.errors.ScopeClosedError].

```python
async with aio_rfcomm.open_bluetooth() as bluetooth:
    async with bluetooth.open_service(DEVICE, SERIAL_PORT) as channel:
        ...
```

## Noticing that a connection has ended

A task waiting in [`receive`][aio_rfcomm.RfcommChannel.receive] finds out
straight away when the connection ends, because `receive` raises. A task
waiting for anything else, such as keyboard input, a queue or a timer, does
not find out. It can wait forever for a device that has gone.

So keep a task receiving in the same task group. When the connection ends,
that task raises, and the task group cancels the other tasks:

```python
async with asyncio.TaskGroup() as group:
    group.create_task(print_received(channel))
    group.create_task(send_typed_lines(channel))
```

A program that only sends should still keep a task receiving, to notice the
end.

A task group raises an `ExceptionGroup`, even when only one task failed. Catch
the group in the function that made the task group, and raise the channel's
error by itself:

```python
try:
    async with asyncio.TaskGroup() as group:
        ...
except ExceptionGroup as failed:
    endings, others = failed.split(ChannelClosedError)
    if endings is not None and others is None:
        raise endings.exceptions[0] from None
    raise
```

Anything else in the group is a bug, so it goes on up unchanged.

When Bluetooth stops being usable, `receive` and `send` raise
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError], so
the same task group notices that too.

## Cancellation

Cancelling an operation stops your program waiting for it. It does not always
stop the operation itself, because some Bluetooth operations underneath
cannot be called off. The library does what each platform allows, such as
closing the connection.

Keep these in mind before putting a timeout around anything.

**The device may still connect.** After you give up on connecting, the
connection may still complete on the device's side. Retrying straight away can
then fail, because a device allows only one connection per channel. Wait
before retrying.

**Do not use `asyncio.shield`.** A shielded call keeps running after you stop
waiting for it. A shielded `receive`, for example, still holds the channel, so
your next `receive` raises
[`BusyResourceError`][aio_rfcomm.errors.BusyResourceError].

**Cancelling `send` loses nothing.** `send` queues all its bytes before it
waits. If it is cancelled, the bytes are still sent, in order, unless the
connection ends first. Sending them again after a timeout sends them twice.

!!! tip "To be sure everything has stopped, leave the block"

    A timeout only stops you waiting. Leaving the `async with` block closes
    everything opened inside it.
