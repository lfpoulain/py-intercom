"""Operating system tuning for the realtime audio path (Windows).

Everything here is best effort: a refused call is logged once and the
engine keeps running with the default scheduling.
"""

from __future__ import annotations

import ctypes
import socket
import sys
from collections.abc import Callable
from ctypes import wintypes
from typing import Final

from loguru import logger

_ABOVE_NORMAL_PRIORITY_CLASS: Final = 0x00008000
# The interpreter hands the GIL over every 5 ms by default: half a 10 ms
# frame that a PortAudio callback or the mixer could spend waiting for it.
_GIL_SWITCH_INTERVAL_S: Final = 0.001
_MMCSS_TASK: Final = "Pro Audio"
_SIO_UDP_CONNRESET: Final = 0x9800000C  # _WSAIOW(IOC_VENDOR, 12)


def raise_process_priority() -> None:
    """Favour this process over desktop software without starving the system.

    ABOVE_NORMAL, never HIGH or REALTIME: an encoder or vMix on the same
    machine must keep running.
    """
    sys.setswitchinterval(_GIL_SWITCH_INTERVAL_S)
    if sys.platform != "win32":
        return
    kernel32 = ctypes.windll.kernel32
    # Declared types: the default int conversion truncates the 64-bit
    # pseudo-handle and the call fails with ERROR_INVALID_HANDLE.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), _ABOVE_NORMAL_PRIORITY_CLASS):
        logger.warning("process priority unchanged (error {})", ctypes.GetLastError())


def run_as_audio_thread(target: Callable[..., None], *args: object, **kwargs: object) -> None:
    """Run ``target(*args, **kwargs)`` registered with MMCSS as a "Pro Audio" thread.

    The Multimedia Class Scheduler raises the thread above ordinary work for
    as long as it runs, which keeps the 10 ms deadline under load.
    """
    handle = _mmcss_register()
    try:
        target(*args, **kwargs)
    finally:
        if handle:
            ctypes.windll.avrt.AvRevertMmThreadCharacteristics(ctypes.c_void_p(handle))


def _mmcss_register() -> int:
    if sys.platform != "win32":
        return 0
    try:
        avrt = ctypes.windll.avrt
    except OSError:
        logger.warning("MMCSS unavailable (avrt.dll missing): audio threads keep the default priority")
        return 0
    avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
    task_index = ctypes.c_ulong(0)
    handle = avrt.AvSetMmThreadCharacteristicsW(_MMCSS_TASK, ctypes.byref(task_index))
    if not handle:
        logger.warning("MMCSS registration refused (error {})", ctypes.GetLastError())
        return 0
    return int(handle)


def configure_udp_socket(sock: socket.socket) -> None:
    """Stop ICMP "port unreachable" from surfacing as WinError 10054 on reads.

    Without it, one datagram sent to a station that just left makes the
    next ``recvfrom`` fail, although the socket itself is fine.
    """
    if sys.platform != "win32":
        return
    # socket.ioctl() only knows a few control codes: call WSAIoctl directly.
    ws2 = ctypes.windll.ws2_32
    ws2.WSAIoctl.argtypes = (
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    report = wintypes.BOOL(False)
    returned = wintypes.DWORD(0)
    if ws2.WSAIoctl(
        sock.fileno(),
        _SIO_UDP_CONNRESET,
        ctypes.byref(report),
        ctypes.sizeof(report),
        None,
        0,
        ctypes.byref(returned),
        None,
        None,
    ):
        logger.warning("SIO_UDP_CONNRESET not applied (error {})", ws2.WSAGetLastError())


def configure_control_socket(sock: socket.socket) -> None:
    """Send each control line at once (no Nagle delay behind a delayed ACK)."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as exc:
        logger.warning("TCP_NODELAY not applied: {}", exc)
