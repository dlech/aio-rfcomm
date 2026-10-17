# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `open_bluetooth()` opens Bluetooth on Linux (BlueZ), macOS (IOBluetooth) and
  Windows (WinRT), with the same API on all three. It succeeds even when
  Bluetooth is turned off or missing. Everything opened through it is closed
  when its `async with` block ends.
- `Bluetooth.watch_availability()` gives the current `Availability`, and then a
  new value each time it changes.
- `Bluetooth.list_known_devices()` lists the paired devices, optionally only
  those that offer a given service.
- `Bluetooth.open_service()` opens a channel to a service on a device, and looks
  up the channel number. `Bluetooth.open_channel()` opens a known channel
  number directly.
- `Bluetooth.serve()` publishes a service. Its `connection_requests()` gives
  each incoming connection, to `accept()` or `decline()`.
- `RfcommChannel` sends with `send()` and receives with `receive()`.
  `receive_exactly()` waits for a given number of bytes, and `receive_until()`
  reads up to a delimiter.
- `BluetoothUnavailableError` is raised while Bluetooth cannot be used. Its
  `reason` says why.
- `ChannelClosedError` is raised once a connection has ended, because the other
  device hung up or the connection to it was lost.
- `aio_rfcomm.macos.prompt_under_name()` gives a script a name for the macOS
  Bluetooth permission prompt.
- A PyInstaller hook, so a frozen program can start the macOS helper.
- Type annotations, marked with `py.typed`.

[Unreleased]: https://github.com/dlech/aio-rfcomm/commits/main
