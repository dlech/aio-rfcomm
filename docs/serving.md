# Serving

Your program can also wait for other devices to connect to it.
[`serve`][aio_rfcomm.Bluetooth.serve] publishes a service and listens for
connections, like a listening socket. Each device that connects arrives as a
request, which you accept.

```python
async def talk(request: aio_rfcomm.RfcommConnectionRequest) -> None:
    try:
        async with request.accept() as channel:
            await channel.send(b"hello\n")
            while True:
                print(f"{request.info.address}: {await channel.receive()!r}")
    except ChannelClosedError:
        print(f"{request.info.address} hung up")


async with bluetooth.serve(CHAT, name="my thing") as service:
    print(f"listening on channel {service.channel}")

    async with asyncio.TaskGroup() as group:
        async for request in service.connection_requests():
            group.create_task(talk(request))
```

Each connection runs as a task in your own task group. Catch errors that end
only one connection, such as the device hanging up, inside that connection's
task, as `talk` does. Otherwise the task group cancels every connection.

## Requests

[`connection_requests`][aio_rfcomm.RfcommService.connection_requests] gives
each connection as an
[`RfcommConnectionRequest`][aio_rfcomm.RfcommConnectionRequest].
[`info`][aio_rfcomm.RfcommConnectionRequest.info] says which device it is
from, and [`accept`][aio_rfcomm.RfcommConnectionRequest.accept] opens it as a
channel, in an `async with` block of its own.

To turn a device away, call
[`decline`][aio_rfcomm.RfcommConnectionRequest.decline] instead. The device
sees its connection fail, or open and then close at once.

Every request must be accepted or declined. A request that is neither is hung
up on when it is garbage collected, with a `ResourceWarning` that Python shows
when run with `-X dev`.

When Bluetooth stops being usable, the `async for` loop over
`connection_requests` raises
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError]. To
serve again, start a new `serve` block once Bluetooth is usable again.

## When serving stops

Leaving the `serve` block stops new connections and hangs up on requests that
are still waiting. Connections already accepted carry on until their own
`async with` blocks end.

In the example above, the task group is inside the `serve` block, so leaving
the block waits for every connection to finish. Put the task group outside to
stop new connections while the current ones carry on:

```python
async with asyncio.TaskGroup() as group:
    async with bluetooth.serve(CHAT, name="my thing") as service:
        async for request in service.connection_requests():
            if enough():
                await request.decline()
                break
            group.create_task(talk(request))
    # No new connections from here on. The current ones carry on.
```

A request has to be accepted before its `serve` block ends, which is why the
last one is declined rather than handed to a task.

## Choosing a channel

If you do not pass `channel`, the library picks a free one, and
[`service.channel`][aio_rfcomm.RfcommService.channel] says which. Devices find
the service by its UUID, so they do not need to know the number.

Pass `channel` when a device expects a fixed number:

```python
async with bluetooth.serve(CHAT, name="my thing", channel=5) as service:
    ...
```

A channel that is already in use raises
[`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError]. A number outside 1
to 30 raises `ValueError`.

On macOS, a channel can be served only once until Bluetooth is turned off and
on again. On Linux, choosing a free channel needs a Python with Bluetooth
socket support, which uv's own Python builds do not have. See
[Platforms](platforms.md) for both.

## One connection per device and channel

A device can make only one connection to a given channel. Different devices
can connect to the same service at the same time. For two connections from
the same device, serve two channels.

**Next:** [Channels](channels.md), to send and receive over an accepted
connection.
