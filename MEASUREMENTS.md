# Measurements

Comments and tests in this repository describe behavior measured on real
hardware. Each claim names, briefly, what it was measured on: "macOS 26.6.2",
"BlueZ 5.85", "Windows 10.0.26200.9457". This file says what each of those
names means in full.

Add a new entry when measuring on something new. Never change an old one,
since claims in the code refer to it.

## macOS 26.6.2

- Build 25G83.
- Controller: Broadcom BCM_4387, firmware 23.5.636.5345.
- Peers: the Linux machine below, a Pybricks EV3 on firmware 4.1.0b5 and
  later 4.1.0b6, and the Windows machine below.

## BlueZ 5.72

- Ubuntu 24.04, package 5.72-0ubuntu5.5.
- Used until 2026-10-05.
- The kernel was not recorded.
- Peer: the Mac above.

## BlueZ 5.85

- Ubuntu 26.04.1, package 5.85-4ubuntu0.3 or later.
- Kernel 7.0.0-38-generic.
- Used from 2026-10-05.
- Peer: the Mac above.

## Windows, version not recorded

- Measurements made by 2026-09-20.
- Neither the Windows build nor the Bluetooth driver versions were recorded.

## Windows 10.0.26200.9457

- Windows 11 Home 25H2. The registry still names it Windows 10.
- bthport.sys, bthenum.sys and bthusb.sys 10.0.26100.9444.
- rfcomm.sys, bthserv.dll, BluetoothApis.dll and
  Windows.Devices.Bluetooth.dll 10.0.26100.9278.
- Adapter: Qualcomm FastConnect 7800.

## Windows 10.0.26200.9711

- Windows 11 Home 25H2, the same machine as above after servicing.
- bthport.sys, bthenum.sys, bthusb.sys, bthserv.dll, BluetoothApis.dll and
  Windows.Devices.Bluetooth.dll 10.0.26100.9711.
- rfcomm.sys 10.0.26100.9549.
- Adapter: Qualcomm FastConnect 7800.
- Python 3.11.9, arm64.
- Peer: the Linux machine above, on BlueZ package 5.85-4ubuntu0.4.

## Recording a new one

- macOS: `sw_vers`, and the controller's firmware from
  `system_profiler SPBluetoothDataType`.
- Linux: `bluetoothd --version`, `uname -r`, and the BlueZ package version.
  The kernel matters, since RFCOMM sockets are the kernel's.
- Windows: the build from the registry (`CurrentBuild` and `UBR`), and the
  file versions of the drivers above. Windows updates the Bluetooth stack
  through servicing, apart from its own version. Read `FileVersionRaw` from
  PowerShell's `(Get-Item FILE).VersionInfo`. The `FileVersion` text is not
  updated by servicing: on 10.0.26200.9711 it said 10.0.26100.3037 for
  Windows.Devices.Bluetooth.dll, whose version is 10.0.26100.9711.
