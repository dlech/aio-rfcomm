# Troubleshooting

When something goes wrong, start with the library's own log. If that does not
explain it, look at the system's Bluetooth logs below. Some problems are bugs
in the operating system, and [Known bugs](known-bugs.md) lists the ones we
know about.

## Common problems

| What you see | Where to look |
| --- | --- |
| `open_bluetooth` does not return on macOS | macOS is waiting for the user to answer the [permission](platforms.md#permission) dialog. |
| `ServiceNotFoundError`, but the device offers the service | The system's stored copy of the device's services is out of date. See [macOS](platforms.md#when-a-devices-services-change) or [Linux](platforms.md#when-a-devices-services-change_1). |
| `ChannelInUseError` from `serve` on macOS | [Each channel is served once](platforms.md#each-channel-is-served-once). |
| `UnsupportedOperationError` on Linux | Python lacks [Bluetooth sockets](platforms.md#bluetooth-sockets). |

## The library's log

The library logs through Python's `logging`, under the name `aio_rfcomm`.
Warnings show without any setup. To see everything it does, turn on debug
logging:

```python
import logging

logging.basicConfig()
logging.getLogger("aio_rfcomm").setLevel(logging.DEBUG)
```

## macOS

### The Bluetooth log

macOS logs what its Bluetooth service does. To watch it while your program
runs:

```sh
/usr/bin/log stream --level debug --style compact --predicate 'subsystem == "com.apple.bluetooth"'
```

- Type `/usr/bin/log` in full. In zsh, `log` on its own is a different
  command.
- Keep `--level debug`. Without it, the useful lines are missing.
- Your program's lines come from the library's helper process, not from your
  program. With python.org's Python it is called `Python`. In an app bundled
  with PyInstaller it is called `helper`.

If the problem has already happened, `/usr/bin/log show` finds the last few
minutes:

```sh
/usr/bin/log show --last 5m --debug --info --style compact --predicate 'subsystem == "com.apple.bluetooth"'
```

`--start` takes local time, such as `'2026-10-15 14:30:00'`, without
fractions of a second.

These lines say what happened. They were seen on macOS 26.6.2.

| Line | Means |
| --- | --- |
| `Inform delegate (rfcommChannelOpenComplete:Success)` | A channel opened. |
| `RFCOMM Channel connect failure 913` | The device refused the channel. |
| `Inform delegate (rfcommChannelClosed)` | A channel closed. |
| `Bluetooth error - restarting` | The Bluetooth service restarted. |

### Apple's Bluetooth logging profile

PacketLogger and a bug report to Apple both need this profile. It turns on
Bluetooth's full logging.

1. Go to <https://developer.apple.com/bug-reporting/profiles-and-logs/>, find
   **Bluetooth for macOS**, and download its profile. Apple asks you to sign
   in with your Apple Account.
2. Open the downloaded `Bluetooth_macOS.mobileconfig`. macOS adds it to
   System Settings but does not install it yet.
3. Open **System Settings > General > Device Management**, double-click
   **Bluetooth Logging for macOS**, click **Install**, and enter your password
   if asked.

The profile removes itself three days after it is installed. Install it again
when you need it again.

### Every Bluetooth packet

PacketLogger records every packet between the Mac and its Bluetooth radio. It
needs the [logging profile](#apples-bluetooth-logging-profile) installed
first.

PacketLogger is in Apple's Additional Tools for Xcode, from
<https://developer.apple.com/download/all/>. Save the trace to attach it to a
bug report. Wireshark opens PacketLogger's saved `.pklg` files too.

### Reporting a bug to Apple

Apple asks for more than the log above. Install the
[logging profile](#apples-bluetooth-logging-profile), make the problem happen,
and run `sudo sysdiagnose`. Attach what it writes to your report in Feedback
Assistant.

## Linux

The library talks to BlueZ, the Linux Bluetooth service. These show what
BlueZ and the radio are doing:

- `bluetoothctl show` says whether the radio is powered.
- `bluetoothctl info 00:1A:7D:DA:71:0C` lists the device's services as BlueZ
  has stored them.
- `journalctl -u bluetooth` is BlueZ's own log.
- `dbus-monitor --system "sender='org.bluez'"` shows every change BlueZ
  reports.
- `sudo btmon` records every Bluetooth packet. `sudo btmon -w trace.btsnoop`
  saves them to a file.
- Wireshark can capture the same packets, on its `bluetooth0` interface for
  the first adapter. Members of the `wireshark` group can capture without
  sudo, on distributions that set that group up. Wireshark also opens the
  files `btmon` saves.

### BlueZ's debug log

BlueZ logs much more when it is started with `-d`. To start it that way,
open an override for its service:

```sh
sudo systemctl edit bluetooth
```

And add:

```ini
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd -d
```

- Keep the empty `ExecStart=` line. It clears the original command, which
  systemd would otherwise refuse to replace.
- Check the path with `systemctl cat bluetooth`, since it differs between
  distributions. If an override is already there, keep its options, such as
  `-E`, and add `-d` to them.

Then restart BlueZ, which disconnects every Bluetooth device, and watch its
log:

```sh
sudo systemctl restart bluetooth
journalctl -u bluetooth -f
```

To turn it off, edit the override again, remove `-d`, and restart BlueZ.

## Windows

### Every Bluetooth packet

Microsoft's Bluetooth Virtual Sniffer sends every packet between Windows and
its Bluetooth radio to Wireshark. It is part of Microsoft's Bluetooth Test
Platform (BTP) package.

1. Install Wireshark, from <https://www.wireshark.org/>.
2. Install the BTP package, from
   <https://learn.microsoft.com/windows-hardware/drivers/bluetooth/testing-btp-setup-package>.
3. In a terminal run as administrator, start the sniffer. Wireshark opens and
   starts capturing.

    ```bat
    C:\BTP\v1.14.0\x86\btvs.exe -Mode Wireshark
    ```

    Put in the version you installed. The sniffer comes for x86 only, and
    Windows on Arm runs it as well.

4. Run your program in an ordinary terminal, and stop the capture in
   Wireshark when you are done.

**Next:** [Known bugs](known-bugs.md), for problems in the operating systems.
