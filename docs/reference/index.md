# API reference

Every public name, one page per topic:

- [Bluetooth](bluetooth.md): opening Bluetooth, and whether it can be used.
- [Devices](devices.md): the devices you connect to.
- [Serving](serving.md): letting devices connect to you.
- [Channels](channels.md): sending and receiving.
- [Errors](errors.md): what can go wrong.
- [macOS](macos.md): naming your program for the permission dialog.

Everything is importable from the top-level `aio_rfcomm` package, except the
errors, which live in `aio_rfcomm.errors`, and `prompt_under_name`, which lives
in `aio_rfcomm.macos`.
