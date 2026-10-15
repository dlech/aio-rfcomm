# Channels

An [`RfcommChannel`][aio_rfcomm.RfcommChannel] carries bytes in both
directions. You get one by [connecting](connecting.md) to a device or by
[accepting](serving.md) a connection. Both kinds work the same way.

## Sending and receiving

[`send`][aio_rfcomm.RfcommChannel.send] sends all the bytes you give it. It
returns once they are on their way, which does not mean the other device has
them yet.
[`receive`][aio_rfcomm.RfcommChannel.receive] waits until some bytes arrive and
returns them:

```python
await channel.send(b"hello\n")

try:
    while True:
        print((await channel.receive()).decode(), end="")
except ChannelClosedError as error:
    print(f"\nthe connection ended: {error}")
```

`receive` never returns empty bytes. When the connection ends, `receive` first
returns any bytes that arrived before the end. After that, every call to
`receive` raises [`ChannelClosedError`][aio_rfcomm.errors.ChannelClosedError],
and so does every call to `send`. That is what ends the loop above.

One task can send while another task receives. Two tasks receiving at the same
time, or two tasks sending, is a bug, and raises
[`BusyResourceError`][aio_rfcomm.errors.BusyResourceError].

## Reading messages

RFCOMM carries a stream of bytes, not separate messages. One `send` on the
other end may arrive in several pieces, or joined to the next message. To read
messages, your program has to know where each one ends.

For text, ending each message with a newline is the simplest way.
[`receive_until`][aio_rfcomm.RfcommChannel.receive_until] reads up to a
delimiter and returns the bytes before it:

```python
line = await channel.receive_until(b"\n", max_bytes=1024)
```

The delimiter is read too, but not returned. `max_bytes` is the most it reads,
delimiter included. It is required, so that a device that never sends a
delimiter cannot make your program use more and more memory. If no delimiter
arrives within `max_bytes`, `receive_until` raises
[`DelimiterNotFoundError`][aio_rfcomm.errors.DelimiterNotFoundError] and leaves
the bytes for the next call.

For binary data, a common way is to send each message's length first.
[`receive_exactly`][aio_rfcomm.RfcommChannel.receive_exactly] reads an exact
number of bytes:

```python
async def read_message(channel) -> bytes:
    (length,) = struct.unpack("<H", await channel.receive_exactly(2))
    return await channel.receive_exactly(length)
```

You can mix `receive`, `receive_until` and `receive_exactly`. Bytes that one
call does not use are kept for the next, and cancelling a call loses no bytes.
If the connection ends partway through a message, `receive_until` and
`receive_exactly` raise `ChannelClosedError`, and `receive` then returns the
part that arrived.

!!! tip "`receive(max_bytes)` does not wait for that many"

    It returns at most `max_bytes`, as soon as anything has arrived. Use
    `receive_exactly` when you need an exact number of bytes.
