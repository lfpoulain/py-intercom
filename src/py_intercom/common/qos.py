"""Voice priority for the UDP audio, through Windows qWAVE.

A flow of type "voice" gets DSCP 56 and 802.1p priority 7: a Wi-Fi card and
access point then send it in the WMM voice queue, ahead of downloads and
updates, and a managed switch can do the same. qWAVE needs no administrator
rights. Best effort like the rest of the realtime tuning: a refused flow
(loopback, missing qWAVE service, other OS) leaves the audio unmarked.
"""

from __future__ import annotations

import ctypes
import socket
import sys
import threading
from ctypes import wintypes
from typing import Final

from loguru import logger

_QOS_TRAFFIC_TYPE_VOICE: Final = 4
# Mark only: no probing of the network path, no adaptive throttling.
_QOS_NON_ADAPTIVE_FLOW: Final = 0x00000002


class _QosVersion(ctypes.Structure):
    _fields_ = [("MajorVersion", wintypes.USHORT), ("MinorVersion", wintypes.USHORT)]


class _SockaddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_short),
        ("sin_port", wintypes.USHORT),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_char * 8),
    ]


def _load_qwave():
    if sys.platform != "win32":
        return None
    try:
        qwave = ctypes.WinDLL("qwave.dll", use_last_error=True)
    except OSError:
        return None
    qwave.QOSCreateHandle.argtypes = (ctypes.POINTER(_QosVersion), ctypes.POINTER(wintypes.HANDLE))
    qwave.QOSCreateHandle.restype = wintypes.BOOL
    qwave.QOSCloseHandle.argtypes = (wintypes.HANDLE,)
    qwave.QOSCloseHandle.restype = wintypes.BOOL
    qwave.QOSAddSocketToFlow.argtypes = (
        wintypes.HANDLE,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.ULONG),
    )
    qwave.QOSAddSocketToFlow.restype = wintypes.BOOL
    qwave.QOSRemoveSocketFromFlow.argtypes = (wintypes.HANDLE, ctypes.c_size_t, wintypes.ULONG, wintypes.DWORD)
    qwave.QOSRemoveSocketFromFlow.restype = wintypes.BOOL
    return qwave


class VoiceQos:
    """Marks what one UDP socket sends to each added address as voice.

    Thread safe; never raises. Call ``close`` before closing the socket.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._lock = threading.Lock()
        self._flows: dict[tuple[str, int], int] = {}
        self._handle: int | None = None
        self._qwave = _load_qwave()
        if self._qwave is None:
            return
        handle = wintypes.HANDLE()
        if self._qwave.QOSCreateHandle(ctypes.byref(_QosVersion(1, 0)), ctypes.byref(handle)):
            self._handle = int(handle.value or 0) or None
        else:
            logger.info("voice priority unavailable (qWAVE error {})", ctypes.get_last_error())

    def add(self, addr: tuple[str, int]) -> bool:
        """Mark the audio sent to ``addr`` (IPv4, port); True once marked."""
        key = (str(addr[0]), int(addr[1]))
        with self._lock:
            if key in self._flows:
                return True
            if self._handle is None:
                return False
            try:
                destination = _SockaddrIn(
                    socket.AF_INET,
                    socket.htons(key[1]),
                    (ctypes.c_ubyte * 4)(*socket.inet_aton(key[0])),
                    b"",
                )
                flow_id = wintypes.ULONG(0)
                added = self._qwave.QOSAddSocketToFlow(
                    self._handle,
                    self._sock.fileno(),
                    ctypes.byref(destination),
                    _QOS_TRAFFIC_TYPE_VOICE,
                    _QOS_NON_ADAPTIVE_FLOW,
                    ctypes.byref(flow_id),
                )
            except (OSError, ValueError) as e:
                logger.debug("voice priority not applied to {}: {}", key, e)
                return False
            if not added:
                # Loopback (a Poste on the Régie PC) is always refused: nothing
                # to prioritise there.
                logger.debug("voice priority not applied to {} (qWAVE error {})", key, ctypes.get_last_error())
                return False
            self._flows[key] = int(flow_id.value)
            return True

    def remove(self, addr: tuple[str, int]) -> None:
        key = (str(addr[0]), int(addr[1]))
        with self._lock:
            flow_id = self._flows.pop(key, None)
            if flow_id is None or self._handle is None:
                return
            try:
                self._qwave.QOSRemoveSocketFromFlow(self._handle, self._sock.fileno(), flow_id, 0)
            except (OSError, ValueError) as e:
                logger.debug("voice priority not removed from {}: {}", key, e)

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            self._flows.clear()
        if handle is not None:
            try:
                self._qwave.QOSCloseHandle(handle)
            except OSError as e:
                logger.debug("qWAVE handle not closed: {}", e)

    @property
    def marked(self) -> list[tuple[str, int]]:
        with self._lock:
            return list(self._flows)
