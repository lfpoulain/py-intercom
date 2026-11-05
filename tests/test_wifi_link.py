import socket
import sys

import pytest

from py_intercom.common.constants import (
    JB_MAX_CONCEALED_FRAMES,
    JB_START_FRAMES,
    WIFI_JB_MAX_CONCEALED_FRAMES,
    WIFI_JB_START_FRAMES,
)
from py_intercom.common.jitter_buffer import new_realtime_jitter_buffer
from py_intercom.common.qos import VoiceQos


def _stall(jb, *, stall_ticks: int) -> list:
    """Steady stream, then ``stall_ticks`` without any packet (Wi-Fi scan), then
    the delayed packets all at once; returns every popped frame."""
    seq = 0
    for _ in range(jb.start_frames):
        jb.push(seq, bytes([seq & 0xFF]))
        seq += 1
    frames = []
    for _ in range(50):
        jb.push(seq, bytes([seq & 0xFF]))
        seq += 1
        frames.append(jb.pop_frame())
    delayed = range(seq, seq + stall_ticks)
    for _ in delayed:
        frames.append(jb.pop_frame())
    for late_seq in delayed:
        jb.push(late_seq, bytes([late_seq & 0xFF]))
    seq += stall_ticks
    for _ in range(50):
        jb.push(seq, bytes([seq & 0xFF]))
        seq += 1
        frames.append(jb.pop_frame())
    return frames


def test_wifi_profile_is_deeper_and_conceals_longer():
    wired = new_realtime_jitter_buffer()
    wifi = new_realtime_jitter_buffer(wifi=True)

    assert (wired.start_frames, wired.max_concealed) == (JB_START_FRAMES, JB_MAX_CONCEALED_FRAMES)
    assert (wifi.start_frames, wifi.max_concealed) == (WIFI_JB_START_FRAMES, WIFI_JB_MAX_CONCEALED_FRAMES)
    assert wifi.trim_above == WIFI_JB_START_FRAMES - 1


def test_wifi_profile_plays_a_50_ms_stall_without_any_glitch():
    wired = _stall(new_realtime_jitter_buffer(), stall_ticks=5)
    wifi = _stall(new_realtime_jitter_buffer(wifi=True), stall_ticks=5)

    # Wired margin: concealment, then silence while the packets are late.
    assert any(frame is None or frame.concealed for frame in wired)
    # Wi-Fi margin: every slot plays its own packet, nothing is dropped.
    assert all(frame is not None and not frame.concealed for frame in wifi)
    played = [frame.sequence_number for frame in wifi]
    assert played == list(range(played[0], played[0] + len(played)))


def test_wifi_profile_bridges_a_100_ms_stall_without_cutting_playout():
    frames = _stall(new_realtime_jitter_buffer(wifi=True), stall_ticks=10)

    assert all(frame is not None for frame in frames)
    assert sum(frame.concealed for frame in frames) <= WIFI_JB_MAX_CONCEALED_FRAMES


def test_wifi_profile_returns_to_its_own_depth_after_a_stall():
    jb = new_realtime_jitter_buffer(wifi=True)
    _stall(jb, stall_ticks=10)

    assert jb.buffered_frames == WIFI_JB_START_FRAMES - 1


@pytest.mark.skipif(sys.platform != "win32", reason="qWAVE is Windows only")
def test_voice_priority_marks_a_lan_address_and_skips_loopback():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    qos = VoiceQos(sock)
    try:
        assert qos.add(("192.0.2.10", 5000))
        assert qos.add(("192.0.2.10", 5000))  # already marked
        assert not qos.add(("127.0.0.1", 5000))
        assert not qos.add(("not-an-address", 5000))
        assert qos.marked == [("192.0.2.10", 5000)]
        qos.remove(("192.0.2.10", 5000))
        assert qos.marked == []
    finally:
        qos.close()
        sock.close()
    assert not qos.add(("192.0.2.10", 5000))


def test_voice_priority_is_a_no_op_without_qwave(monkeypatch):
    import py_intercom.common.qos as qos_mod

    monkeypatch.setattr(qos_mod, "_load_qwave", lambda: None)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        qos = VoiceQos(sock)
        assert not qos.add(("192.0.2.10", 5000))
        qos.remove(("192.0.2.10", 5000))
        qos.close()
    finally:
        sock.close()
