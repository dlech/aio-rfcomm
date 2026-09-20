# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Starting the helper process and talking to it.

Nothing here imports rubicon or touches IOBluetooth: the framework only ever
runs in the child. That keeps the cost of the Objective-C bridge out of the
caller's process and means a failure to load it shows up as a helper that will
not start, rather than as an import error in an unrelated program.

The pair of sockets is the whole lifetime story. When the caller leaves the
adapter's ``async with``, this end closes and the helper reads end of file and
exits; macOS has no ``PDEATHSIG``, so that is also what stops a helper whose
parent died without tidying up.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import itertools
import logging
import os
import signal
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from aio_rfcomm._teardown import unwrap_lone_error
from aio_rfcomm.backend.iobluetooth._bundle import build_helper
from aio_rfcomm.backend.iobluetooth._protocol import Link
from aio_rfcomm.errors import (
    CloseReason,
    ConnectionFailedError,
    PermissionDeniedError,
    RfcommError,
    UnsupportedOperationError,
)

__all__ = ["Helper", "prompt_under_own_name", "run_helper"]

logger = logging.getLogger(__name__)

# The first descriptor after the standard streams, and so the one the
# control socket is moved to in the helper unless it is already there.
_FIRST_FREE_FD = 3

# How the helper identifies itself to macOS, when it has to identify itself at
# all. Left unset, the helper inherits the responsibility of whatever started
# it, which is what a properly bundled application wants.
_own_name: str | None = None
_own_reason: str | None = None


def prompt_under_own_name(*, name: str, reason: str) -> None:
    """
    Let the helper ask for Bluetooth permission as itself.

    macOS asks the *responsible* process for permission, which normally means
    the application that started this one. That is the right behaviour for a
    bundled application: the dialog names the application, and the user finds
    it under that name in Privacy & Security.

    A script, a REPL or a command line tool has no such application to speak
    for it, and macOS ends such a process with ``SIGABRT`` rather than asking
    anybody anything. Calling this before opening an adapter breaks the link
    to the responsible process, so the helper asks on its own behalf instead.

    The cost is that ``name`` is what the user sees, both in the permission
    dialog and afterwards in Privacy & Security -- so name it after the
    program the user thinks they are running, never after this library.

    Args:
        name: What to call the helper. The user sees this.
        reason: Why the program needs Bluetooth. The user reads this in the
            permission dialog, so write it for them.
    """
    global _own_name, _own_reason
    _own_name, _own_reason = name, reason


class HelperFailedError(RfcommError):
    """
    The helper process stopped, or never started.

    Not exported from :mod:`aio_rfcomm.errors`: it is specific to macOS, and
    what a caller can do about it is the same as for any other failure to
    reach the adapter.
    """


