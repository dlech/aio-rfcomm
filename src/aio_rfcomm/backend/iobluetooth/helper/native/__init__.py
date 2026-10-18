# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
The IOBluetooth layer, as thin a wrapper as the framework allows.

Everything here must run on the main thread of a process whose event loop is
backed by a CFRunLoop. That is not a style preference: on macOS 26.6.2 an
asynchronous IOBluetooth call made from anywhere else returns success and then
never completes, and ``openRFCOMMChannelSync`` is refused outright. The helper
process exists to provide such a thread. Nothing in this module arranges one.

This module knows nothing about the helper or its control protocol, so the
same code serves an in-process caller that already owns a CFRunLoop main
thread.
"""

import sys
from typing import TYPE_CHECKING

# Type checking off macOS skips the rest of each module of this package,
# which all start this way. It does nothing at run time.
assert not TYPE_CHECKING or sys.platform == "darwin"

from aio_rfcomm.backend.iobluetooth.helper.native.bindings import MachError
from aio_rfcomm.backend.iobluetooth.helper.native.connecting import (
    Channel,
    open_channel,
)
from aio_rfcomm.backend.iobluetooth.helper.native.hooks import (
    hear_open_results,
    keep_unread_bytes,
    record_open_result,
)
from aio_rfcomm.backend.iobluetooth.helper.native.listing import list_known_devices
from aio_rfcomm.backend.iobluetooth.helper.native.manager import (
    read_authorization,
    read_power_state,
    report_states,
)
from aio_rfcomm.backend.iobluetooth.helper.native.serving import Service, serve

__all__ = [
    "Channel",
    "MachError",
    "Service",
    "hear_open_results",
    "keep_unread_bytes",
    "list_known_devices",
    "open_channel",
    "read_authorization",
    "read_power_state",
    "record_open_result",
    "report_states",
    "serve",
]
