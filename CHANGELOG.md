# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `Availability.AWAITING_PERMISSION`: the system is asking the user whether
  this program may use Bluetooth. It is also an `UnavailableReason`, so an
  exhaustive `match` on the reasons needs a case for it.

### Changed

- macOS: `open_bluetooth` no longer waits for the user to answer the
  permission dialog. The availability is `AWAITING_PERMISSION` until they do,
  then `USABLE` or `NOT_ALLOWED`.
- macOS: when macOS ends the helper process because the program has no
  usage description, the `open_bluetooth()` block raises
  `aio_rfcomm.macos.MissingUsageDescriptionError`, a `RuntimeError`. This is
  the program's mistake, so it is no longer reported as `NOT_ALLOWED`.

### Fixed

- macOS: `open_bluetooth` never returned on a machine where nobody can answer
  the permission dialog, such as a CI runner.
- macOS: a refused permission was reported as `UNSUPPORTED` rather than
  `NOT_ALLOWED`.
- macOS: `open_bluetooth` never returned on a Mac whose Bluetooth never comes
  up, such as GitHub's macOS runners with their virtual controller. The
  availability is now `UNSUPPORTED`.
- macOS: the permission dialog and Privacy & Security show the name passed to
  `aio_rfcomm.macos.prompt_under_name()` instead of "AioRfcommHelper".
- macOS: a program that ended while the permission dialog was open waited two
  seconds for the helper process, then logged a warning and killed it. The
  helper now exits at once.
- macOS: if the Bluetooth service restarted while the permission dialog was
  open, the availability became `UNSUPPORTED` and stayed so after the user
  allowed Bluetooth. It now stays `AWAITING_PERMISSION` until the answer.
- macOS: the message for a refused permission ended with a note about
  `prompt_under_name()` that only a programmer could act on. It now names the
  program as System Settings shows it instead. So does the message for an
  open permission dialog.

## [1.0.0] - 2026-10-17

Initial release.

[Unreleased]: https://github.com/dlech/aio-rfcomm/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dlech/aio-rfcomm/releases/tag/v1.0.0