class Helper:
    """
    A running helper process, and the requests in flight to it.

    Replies and events arrive on one socket in whatever order the helper
    produces them, so a reader task hands each to whoever is waiting: replies
    to the request they answer, and the end of a channel to that channel.
    """

    def __init__(self, link: Link) -> None:
        """
        Args:
            link: The control socket. Taken over.
        """
        self._link = link
        self._numbers = itertools.count()
        self._waiting: dict[int, asyncio.Future[tuple[Any, list[int]]]] = {}
        self._channels: dict[int, asyncio.Future[str]] = {}
        self._stopped: RfcommError | None = None

    async def ask(self, op: str, **fields: Any) -> tuple[Any, list[int]]:
        """
        Make one request and wait for its answer.

        Args:
            op: Which operation.
            fields: Its arguments.

        Returns:
            The result, and any descriptors that came with it. The caller owns
            the descriptors.

        Raises:
            RfcommError: The helper reported a failure, or stopped.
        """
        if self._stopped is not None:
            raise self._stopped

        number = next(self._numbers)
        answer: asyncio.Future[tuple[Any, list[int]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[number] = answer
        try:
            self._link.send({"id": number, "op": op, **fields})
            return await answer
        except asyncio.CancelledError:
            if answer.done() and not answer.cancelled() and not answer.exception():
                # The answer landed in the same moment the caller gave up, so
                # nothing needs abandoning -- but the socket that came with it
                # is ours now, and closing it is what tells the helper the
                # channel is not wanted.
                _, fds = answer.result()
                for fd in fds:
                    os.close(fd)
            elif self._stopped is None:
                # Opening a channel can wait on a device that never answers,
                # so a caller who gives up has to be able to make the helper
                # give up too. Nothing waits for the acknowledgement; there is
                # nothing useful to do with it.
                self._link.send(
                    {"id": next(self._numbers), "op": "abandon", "of": number}
                )
            raise
        finally:
            self._waiting.pop(number, None)

    def expect(self, handle: int) -> asyncio.Future[str]:
        """
        Register interest in how one channel ends.

        Args:
            handle: The channel's handle, from the reply that opened it.

        Returns:
            A future resolved with the reason once the channel ends.
        """
        ending: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._channels[handle] = ending
        return ending

    def forget(self, handle: int) -> None:
        """
        Stop caring how one channel ends.

        Args:
            handle: The channel's handle.
        """
        self._channels.pop(handle, None)

    async def deliver(self) -> None:
        """
        Hand each frame to whoever is waiting for it, until the helper stops.

        Runs for as long as the helper does, and returns when it has gone.
        """
        while True:
            frame = await self._link.receive()
            if frame is None:
                self._stop(HelperFailedError("the macOS Bluetooth helper stopped"))
                return

            message, fds = frame
            if "event" in message:
                self._event(message)
                continue

            answer = self._waiting.get(message.get("id", -1))
            if answer is None or answer.done():
                # Nobody is waiting: the request was abandoned. Any descriptor
                # that came with the reply is ours to close.
                for fd in fds:
                    os.close(fd)
                continue

            if "error" in message:
                answer.set_exception(_rebuild(message["error"], message["message"]))
            else:
                answer.set_result((message.get("ok"), fds))

    def _event(self, message: dict[str, Any]) -> None:
        """
        Act on something the helper reported without being asked.

        Args:
            message: The event.
        """
        if message["event"] == "gone":
            ending = self._channels.get(message["handle"])
            if ending is not None and not ending.done():
                ending.set_result(message["reason"])

    def _stop(self, error: RfcommError) -> None:
        """
        Fail everything outstanding, because the helper has gone.

        Args:
            error: What to fail them with.
        """
        self._stopped = error
        for answer in list(self._waiting.values()):
            if not answer.done():
                answer.set_exception(error)
        for ending in list(self._channels.values()):
            if not ending.done():
                # The helper is not there to say why, and from out here a
                # helper that stopped is indistinguishable from a link that
                # dropped. Spelled the way the helper would have spelled it,
                # so there is only one wire format to read.
                ending.set_result(CloseReason.LINK_LOST.name)


@asynccontextmanager
async def run_helper() -> AsyncGenerator[Helper, None]:
    """
    Start a helper process and keep it running for the enclosing block.

    Yields:
        The helper, ready for requests.

    Raises:
        PermissionDeniedError: macOS refused Bluetooth to the helper.
        HelperFailedError: The helper could not be started.
    """
    if sys.platform != "darwin":
        raise UnsupportedOperationError("the helper process is macOS only")

    # Left to its defaults the bundle is never the one macOS asks about:
    # it inherits the responsibility of whatever started this process.
    executable = (
        build_helper(_own_name, _own_reason)
        if _own_name is not None and _own_reason is not None
        else build_helper()
    )
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid = _spawn(executable, theirs, disclaim=_own_name is not None)
    finally:
        theirs.close()

    link = Link(ours)
    helper = Helper(link)
    try:
        # The reader task is an implementation detail of this function, so
        # what the caller sees is their own error rather than a group of one
        # wrapped around it.
        with unwrap_lone_error():
            async with asyncio.TaskGroup() as group:
                reader = group.create_task(helper.deliver(), name="aio-rfcomm helper")
                try:
                    await _wait_until_ready(helper, pid)
                    yield helper
                finally:
                    reader.cancel()
    finally:
        link.close()
        await _reap(pid)


async def _wait_until_ready(helper: Helper, pid: int) -> None:
    """
    Wait for the helper to report that Bluetooth is usable.

    The request the helper answers here makes a real IOBluetooth call, because
    macOS does not refuse Bluetooth when a process starts: it refuses at the
    first call that needs it, and refuses by ending the process with
    ``SIGABRT``. A helper that has merely started tells us nothing, so this
    waits until one has actually used the radio and lived.

    There is deliberately no time limit. The first such call is what puts the
    permission dialog on screen, so this waits for however long somebody takes
    to read it and decide -- which is not a length of time a library is
    entitled to guess at. A caller who is not prepared to wait says so by
    opening the adapter inside a cancel scope of their own.

    Args:
        helper: The helper being started.
        pid: Its process id.

    Raises:
        PermissionDeniedError: macOS refused Bluetooth to the helper.
        HelperFailedError: The helper stopped before answering.
    """
    try:
        await helper.ask("ready")
    except HelperFailedError:
        raise _died(pid) from None


def _died(pid: int) -> RfcommError:
    """
    Work out why a helper that stopped during startup stopped.

    Args:
        pid: The helper's process id.

    Returns:
        The error to raise.
    """
    try:
        _done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        status = 0

    if os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGABRT:
        return PermissionDeniedError(
            "macOS refused Bluetooth to the helper process and ended it. "
            "Bluetooth is granted to the application responsible for this "
            "process, which must carry NSBluetoothAlwaysUsageDescription in "
            "its Info.plist. If this program is not a bundled application -- "
            "a script, a REPL or a command line tool -- call "
            "aio_rfcomm.macos.prompt_under_own_name() before opening an "
            "adapter, so the helper can ask for permission itself."
        )
    return HelperFailedError(
        f"the macOS Bluetooth helper stopped during startup (status {status})"
    )


def _rebuild(name: str, message: str) -> RfcommError:
    """
    Turn an error the helper reported back into an exception.

    Args:
        name: The exception's class name.
        message: Its message.

    Returns:
        The error, or a generic connection failure if the name is not one of
        ours.
    """
    from aio_rfcomm import errors

    kind = getattr(errors, name, None)
    if isinstance(kind, type) and issubclass(kind, RfcommError):
        try:
            return kind(message)
        except (TypeError, AttributeError):
            # Some of our errors are built from a reason rather than from a
            # message, and take a string either by refusing it outright or by
            # accepting it and then failing to read it as a reason. The helper
            # never names one of those, so this is only reached by a helper
            # that does not match this library -- where losing the exact type
            # matters far less than getting the message to the caller.
            return ConnectionFailedError(message)
    return ConnectionFailedError(message)


# --------------------------------------------------------------------------
# Spawning
#
# posix_spawn is reached through ctypes rather than through subprocess,
# which would otherwise do all of this and the reaping besides. The helper
# has to be started with responsibility disclaimed, and nothing in the
# standard library can ask for that: subprocess exposes no spawn attributes
# at all, and os.posix_spawn exposes only the ones POSIX defines --
# setpgroup, resetids, setsid, the signal masks and the scheduler. Nor can
# preexec_fn stand in for it, because disclaiming is not something a forked
# child does to itself; it is an attribute posix_spawn applies while making
# the process, and macOS offers no equivalent afterwards.
#
# Without it the helper inherits the responsibility of whatever started this
# program and is killed the first time it touches Bluetooth, so this is the
# whole reason a helper process can reach the radio at all.
# --------------------------------------------------------------------------

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

_attributes = ctypes.c_void_p
_actions = ctypes.c_void_p


def _declare() -> None:
    """
    Tell ctypes the shapes of the libc calls used below.
    """
    _libc.posix_spawnattr_init.argtypes = [ctypes.POINTER(_attributes)]
    _libc.posix_spawnattr_destroy.argtypes = [ctypes.POINTER(_attributes)]
    _libc.posix_spawn_file_actions_init.argtypes = [ctypes.POINTER(_actions)]
    _libc.posix_spawn_file_actions_destroy.argtypes = [ctypes.POINTER(_actions)]
    _libc.posix_spawn_file_actions_adddup2.argtypes = [
        ctypes.POINTER(_actions),
        ctypes.c_int,
        ctypes.c_int,
    ]
    _libc.posix_spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        ctypes.POINTER(_actions),
        ctypes.POINTER(_attributes),
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]


if sys.platform == "darwin":
    _declare()


def _spawn(
    executable: os.PathLike[str], control: socket.socket, *, disclaim: bool
) -> int:
    """
    Start the helper.

    Args:
        executable: The helper executable inside its bundle.
        control: The helper's end of the control socket, which is put on a
            descriptor of its own in the child.
        disclaim: Whether to break the chain of responsibility, so that macOS
            treats the helper as its own application rather than as part of
            whatever started this process.

    Returns:
        The child's process id.

    Raises:
        HelperFailedError: The process could not be started.
    """
    attributes = _attributes()
    actions = _actions()
    _libc.posix_spawnattr_init(ctypes.byref(attributes))
    _libc.posix_spawn_file_actions_init(ctypes.byref(actions))
    try:
        if disclaim:
            _libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attributes), 1)

        # Python opens descriptors close-on-exec, so something has to clear
        # that for this one or the helper inherits nothing. Marking the
        # socket inheritable would do it, but that is a change to this
        # process that lasts until the spawn returns, and anything else
        # starting a subprocess in the meantime would inherit the helper's
        # end of the control socket and hold it open -- which is precisely
        # what stops the library noticing the helper has died. dup2 has no
        # such window: it clears close-on-exec on the descriptor it creates,
        # in the child, leaving this process untouched.
        #
        # The descriptor it creates, though -- dup2 onto the number the
        # socket already has is defined as doing nothing at all,
        # close-on-exec included, and a socket pair in a program that has
        # opened little else really does land on 3. So the one number that
        # cannot work is ruled out rather than hoped against, and the helper
        # is told which it ended up with.
        source = control.fileno()
        target = _FIRST_FREE_FD if source != _FIRST_FREE_FD else _FIRST_FREE_FD + 1
        _libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), source, target)

        words = [os.fsencode(executable), b"-m", b"aio_rfcomm.backend.iobluetooth"]
        argv = (ctypes.c_char_p * (len(words) + 1))(*words, None)

        pairs = [f"{k}={v}".encode() for k, v in _environment(target).items()]
        envp = (ctypes.c_char_p * (len(pairs) + 1))(*pairs, None)

        pid = ctypes.c_int()
        failed = _libc.posix_spawn(
            ctypes.byref(pid),
            os.fsencode(executable),
            ctypes.byref(actions),
            ctypes.byref(attributes),
            argv,
            envp,
        )
        if failed:
            raise HelperFailedError(
                f"could not start the macOS Bluetooth helper: {os.strerror(failed)}"
            )
        return pid.value
    finally:
        _libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        _libc.posix_spawnattr_destroy(ctypes.byref(attributes))


