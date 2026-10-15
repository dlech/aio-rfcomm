# Serving

Most programs connect *to* something. Sometimes you want to be the thing that
is connected to — a robot waiting for a laptop, a desktop tool a handheld talks
to. [`serve`][aio_rfcomm.Bluetooth.serve] publishes a service record and
listens, the way a listening socket does: each peer that connects arrives as a
request, which your program accepts or declines.

!!! info "Linux and Windows only"

    macOS accepts the connection but never delivers it to the program. See
    [Platforms](platforms.md) for what was measured and why this is not
    something the library can work around.

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

Each conversation is a task in your own task group, so nothing runs that you
did not start, and a conversation that fails takes down whatever its task
group says it does. Catch what one conversation should survive — a peer
hanging up, say — inside that conversation.

## Requests

[`connection_requests`][aio_rfcomm.RfcommService.connection_requests] hands
over each peer as an
[`RfcommConnectionRequest`][aio_rfcomm.RfcommConnectionRequest]: who is asking,
in [`info`][aio_rfcomm.RfcommConnectionRequest.info], and two things to do
about it.

- [`accept`][aio_rfcomm.RfcommConnectionRequest.accept] takes the connection,
  as a channel in a block of its own. A peer that already hung up still gets a
  channel, whose first `receive` raises, so every ending arrives the same way.
- [`decline`][aio_rfcomm.RfcommConnectionRequest.decline] turns the peer away.
  It hangs up before it awaits anything, so it takes effect even if cancelled,
  and it never raises for a hang-up that failed.

Declining is not a refusal. On BlueZ and Windows the peer's channel appears to
be open before the request reaches your program, so the peer should see its
open succeed and the channel then end; this has not yet been measured.

A request that is neither accepted nor declined is a bug. When it is
garbage-collected it raises `ResourceWarning`, shown under `python -X dev`, and
is hung up on then, the way an unclosed socket is. Leaving the `serve` block
closes any that remain.

Each request goes to exactly one reader of `connection_requests`, and the
iterator never ends by itself. When Bluetooth stops being usable it raises
[`BluetoothUnavailableError`][aio_rfcomm.errors.BluetoothUnavailableError], and
goes on raising it: the service is over, and serving again means a new `serve`
block once Bluetooth is back.

## How long things last

Leaving the `serve` block withdraws the service and closes the requests nobody
answered. Channels already accepted belong to their own blocks, and outlive
it, as connections accepted from a listening socket do. With the task group
inside the `serve` block, as above, every conversation finishes before serving
stops. With it outside, serving can stop while the conversations finish:

```python
async with asyncio.TaskGroup() as group:
    async with bluetooth.serve(CHAT, name="my thing") as service:
        async for request in service.connection_requests():
            group.create_task(talk(request))
            if enough():
                break
    # No new peers from here on; the conversations carry on.
```

## Choosing a channel

Leave `channel` unset and the library picks a free one, which is almost always
what you want. [`service.channel`][aio_rfcomm.RfcommService.channel] then tells
you which it got, and peers find it by looking up the service UUID.

Set it explicitly when the peer cannot do a lookup and expects a fixed number:

```python
async with bluetooth.serve(CHAT, name="my thing", channel=5) as service:
    ...
```

A number outside 1 to 30 raises `ValueError`. A channel already taken — by
another program or by another service in this one — raises
[`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError].

!!! note "One Linux case where the choice has to be yours"

    Letting the library choose needs a Bluetooth socket to probe with. A Linux
    Python built without Bluetooth socket support cannot open one, and BlueZ
    offers no way to ask over D-Bus — so rather than register a channel that
    might silently collide, the library refuses and asks you to name one.
    Everywhere else, and on any normal Linux build, the automatic choice
    works.

    On such a Python a channel you name is used unchecked, and a collision is
    silent: BlueZ logs `rfcomm_bind: Address already in use` and publishes
    nothing, and peers reach whatever already holds the channel. Channel 9,
    for instance, is commonly taken by Object Push.

## One peer per device

RFCOMM allows one connection per device per server channel. Two different
devices can talk to your service at the same time quite happily; the same
device opening a second connection to the same channel cannot, and the
protocol will refuse it. If you need several conversations with one device,
you need several channels, or your own multiplexing inside the one.
