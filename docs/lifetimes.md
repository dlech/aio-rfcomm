# Lifetimes

Every handle in this library comes out of an `async with` block and stops
working when that block ends. There is no `close()` to forget and no way to
use a connection after it is gone — a handle used outside its block raises
[`ScopeClosedError`][aio_rfcomm.errors.ScopeClosedError] rather than doing
something undefined.

The blocks nest the way the objects depend on each other:

```python
async with aio_rfcomm.open_adapter() as adapter:  # the radio
    device = adapter.use_device(address)  # just a name; no resource
    async with device.open_service(SERVICE) as channel:  # the connection
        ...
```

## Waiting is not the same as noticing

The hard part of a connection is not opening it. It is what happens when it
dies while your program is busy doing something else.

A task blocked on `receive` finds out immediately: the call raises. A task
blocked on anything *else* — reading the keyboard, waiting on a queue, sleeping
— never finds out at all, and waits forever for a peer that hung up ten minutes
ago. This is the failure that makes ad-hoc connection code hang, and no amount
of care inside the receiving task fixes it.

[`fail_when_gone`][aio_rfcomm.RfcommChannel.fail_when_gone] is the answer. It
wraps a block and raises [`ChannelClosedError`][aio_rfcomm.errors.ChannelClosedError]
*inside* it the moment the channel goes:

```python
async with channel.fail_when_gone(), asyncio.TaskGroup() as group:
    group.create_task(read_from_peer(channel))
    group.create_task(read_from_keyboard(channel))
```

The keyboard task is not watching the channel and does not have to. When the
peer hangs up, the block it is running in fails, and the task group cancels it.

Adapters have the same method:
[`RfcommAdapter.fail_when_gone`][aio_rfcomm.RfcommAdapter.fail_when_gone]
raises when the radio is switched off.

[`RfcommService.fail_when_gone`][aio_rfcomm.RfcommService.fail_when_gone]
watches exactly the same thing — the adapter underneath, not the service — and
exists only so that a `serve` block, which already has the service in hand,
does not have to reach back for the adapter. Either one will do, and
`adapter.fail_when_gone()` is the clearer of the two if both are in scope.

!!! note "One error, not a group"

    `fail_when_gone` runs a task group internally, but reports its lone failure
    as itself. Callers catch `ChannelClosedError`, not `except*`.

## Asking instead of failing

Where you want to *wait* for the end rather than be interrupted by it,
[`wait_until_gone`][aio_rfcomm.RfcommChannel.wait_until_gone] returns the
[`CloseReason`][aio_rfcomm.errors.CloseReason] instead of raising:

```python
reason = await channel.wait_until_gone()
print(f"finished: {reason.value}")
```

## Cancellation and `send`

A cancelled [`send`][aio_rfcomm.RfcommChannel.send] leaves an unknown number of
bytes on the wire. There is no way to find out how many, and no way to resume,
so the channel cannot be trusted afterwards: every later call raises
[`ChannelBrokenError`][aio_rfcomm.errors.ChannelBrokenError]. Close the channel
and open a new one.

This is deliberate. Silently continuing would corrupt whatever protocol you are
speaking, at a point far from the cancellation that caused it.
