# Lifetimes

Everything you open comes from an `async with` block, and is closed when the
block ends:

```python
async with aio_rfcomm.open_bluetooth() as bluetooth:
    async with bluetooth.open_service(DEVICE, SERIAL_PORT) as channel:
        ...
```

Using it after its block has ended is a bug, and raises
[`ScopeClosedError`][aio_rfcomm.errors.ScopeClosedError]:

```python
async with bluetooth.open_service(DEVICE, SERIAL_PORT) as channel:
    ...

await channel.send(b"hello\n")  # raises ScopeClosedError
```

## Noticing that a connection has ended

A task waiting in [`receive`][aio_rfcomm.RfcommChannel.receive] finds out
straight away when the connection ends, because `receive` raises. A task
waiting for anything else, such as keyboard input, a queue or a timer, does
not find out. It can wait forever for a device that has gone.

So keep a task receiving in the same task group, even if your program does not
expect anything from the device. When the connection ends, that task raises,
and the task group cancels the other tasks:

```python
async with asyncio.TaskGroup() as group:
    group.create_task(print_received(channel))
    group.create_task(send_typed_lines(channel))
```

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

Canceling an operation stops your program waiting for it. It does not always
stop the operation itself, because some Bluetooth operations underneath
cannot be called off. The library does what each platform allows, such as
closing the connection.

Keep these in mind before putting a timeout around anything.

**The device may still connect.** After you give up on connecting, the
connection may still complete on the device's side. Retrying straight away can
then fail, because a device allows only one connection per channel. Wait
before retrying. On Windows, the library lets the connection finish and then
hangs up, and your next open of that channel waits until it has.

**Do not use `asyncio.shield`.** A shielded call keeps running after you stop
waiting for it. A shielded `receive`, for example, still holds the channel, so
your next `receive` raises
[`BusyResourceError`][aio_rfcomm.errors.BusyResourceError].

**Canceling `send` loses nothing.** `send` queues all its bytes before it
waits. If it is canceled, the bytes are still sent, in order, unless the
connection ends first. Sending them again after a timeout sends them twice.

!!! tip "To be sure everything has stopped, leave the block"

    A timeout only stops you waiting. Leaving the `async with` block closes
    everything opened inside it.

**Next:** [Errors](errors.md), for what each error means.
