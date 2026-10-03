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

while data := await channel.receive():
    print(data.decode(), end="")
```

Once the peer hangs up, `receive` returns `b""` — and keeps returning it, so
it is an end rather than an error. That is what ends the loop above. If you
would rather be interrupted than check a return value, see
[Lifetimes](lifetimes.md).

One task may send while another receives — the two directions are independent
and do not block each other. Two tasks using the *same* direction is a bug,
and is reported as [`ChannelBusyError`][aio_rfcomm.errors.ChannelBusyError]
rather than left to interleave unpredictably.

## Framing

RFCOMM is a byte stream. It keeps no message boundaries, so one `send` on the
far end may arrive split across several `receive` calls, or share one with the
message after it. Anything message-shaped has to be framed by your code.

A delimiter is the simplest thing that works, and a newline is the usual one
for text protocols:

```python
def split_lines(buffer: bytearray, arrived: bytes) -> Iterator[str]:
    buffer += arrived
    while (end := buffer.find(b"\n")) >= 0:
        line = bytes(buffer[:end])
        del buffer[: end + 1]
        yield line.decode("utf-8", errors="replace")
```

Binary protocols usually put a length at the front instead, which means
reading an exact number of bytes:

```python
async def read_exactly(channel, count: int, buffer: bytearray) -> bytes:
    while len(buffer) < count:
        arrived = await channel.receive()
        if not arrived:
            raise EOFError(f"wanted {count} bytes, stream ended with {len(buffer)}")
        buffer += arrived
    taken = bytes(buffer[:count])
    del buffer[:count]
    return taken


async def read_message(channel, buffer: bytearray) -> bytes:
    (length,) = struct.unpack("<H", await read_exactly(channel, 2, buffer))
    return await read_exactly(channel, length, buffer)
```

The buffer has to outlive the call in both cases: whatever arrived past the
end of one message is the start of the next, and dropping it corrupts
everything that follows.

!!! tip "Do not size reads to messages"

    `receive(max_bytes)` caps how much comes back; it does not wait for that
    much. Asking for exactly the number of bytes you want does not get you
    them, which is why the loop above exists.
