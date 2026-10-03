# Serving

Most programs connect *to* something. Sometimes you want to be the thing that
is connected to — a robot waiting for a laptop, a desktop tool a handheld talks
to. [`serve`][aio_rfcomm.RfcommAdapter.serve] publishes a service record and
runs a handler for every peer that arrives.

!!! info "Linux and Windows only"

    macOS accepts the connection but never delivers it to the program. See
    [Platforms](platforms.md) for what was measured and why this is not
    something the library can work around.

```python
async def talk(
    peer: aio_rfcomm.RfcommDeviceInfo, channel: aio_rfcomm.RfcommChannel
) -> None:
    await channel.send(b"hello\n")

    while data := await channel.receive():
        print(f"{peer.address}: {data!r}")


async with adapter.serve(CHAT, talk, name="my thing") as service:
    print(f"listening on channel {service.channel}")

    async with service.fail_when_gone():
        await asyncio.Event().wait()
```

`asyncio.Event().wait()` is just a wait that never ends: a server has nothing
else to do, and waiting on an event nobody sets costs nothing.
[`fail_when_gone`][aio_rfcomm.RfcommService.fail_when_gone] is what makes it
safe, by raising *inside* that block if the adapter is switched off — otherwise
the program would go on waiting just as patiently with no radio underneath it.
See [Lifetimes](lifetimes.md).

## The handler

The handler is called once per peer, with the peer's description and its open
channel. The channel is closed when the handler returns, so there is nothing
to clean up. Handlers run concurrently — one per peer — and all of them stop
when the `serve` block ends.

An exception escaping a handler has nobody to be raised to: the peer is gone
and the `serve` block is elsewhere, quite possibly waiting forever on purpose.
Rather than tear down the whole service because one peer misbehaved, the
library hands the exception to the event loop's exception handler, the same
place an unhandled task exception goes.

## Choosing a channel

Leave `channel` unset and the library picks a free one, which is almost always
what you want. [`service.channel`][aio_rfcomm.RfcommService.channel] then tells
you which it got, and peers find it by looking up the service UUID.

Set it explicitly when the peer cannot do a lookup and expects a fixed number:

```python
async with adapter.serve(CHAT, talk, name="my thing", channel=5) as service:
    ...
```

A channel already taken — by another program or by another service in this one
— raises [`ChannelInUseError`][aio_rfcomm.errors.ChannelInUseError].

!!! note "One Linux case where the choice has to be yours"

    Letting the library choose needs a Bluetooth socket to probe with. A Linux
    Python built without Bluetooth socket support cannot open one, and BlueZ
    offers no way to ask over D-Bus — so rather than register a channel that
    might silently collide, the library refuses and asks you to name one.
    Everywhere else, and on any normal Linux build, the automatic choice
    works.

## Waiting

`serve` returns a context manager, and the service lives for as long as that
block does. How you wait inside it is your business, but waiting on an
`asyncio.Event` nothing ever sets costs nothing:

```python
async with service.fail_when_gone():
    await asyncio.Event().wait()
```

A loop that sleeps and re-checks achieves exactly the same nothing, at the
price of a wakeup every time round. The
[`fail_when_gone`][aio_rfcomm.RfcommService.fail_when_gone] wrapper is what
turns the adapter being switched off into an exception rather than a hang —
see [Lifetimes](lifetimes.md).

## One peer per device

RFCOMM allows one connection per device per server channel. Two different
devices can talk to your service at the same time quite happily; the same
device opening a second connection to the same channel cannot, and the
protocol will refuse it. If you need several conversations with one device,
you need several channels, or your own multiplexing inside the one.
