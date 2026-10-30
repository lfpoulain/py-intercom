from __future__ import annotations

import socket
import sys
import threading

import pytest

from py_intercom.common import realtime


def test_audio_thread_runs_its_target_with_arguments() -> None:
    seen: list[tuple] = []

    thread = threading.Thread(
        target=realtime.run_as_audio_thread,
        args=(lambda *args, **kwargs: seen.append((args, kwargs)), 1, 2),
        kwargs={"critical": True},
    )
    thread.start()
    thread.join(timeout=2.0)

    assert seen == [((1, 2), {"critical": True})]


def test_audio_thread_reverts_its_priority_even_when_the_target_fails(monkeypatch) -> None:
    reverted: list[int] = []
    monkeypatch.setattr(realtime, "_mmcss_register", lambda: 42)
    monkeypatch.setattr(
        realtime.ctypes,
        "windll",
        type("W", (), {"avrt": type("A", (), {"AvRevertMmThreadCharacteristics": lambda h: reverted.append(h.value)})}),
        raising=False,
    )

    def boom() -> None:
        raise RuntimeError("worker crashed")

    with pytest.raises(RuntimeError):
        realtime.run_as_audio_thread(boom)
    assert reverted == [42]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scheduling")
def test_process_priority_is_above_normal_and_gil_switch_is_short() -> None:
    previous = sys.getswitchinterval()
    try:
        realtime.raise_process_priority()
        kernel32 = realtime.ctypes.windll.kernel32
        kernel32.GetPriorityClass.argtypes = (realtime.wintypes.HANDLE,)
        assert kernel32.GetPriorityClass(kernel32.GetCurrentProcess()) == 0x8000
        assert sys.getswitchinterval() == pytest.approx(0.001)
    finally:
        sys.setswitchinterval(previous)


def test_socket_options_are_applied() -> None:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        realtime.configure_udp_socket(udp)
        realtime.configure_control_socket(tcp)
        assert tcp.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
    finally:
        udp.close()
        tcp.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ICMP reporting")
def test_udp_socket_survives_a_send_to_a_closed_port() -> None:
    closed = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    closed.bind(("127.0.0.1", 0))
    target = closed.getsockname()
    closed.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        realtime.configure_udp_socket(sock)
        sock.settimeout(0.2)
        sock.sendto(b"x", target)
        # Without the option, Windows reports the ICMP reply as WinError 10054.
        with pytest.raises(TimeoutError):
            sock.recvfrom(64)
    finally:
        sock.close()
