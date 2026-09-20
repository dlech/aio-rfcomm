# Prototypes

Working spikes, copied here verbatim from three separate repositories. They are
**reference material, not the library** — none of them implements the API in the
design notes, and none is imported by `aio_rfcomm`.

They are kept unmodified because each one is known to work as-is, which makes
them a reliable baseline to port from.

| file | origin | what it demonstrates | verified |
|---|---|---|---|
| `windows_winrt.py` | `aio-rfcomm` on the Windows box | `DeviceWatcher` enumeration, SDP-by-UUID, a full SDP data element codec, client and server | client path verified against the Linux server |
| `linux_bluez_server.py` | `bluez-rfcomm` on the Linux box | BlueZ `Profile1` server role, fd delivered over D-Bus, echo loop | verified, on a Python built without `AF_BLUETOOTH` |
| `macos_iobluetooth.py` | `rfcomm` on the Mac | IOBluetooth client via rubicon-objc: connect, SDP query, channel open, read/write | verified end to end against the Linux server |

## Known issues in this code

Carried over deliberately — do not copy these forward:

- **`windows_winrt.py`** — `loop.sock_connect` fails on `ProactorEventLoop`
  (`ConnectEx` is INET-only), which is why the client stalls at connect. The
  fix is to connect in a thread executor, then hand the socket to
  `asyncio.open_connection`.
- **`linux_bluez_server.py`** — `self._task` is overwritten on every
  connection, so tasks are unreferenced and never awaited; and
  `RequestDisconnection` closes an fd already owned by the socket object, which
  is a double close.
- **`macos_iobluetooth.py`** — a cancelled `Channel.write` frees both the
  `py_object` refcon and the data buffer while IOBluetooth is still using them.
  The Trio-style contract in the design notes removes this class of bug by
  declaring a cancelled `send()` fatal to the channel.

## Most reusable piece

The SDP data element encoder/decoder in `windows_winrt.py`
(`parse_data_element`, `encode_*`, `ServiceAttribute`, and the attribute and
protocol identifier enums) is genuine library code, not scaffolding. WinRT hands
back raw SDP attributes rather than a channel number, so it had to be written —
and it is equally useful on macOS. Extracting it into the package is the
obvious first real commit.
