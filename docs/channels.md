# Channels

An open [`RfcommChannel`][aio_rfcomm.RfcommChannel] is a byte stream in both
directions. It comes from [connecting](connecting.md) to a device or from
[serving](serving.md) one, and behaves the same either way.

## Sending and receiving

[`send`][aio_rfcomm.RfcommChannel.send] delivers all the bytes you give it.
[`receive`][aio_rfcomm.RfcommChannel.receive] returns whatever has arrived,
blocking until something has:

```python
await channel.send(b"hello\n")

try:
    while True:
        print((await channel.receive()).decode(), end="")
except ChannelClosedError as ended:
    print(f"\nchannel over: {ended}")
```

`receive` never returns empty. Once the channel is over — the peer hanging up
included — it hands over whatever arrived before the end and then raises
[`ChannelClosedError`][aio_rfcomm.errors.ChannelClosedError], and goes on
raising the same error. That is what ends the loop above. See
[Lifetimes](lifetimes.md) for a channel used by more than one task.

One task may send while another receives — the two directions are independent
and do not block each other. Two tasks using the *same* direction is a bug,
and is reported as [`ChannelBusyError`][aio_rfcomm.errors.ChannelBusyError]
rather than left to interleave unpredictably.

## Framing

RFCOMM is a byte stream. It keeps no message boundaries, so one `send` on the
far end may arrive split across several `receive` calls, or share one with the
message after it. Anything message-shaped has to be framed by your code.

A delimiter is the simplest thing that works, and a newline is the usual one
for text protocols.
[`receive_until`][aio_rfcomm.RfcommChannel.receive_until] takes the bytes up
to one, takes the delimiter too, and returns what came before it:

```python
line = await channel.receive_until(b"\n", max_bytes=1024)
```

`max_bytes` is required, delimiter included, so that a peer that never sends
one cannot make your program buffer for ever; past it,
[`DelimiterNotFoundError`][aio_rfcomm.errors.DelimiterNotFoundError] is raised
and nothing is taken.

Binary protocols usually put a length at the front instead, which means
reading an exact number of bytes, as
[`receive_exactly`][aio_rfcomm.RfcommChannel.receive_exactly] does:

```python
async def read_message(channel) -> bytes:
    (length,) = struct.unpack("<H", await channel.receive_exactly(2))
    return await channel.receive_exactly(length)
```

Every way of receiving shares one buffer kept by the channel, so they can be
mixed freely, and whatever arrives past the end of one message is kept for the
next. Cancelling one loses nothing: what it had gathered is left for the next
call. A channel that ends part way through raises its ending, and leaves what
had arrived for `receive`.

!!! tip "Do not size reads to messages"

    `receive(max_bytes)` caps how much comes back; it does not wait for that
    much. Asking for exactly the number of bytes you want does not get you
    them — that is what `receive_exactly` is for.