def _environment(control: int) -> dict[str, str]:
    """
    Build the environment the helper runs in.

    The helper is a copy of this interpreter sitting somewhere it has never
    been, so it cannot work out where its own standard library is. Both paths
    are handed to it explicitly, which also gives it the same packages this
    process imports -- including rubicon, which it needs and this process does
    not.

    Args:
        control: The descriptor the helper will find its end of the control
            socket on.

    Returns:
        The environment.
    """
    environment = dict(os.environ)
    # Left behind, these point the copied interpreter back at a virtual
    # environment whose layout no longer describes it.
    for stale in ("PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "PYTHONSTARTUP"):
        environment.pop(stale, None)
    environment["PYTHONHOME"] = sys.base_prefix
    environment["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    environment["AIO_RFCOMM_CONTROL_FD"] = str(control)
    return environment


async def _reap(pid: int) -> None:
    """
    Wait for the helper to exit, insisting if it will not.

    The waits below are polls -- ``WNOHANG`` returns at once whether or not
    the child has finished -- so nothing here blocks the loop.

    Args:
        pid: The helper's process id.
    """
    for _ in range(200):
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)  # noqa: ASYNC222
        except ChildProcessError:
            return
        if done:
            return
        await asyncio.sleep(0.01)

    logger.warning("macOS Bluetooth helper %d did not exit; killing it", pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    for _ in range(100):
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)  # noqa: ASYNC222
        except ChildProcessError:
            return
        if done:
            return
        await asyncio.sleep(0.01)
