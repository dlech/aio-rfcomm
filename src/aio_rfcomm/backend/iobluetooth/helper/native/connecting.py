# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Opening RFCOMM channels to devices.
"""

# Do not add ``from __future__ import annotations`` to this module. rubicon
# reads the annotations on the Objective-C methods below at runtime to build
# each one's Objective-C type encoding, and under PEP 563 an annotation is
# stored as its own source text rather than the object, so a callback would
# be given the wrong signature and read rubbish off the stack.

import asyncio
import ctypes
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

from rubicon.objc import NSObject, ObjCInstance, objc_method
from rubicon.objc.runtime import objc_id

from aio_rfcomm.backend.iobluetooth.codes import (
    ALREADY_CONNECTING,
    CBError,
    IOReturn,
    OIStatus,
)
from aio_rfcomm.backend.iobluetooth.helper.native import bindings, hooks
from aio_rfcomm.backend.iobluetooth.helper.native.bindings import (
    MachError,
    NativeDevice,
    awaiting_completion,
    check,
    completion_tokens,
    hold,
    normalize,
    release,
)
from aio_rfcomm.backend.iobluetooth.helper.native.channel import (
    Channel,
    ChannelDelegate,
)
from aio_rfcomm.backend.iobluetooth.helper.native.hooks import (
    describe_result,
)
from aio_rfcomm.backend.iobluetooth.helper.native.listing import (
    offering,
)
from aio_rfcomm.backend.iobluetooth.helper.native.manager import (
    wait_until_ready,
)
from aio_rfcomm.errors import (
    ConnectionFailedError,
    DeviceNotFoundError,
    RfcommError,
    ServiceNotFoundError,
    UnsupportedOperationError,
)

logger = logging.getLogger(__name__)

# How many times to ask for a channel whose predecessor is still going away,
# and how long to leave between asks. Measured on macOS 26.6.2: a reconnect
# straight after a close needs exactly one wait, so this is twenty times what
# the framework actually takes. It is not a guess at how long an operation
# should be allowed -- that is the caller's to decide -- but the point at
# which a local quirk stops looking like settling and starts looking like a
# framework that is wedged, which is worth saying out loud rather than waiting
# on forever.
_OPEN_ATTEMPTS = 20
_SETTLE_SECONDS = 0.1

# How long to let a channel open go unreported before asking for it again, and
# how many times to ask. macOS fails some opens internally -- a nil channel
# with a zero status -- and then tells nobody, so silence is the only symptom
# and no state distinguishes it from an open still in progress. Measured on
# macOS 26.6.2 over 64 successful opens: 58 to 498 ms, so this is twice the
# slowest. Asking again costs nothing on the air: an open that did land leaves
# the channel where the framework finds it and hands it straight back.
_OPEN_REPORT_SECONDS = 1.0
_OPEN_REPORT_ATTEMPTS = 4


# How long a device is left alone after a channel on it closes. Closing the
# last channel tears down the RFCOMM session, and the L2CAP link underneath it
# goes about thirty milliseconds after the close is reported finished. An open
# issued inside that window is handed the dying link and fails as a nil channel
# with a zero status, reported to nobody. Measured on macOS 26.6.2: swallowed
# at 25 ms and never at 50, so twice that. A deadline rather than a delay: a
# caller that did anything at all in between waits for none of it.
_RELINK_SECONDS = 0.1


# Devices a caller is using. Claimed before the link is brought up and held
# until after the channel closes, so for part of its life there is no channel
# yet -- see open_channel, which is the only thing that puts anything here.
_in_use: set[str] = set()

# When each device last had a channel closed -- see _RELINK_SECONDS.
_closed_at: dict[str, float] = {}


@asynccontextmanager
async def open_channel(
    address: str, *, service: UUID | None = None, channel: int | None = None
) -> AsyncGenerator[Channel, None]:
    """
    Open an RFCOMM channel to a device.

    Exactly one of ``service`` and ``channel`` says which channel to open. A
    service is looked up in the SDP records macOS stored for the device.

    Args:
        address: The device's Bluetooth address.
        service: The service to look up.
        channel: The channel number to open directly.

    Yields:
        The open channel, closed on exit.

    Raises:
        BluetoothUnavailableError: The radio is switched off, this program
            may not use Bluetooth, or the machine has none that works.
        DeviceNotFoundError: The device did not answer.
        ServiceNotFoundError: The device does not offer that service.
        ConnectionFailedError: The channel could not be opened.
        UnsupportedOperationError: The device already has a channel open.
        ValueError: ``address`` is not a Bluetooth address. The library
            checks addresses before they reach the helper, so this is only
            ever a bug.
    """
    if (service is None) == (channel is None):
        raise TypeError("open_channel takes exactly one of service and channel")

    # Before anything else touches the framework. That is the discipline
    # CoreBluetooth asks of its own callers, and reaching for a device first
    # measurably delays the manager coming up on macOS 26.6.2.
    await wait_until_ready()

    device = bindings.IOBluetoothDevice.deviceWithAddressString(address)
    if not device:
        raise ValueError(f"{address!r} is not a Bluetooth address")
    logger.debug("%s: connected=%s", address, device.isConnected())

    # One caller at a time per device, refused rather than queued, because
    # macOS gets a second one wrong three separate ways. Connecting keeps one
    # completion target per device, so a second ``openConnection:`` while one
    # is outstanding overwrites the first's and that caller waits forever.
    # Opening a channel likewise installs its completion in a slot the device
    # has only one of. And closing is serviced in the order channels were
    # opened rather than the one asked for, so with two open, closing either
    # tears down the wrong one and writes to the other go nowhere -- measured
    # on macOS 26.6.2 against a peer listening on three channels: asked to
    # close 6, then 5, then 4, and 4, 5, 6 closed in that order, with the
    # right number on the wire each time.
    #
    # All three are per device. Two devices at once share nothing: measured
    # on macOS 26.6.2 with channels open to two at the same time, neither the
    # channels, the delegates nor the data crossed, and both closes were
    # reported. So this is claimed per address, and before the first await, so
    # that two callers racing here cannot both pass.
    key = address.upper()
    if key in _in_use:
        raise UnsupportedOperationError(
            f"{address} already has an RFCOMM channel open, and macOS cannot "
            "hold two to one device at once -- it closes them in the order "
            "they were opened rather than the one asked for, which loses both. "
            "Close the first channel before opening another."
        )
    _in_use.add(key)
    try:
        await _wait_for_relink(key)

        # A link brought up here also refreshes the stored records. On macOS
        # 26.6.2 bluetoothd reads the device's services again as it connects,
        # if they changed, before it reports the link up. Nothing else does.
        linked_here = not device.isConnected()
        if linked_here:
            await _connect(device, address)

        if channel is not None:
            number = channel
            opening = await _open_reported(device, number, address)
        else:
            try:
                # Guaranteed by the check above, which the type checker cannot
                # see.
                number = _look_up(device, address, cast(UUID, service))
                opening = await _open_reported(device, number, address)
            except ServiceNotFoundError as error:
                if linked_here:
                    raise

                raise ServiceNotFoundError(
                    f"{error}. macOS's copy of {address}'s services may be out "
                    "of date. It reads them again only when it connects to the "
                    "device. Disconnect the device and try again"
                ) from error

        logger.debug("channel %d to %s is open", number, address)
        try:
            yield opening
        finally:
            try:
                await opening.close()
            finally:
                _closed_at[key] = time.monotonic()
    finally:
        _in_use.discard(key)


async def _wait_for_relink(key: str) -> None:
    """
    Wait out the previous channel's teardown, if it is still going on.

    Args:
        key: The device's address, upper case.
    """
    closed = _closed_at.get(key)
    if closed is None:
        return
    left = _RELINK_SECONDS - (time.monotonic() - closed)
    if left > 0:
        logger.debug("letting %s's last channel finish going away", key)
        await asyncio.sleep(left)


def _failure(result: int | None, number: int, address: str) -> RfcommError | None:
    """
    Say what one of bluetoothd's results for a channel open means.

    Args:
        result: The daemon's ``kCBMsgArgResult``, if it sent one.
        number: The RFCOMM channel number.
        address: The device's address, for the error message.

    Returns:
        The error to raise, or ``None`` while the open may still succeed.
    """
    if result is None or result in (0, ALREADY_CONNECTING):
        return None

    # Every other result bluetoothd gave over four days of opens on macOS
    # 26.6.2 (911, 913 and 927) was a definite failure.
    match result:
        case OIStatus.RFCOMM_DLCI_EXISTS:
            return ConnectionFailedError(
                f"channel {number} on {address} is already open or "
                "opening, perhaps in another program on this computer"
            )
        case OIStatus.RFCOMM_REMOTE_REJECT:
            return ServiceNotFoundError(
                f"{address} refused channel {number}. Usually nothing "
                "there is listening on it"
            )
        case OIStatus.RFCOMM_REMOTE_DEVICE_DISCONNECTED:
            return ConnectionFailedError(
                f"{address} hung up as channel {number} opened"
            )
        case _:
            return ConnectionFailedError(
                f"could not open channel {number} on {address} "
                f"(bluetoothd said {result}: {describe_result(result)})"
            )


async def _wait_for_report(opening: Channel, number: int, address: str) -> bool:
    """
    Wait for the framework to say a channel opened.

    Args:
        opening: The channel, asked for and not yet open.
        number: The RFCOMM channel number.
        address: The device's address.

    For as long as bluetoothd says an earlier ask is still going, this keeps
    waiting. The caller's cancel is what ends that.

    Returns:
        Whether it said so. ``False`` means silence from the framework and
        nothing from bluetoothd that settles it either.

    Raises:
        MachError: The framework said the open failed.
        RfcommError: bluetoothd said the open failed.
    """
    key = (normalize(address), number)
    while True:
        if await opening.wait_until_open(_OPEN_REPORT_SECONDS):
            return True

        result = hooks.open_results.get(key)
        failure = _failure(result, number, address)
        if failure is not None:
            raise failure

        if result != ALREADY_CONNECTING:
            return False

        # An earlier ask for a channel on this device is still going, and
        # whatever it ends in is reported to this channel, the newest. Asking
        # again would only be answered the same way. Measured on macOS 26.6.2
        # against BlueZ 5.85: an ask went unanswered for 5 s, asking again got
        # this at once, and the first ask then opened its channel.
        logger.debug(
            "an earlier open on %s is still going. waiting for channel %d",
            address,
            number,
        )


async def _open_reported(device: NativeDevice, number: int, address: str) -> Channel:
    """
    Open a channel, asking again if the framework never says whether it did.

    A failed open that reports no error reaches neither delegate method, so the
    caller would otherwise wait forever -- see ``_OPEN_REPORT_SECONDS``.
    Asking again costs nothing on the air: an open that did land leaves the
    channel where the framework finds it and hands it straight back, and one
    that did not never reached the peer.

    An ask is never given up on while bluetoothd is still working on it. One
    given up on can land later on the next open, as another channel, and
    that one is closed rather than returned.

    Args:
        device: The ``IOBluetoothDevice``.
        number: The RFCOMM channel number.
        address: Its address, for the error messages.

    Returns:
        The channel, open.

    Raises:
        ServiceNotFoundError: The device refused the channel, usually
            because nothing there is listening on it.
        ConnectionFailedError: The channel could not be opened, or the
            framework never reported one open.
    """
    key = (normalize(address), number)
    unreported = 0
    while unreported < _OPEN_REPORT_ATTEMPTS:
        hooks.open_results.pop(key, None)
        opening = await _request(device, number, address)
        try:
            reported = await _wait_for_report(opening, number, address)
        except MachError as error:
            await opening.close()
            raise ConnectionFailedError(
                f"could not open channel {number} on {address}: {error}"
            ) from error
        except RfcommError:
            opening.release()
            raise
        except BaseException:
            await opening.close()
            raise

        if not reported:
            opening.release()
            unreported += 1
            logger.debug(
                "channel %d to %s was never reported open. asking again",
                number,
                address,
            )
            continue

        connected = opening.connected_number()
        if connected is None or connected == number:
            return opening

        # An open given up on earlier has landed on this one. The device's
        # peer keeps one callback for opens, which the newest replaces, and
        # nothing on the way compares channel numbers (macOS 26.6.2
        # disassembly). Measured there against BlueZ 5.85: asked for 25, told
        # it opened, and holding the channel to 22. Nobody asked for that one
        # any more, so it is closed rather than handed out.
        logger.debug(
            "channel %d to %s came back as channel %d. closing that one",
            number,
            address,
            connected,
        )
        await opening.close()
        failure = _failure(hooks.open_results.get(key), number, address)
        if failure is not None:
            raise failure

    raise ConnectionFailedError(
        f"the framework never reported channel {number} on {address} open, "
        f"after {_OPEN_REPORT_ATTEMPTS} attempts"
    )


async def _request(device: NativeDevice, number: int, address: str) -> Channel:
    """
    Ask the framework for a channel, waiting out the previous one if need be.

    A channel that comes back already open was not opened by this call: the
    framework found one on that number and bound this wrapper to it instead.
    Nothing is outstanding, so no open-complete callback is coming and
    waiting on one would wait forever. Such a wrapper is no use either --
    on macOS 26.6.2 the framework binds it and sets its delegate but never
    wires its streams, so it can be written to and never delivers a byte, and
    what is written goes to whoever still holds the channel. It is the previous
    channel to this device on this number, which the framework keeps handing
    over until it has finished tearing that one down, so the thing to do is
    wait and ask again. One that never goes is an earlier open that landed
    after it was given up on, and is closed.

    Waiting and asking again is cruder than waiting for the framework to say
    the old channel is gone, but nothing says so. ``rfcommChannelClosed:``
    answers a different question -- it says the channel closed, which on
    macOS 26.6.2 it reports around thirteen milliseconds after
    ``closeChannel`` and well before the framework stops handing that channel
    back.

    ``registerForChannelCloseNotification`` never fires, for a close this
    program asks for or one the peer causes, where the delegate callback
    does: measured on macOS 26.6.2 with a real ``SEL``. The disassembly says
    why. A ``CBClassicPeer`` has one disconnect callback. The coordinator that
    would pass a close on to notifications sets it, and every
    ``IOBluetoothRFCOMMChannel`` set up on that peer replaces it with its own.

    Args:
        device: The connected ``IOBluetoothDevice``.
        number: The RFCOMM channel number.
        address: The device's address, for the error message.

    Returns:
        The channel, which is not open yet.

    Raises:
        ConnectionFailedError: The channel could not be requested, or the
            previous one never finished closing.
    """
    for attempt in range(_OPEN_ATTEMPTS):
        delegate = ChannelDelegate.alloc().init()
        handle = objc_id()
        try:
            check(
                device.openRFCOMMChannelAsync(
                    ctypes.byref(handle), withChannelID=number, delegate=delegate
                )
            )
        except MachError as error:
            raise ConnectionFailedError(
                f"could not open channel {number} on {address}: {error}"
            ) from error

        opened = bindings.IOBluetoothRFCOMMChannel(handle)
        if not opened.isOpen():
            return Channel(opened, delegate, device)

        earlier = Channel(opened, delegate, device)
        del opened, delegate
        if attempt == _OPEN_ATTEMPTS // 2:
            # Still there, so not the previous channel on its way out but one
            # nobody holds: an open given up on that landed after all, with no
            # channel waiting to be told. Nothing else in this program can be
            # using it, since the device is claimed. Closed halfway, so the
            # rest of the wait covers its going. Measured on macOS 26.6.2
            # against BlueZ 5.85: one stayed open for 22 s until something
            # else closed it. Closed here, 15 went from the air at once or
            # within 5 s.
            logger.debug(
                "channel %d on %s is open and nobody holds it. Closing it",
                number,
                address,
            )
            await earlier.close()
            continue

        logger.debug(
            "channel %d on %s is still open from an earlier use. Waiting",
            number,
            address,
        )
        # Let go of without closing it: this is the previous channel, already
        # on its way out under its own steam.
        earlier.release()
        await asyncio.sleep(_SETTLE_SECONDS)

    raise ConnectionFailedError(
        f"channel {number} on {address} is already open and did not come free"
    )


class ConnectionComplete(NSObject):
    """
    Completion for the ACL connection a channel is carried over.
    """

    future: asyncio.Future[None]
    token: int

    # No return annotation: rubicon reads annotations to build the
    # Objective-C signature, and an initializer's is already known.
    @objc_method
    def init(self):
        self.future = asyncio.get_running_loop().create_future()
        self.token = next(completion_tokens)
        return self

    @objc_method
    def connectionComplete_status_(self, _device: ObjCInstance, status: int) -> None:
        awaiting_completion.pop(self.token, None)
        if self.future.done():
            return
        if status:
            # On macOS 26.6.2, not the IOKit status the header promises but
            # CoreBluetooth's error code: the daemon's result made into an
            # NSError, of which only the code is passed on. An unanswered page
            # arrives as CONNECTION_FAILED, which mach_error_string renders as
            # nonsense, so it is reported as the timeout it really is.
            raise_as = (
                IOReturn.TIMEOUT if status == CBError.CONNECTION_FAILED else status
            )
            self.future.set_exception(MachError(raise_as))
        else:
            self.future.set_result(None)


async def _connect(device: NativeDevice, address: str) -> None:
    """
    Bring up the baseband link to a device.

    Args:
        device: The ``IOBluetoothDevice``.
        address: Its address, for the error message.

    Raises:
        DeviceNotFoundError: The device did not answer.
        ConnectionFailedError: The link could not be brought up.
    """
    # Never closed again. On macOS 26.6.2 closeConnection makes bluetoothd
    # disconnect everything on the device, other programs' channels and audio
    # included. The gentler cancelPeerConnection: leaves the link up.
    complete = ConnectionComplete.alloc().init()
    hold(complete)
    try:
        check(device.openConnection(complete))
        await complete.future
    except MachError as error:
        release(complete)
        raise _translate(error, address) from error


def _look_up(device: NativeDevice, address: str, service: UUID) -> int:
    """
    Find which RFCOMM channel a service is on, in the stored records.

    Args:
        device: The connected ``IOBluetoothDevice``.
        address: Its address, for the error message.
        service: The service to find.

    Returns:
        The RFCOMM channel number.

    Raises:
        ServiceNotFoundError: The device does not offer that service.
    """
    # Never performSDPQuery:. On macOS 26.6.2, with the link up, it answers
    # from the same stored records within milliseconds, without asking the
    # device.
    record = offering(device, [service])
    if record is None:
        raise ServiceNotFoundError(f"{address} does not offer {service}")

    # A channel number is one byte, and the framework declares the
    # out-parameter as a pointer to unsigned char -- which rubicon reads as
    # ``char *``, so it is the one ctypes type it will accept here. The value
    # comes back as a one-byte string rather than as a number.
    number = ctypes.c_char()
    if record.getRFCOMMChannelID(ctypes.byref(number)):
        raise ServiceNotFoundError(f"{service} on {address} is not an RFCOMM service")
    return number.value[0]


def _translate(error: MachError, address: str) -> RfcommError:
    """
    Turn an IOBluetooth status into one of ours.

    Args:
        error: What IOBluetooth reported.
        address: The device being reached.

    Returns:
        The error to raise instead.
    """
    if error.code == IOReturn.TIMEOUT:
        return DeviceNotFoundError(address)
    return ConnectionFailedError(f"could not reach {address}: {error}")
