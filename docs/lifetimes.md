# Lifetimes

Every handle in this library comes out of an `async with` block and stops
working when that block ends. There is no `close()` to forget and no way to
use a connection after it is gone — a handle used outside its block raises
[`ScopeClosedError`][aio_rfcomm.errors.ScopeClosedError] rather than doing
something undefined.

The blocks nest the way the objects depend on each other:

```python
async with aio_rfcomm.open_bluetooth() as bluetooth:  # Bluetooth on this machine
    async with bluetooth.open_service(device, SERVICE) as channel:  # the connection
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

The answer is a task group with a task receiving in it. Every ending is raised
by [`receive`][aio_rfcomm.RfcommChannel.receive], the peer hanging up
included, and a task group cancels the rest of its tasks when one fails:

```python
async with asyncio.TaskGroup() as group:
    group.create_task(read_from_peer(channel))
    group.create_task(read_from_keyboard(channel))
```

The keyboard task is not watching the channel and does not have to. When the
peer hangs up, the reading task raises, and the task group cancels the
keyboard task. A program that only sends keeps a task receiving for the same
reason.

A task group reports what its tasks raised as an `ExceptionGroup`, even when
there is only one, and the reader and a writer may both have seen the end. The
task group is only how your function does two things at once, so that function
is the place to turn a group made only of the channel's ending back into the
ending itself. The chat example's `unwrap_ending` does exactly that, and lets
anything else go on up untouched.

Bluetooth going away ends a channel the same way, with
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError]
from `receive` and `send`, so the same task group notices it. See
[Bluetooth](bluetooth.md).

## Cancellation

Cancelling an operation stops *you* waiting for it. It does not stop the
operation.

Most of what this library sits on cannot be called off. There is no way to
abort an SDP query on macOS, and none to withdraw a D-Bus method call BlueZ
has already begun. What the library does on cancellation is whatever the
platform allows — close the socket, tell the macOS helper process to give up,
close a channel that finished opening after you had stopped waiting — and no
more than that.

How long it keeps running depends on which part you gave up on. Reaching the
device is bounded by the radio: a peer that never answers ends in the page
timeout, so an abandoned connect finishes by itself within seconds and would
have told you [`DeviceNotFoundError`][aio_rfcomm.errors.DeviceNotFoundError]
had you waited. Opening a channel has no such bound, and on macOS it can fail
in a way that is never reported at all. So what you have given up is usually
the answer rather than the work, but not always: some of it may simply never
finish.

Three consequences are worth knowing before you put a deadline around anything.

**Assume the peer saw it.** A connect you gave up on may still arrive. RFCOMM
allows one connection per device per channel, so retrying straight away can
collide with your own abandoned attempt and fail in a way that looks like the
device being busy. Waiting out the platform's own limit before trying again is
the remedy, and the only one: nothing you can close on this side will make the
peer forget a connection it has already accepted.

**Cancelling is not always prompt.** On Windows the bindings wait for the
underlying operation to finish before delivering the cancellation, so a
cancelled service lookup unwinds when the lookup ends rather than when you asked.
That is the safe behaviour — nothing is left half-done — but it is not an escape
hatch from a slow device.

**Do not reach for `asyncio.shield`.** The work carries on regardless, as
above — what shielding costs you is the tidying up. The library's cleanup runs
*on* the cancellation path: closing the socket, telling the macOS helper to
give up, closing a channel that finished opening after you had stopped
waiting. Shield stops the cancellation reaching that code at all, so none of
it happens.

!!! tip "Bound work with a scope, not a deadline"

    When to give up is mostly yours to decide, with `asyncio.timeout`. The
    library sets a bound of its own only in the few places where the platform
    would otherwise leave it waiting for ever. But a deadline only bounds your
    waiting. Leaving the `async with` block is what bounds the *work*: it
    tears down everything underneath, including, on macOS, the helper process
    that owns the framework's objects. Where a program needs a point at which
    everything has definitely stopped, that is the construct to use.

### Cancelling a send

Cancelling a [`send`][aio_rfcomm.RfcommChannel.send] stops only the waiting.
Everything passed to it is queued before it waits, so all of it still goes,
in order, unless the channel ends first. A send is never cut short part way
through, and a deadline around one followed by a retry sends the data twice
rather than corrupting it.
