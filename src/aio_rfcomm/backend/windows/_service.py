# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Lechner <david@pybricks.com>

"""
Publishing a service record on Windows, through Winsock's name registration.

WinRT's ``RfcommServiceProvider`` would also publish one, but it hands back a
``StreamSocket`` to listen on, and the backend deliberately does its I/O over
a plain socket instead. ``WSASetService`` publishes a record for a socket we
already own, which keeps the two halves consistent.

The structures below are Winsock's own, and the one thing that matters about
them is that ``SOCKADDR_BTH`` is **packed**. Windows declares it without
padding, so it is thirty bytes and ``port`` sits at offset 26. Left to
ctypes' natural alignment it becomes forty bytes with ``port`` at 32, Windows
reads the four bytes at 26 -- the tail of ``serviceClassId`` -- and publishes
a record advertising a channel nobody is listening on. Measured: a service on
channel 25 was advertised as channel 161.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import socket
import uuid

__all__ = ["delete", "register"]

_NS_BTH = 16
_RNRSERVICE_REGISTER = 0
_RNRSERVICE_DELETE = 1
_AF_BTH = 32


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", w.DWORD),
        ("Data2", w.WORD),
        ("Data3", w.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def of(cls, value: uuid.UUID) -> _GUID:
        """
        Convert a Python UUID to the Windows layout.

        Args:
            value: The service UUID.

        Returns:
            The same UUID as a ``GUID``.
        """
        first, second, third, fourth, fifth, _ = value.fields
        rest = (ctypes.c_ubyte * 8)(fourth, fifth, *value.bytes[10:])
        return cls(first, second, third, rest)


class _SOCKADDR_BTH(ctypes.Structure):
    # Packed, for the reason in the module docstring. Do not remove.
    _pack_ = 1
    _fields_ = (
        ("addressFamily", w.USHORT),
        ("btAddr", ctypes.c_ulonglong),
        ("serviceClassId", _GUID),
        ("port", w.ULONG),
    )


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = (("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int))


class _CSADDR_INFO(ctypes.Structure):
    _fields_ = (
        ("LocalAddr", _SOCKET_ADDRESS),
        ("RemoteAddr", _SOCKET_ADDRESS),
        ("iSocketType", ctypes.c_int),
        ("iProtocol", ctypes.c_int),
    )


class _WSAQUERYSET(ctypes.Structure):
    _fields_ = (
        ("dwSize", w.DWORD),
        ("lpszServiceInstanceName", w.LPWSTR),
        ("lpServiceClassId", ctypes.POINTER(_GUID)),
        ("lpVersion", ctypes.c_void_p),
        ("lpszComment", w.LPWSTR),
        ("dwNameSpace", w.DWORD),
        ("lpNSProviderId", ctypes.POINTER(_GUID)),
        ("lpszContext", w.LPWSTR),
        ("dwNumberOfProtocols", w.DWORD),
        ("lpafpProtocols", ctypes.c_void_p),
        ("lpszQueryString", w.LPWSTR),
        ("dwNumberOfCsAddrs", w.DWORD),
        ("lpcsaBuffer", ctypes.POINTER(_CSADDR_INFO)),
        ("dwOutputFlags", w.DWORD),
        ("lpBlob", ctypes.c_void_p),
    )


_ws2 = ctypes.WinDLL("ws2_32")
_ws2.WSAGetLastError.restype = ctypes.c_int
_ws2.WSASetServiceW.argtypes = (ctypes.POINTER(_WSAQUERYSET), ctypes.c_int, w.DWORD)
_ws2.WSASetServiceW.restype = ctypes.c_int


def _set(service: uuid.UUID, name: str, channel: int, operation: int) -> int:
    """
    Register or remove one service record.

    Args:
        service: The service UUID.
        name: The human-readable service name.
        channel: The RFCOMM channel the record should advertise.
        operation: ``_RNRSERVICE_REGISTER`` or ``_RNRSERVICE_DELETE``.

    Returns:
        The Winsock error code, or zero.
    """
    identifier = _GUID.of(service)
    address = _SOCKADDR_BTH(_AF_BTH, 0, identifier, channel)
    local = _SOCKET_ADDRESS(
        ctypes.cast(ctypes.byref(address), ctypes.c_void_p), ctypes.sizeof(address)
    )
    where = _CSADDR_INFO(
        local, _SOCKET_ADDRESS(None, 0), socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
    )

    query = _WSAQUERYSET()
    query.dwSize = ctypes.sizeof(_WSAQUERYSET)
    # Held in a local so the buffer outlives the call.
    held = ctypes.create_unicode_buffer(name)
    query.lpszServiceInstanceName = ctypes.cast(held, w.LPWSTR)
    query.lpServiceClassId = ctypes.pointer(identifier)
    query.dwNameSpace = _NS_BTH
    query.dwNumberOfCsAddrs = 1
    query.lpcsaBuffer = ctypes.pointer(where)

    if _ws2.WSASetServiceW(ctypes.byref(query), operation, 0) == 0:
        return 0
    return _ws2.WSAGetLastError()


def register(service: uuid.UUID, name: str, channel: int) -> None:
    """
    Advertise a service on a channel -- ``RNRSERVICE_REGISTER``.

    Args:
        service: The service UUID peers will look for.
        name: The human-readable service name.
        channel: The RFCOMM channel to advertise.

    Raises:
        OSError: Windows refused the registration.
    """
    failure = _set(service, name, channel, _RNRSERVICE_REGISTER)
    if failure:
        raise OSError(failure, f"WSASetService could not publish {service}")


def delete(service: uuid.UUID, name: str, channel: int) -> int:
    """
    Stop advertising a service -- ``RNRSERVICE_DELETE``.

    Failure is reported rather than raised, because it does not matter much:
    Windows removes the record when the process exits regardless, and the
    delete is refused with ``WSAEINVAL`` even when the same query set
    registered successfully -- measured, and the record still disappeared.

    Args:
        service: The service UUID.
        name: The name it was published under.
        channel: The channel it was published on.

    Returns:
        The Winsock error code, or zero.
    """
    return _set(service, name, channel, _RNRSERVICE_DELETE)
