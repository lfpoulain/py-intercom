from dataclasses import FrozenInstanceError

import pytest

from py_intercom.common.jitter_buffer import (
    FAIL_CLOSED_METADATA,
    JitterFrameMetadata,
    OpusPacketJitterBuffer,
    _seq_distance,
)


def _payload(seq: int) -> bytes:
    return f"p{seq}".encode("ascii")


def test_seq_distance_basic():
    assert _seq_distance(10, 5) == 5
    assert _seq_distance(5, 10) == -5


def test_seq_distance_wraparound():
    # Just before / after the 32-bit wrap
    assert _seq_distance(0, 0xFFFFFFFE) == 2
    assert _seq_distance(0xFFFFFFFE, 0) == -2


def test_pop_returns_none_until_start_threshold():
    jb = OpusPacketJitterBuffer(start_frames=3, max_frames=10)
    jb.push(0, _payload(0))
    jb.push(1, _payload(1))
    assert jb.pop() is None
    assert jb.pop() is None


def test_pop_starts_after_threshold_and_advances():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=10)
    jb.push(10, _payload(10))
    jb.push(11, _payload(11))
    jb.push(12, _payload(12))

    # Once start threshold is met, pop should yield the most recent window.
    out1 = jb.pop()
    assert out1 is not None
    assert jb.pop() == _payload(12) or out1 == _payload(11)


def test_late_packet_dropped():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=10)
    jb.push(50, _payload(50))
    jb.pop()  # advances expected_seq past 50
    jb.push(40, _payload(40))  # well in the past
    assert jb.stats.late_dropped >= 1


def test_far_future_resets_buffer():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=4)
    jb.push(0, _payload(0))
    jb.pop()  # expected_seq is now 1
    # Reset triggers when dist(seq, expected_seq) > max_frames * 4 == 16,
    # so we need a seq at least 18 ahead of expected_seq (= 1).
    far = 1 + (4 * 4) + 2
    jb.push(far, _payload(far))
    assert jb.stats.resets >= 1


def test_plc_emits_empty_payload_on_small_gap():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=20)
    jb.push(0, _payload(0))
    jb.push(1, _payload(1))
    jb.pop()  # consume one
    # Skip seq 2, push seq 3 and 4. Now there's a 1-frame gap and we have
    # buffered frames ahead, which should trigger PLC (empty bytes payload).
    jb.push(3, _payload(3))
    jb.push(4, _payload(4))
    seen_plc = False
    for _ in range(8):
        out = jb.pop()
        if out == b"":
            seen_plc = True
            break
    assert seen_plc
    assert jb.stats.concealed >= 1


def test_reset_clears_state():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=5)
    jb.push(7, _payload(7))
    jb.reset()
    assert jb.buffered_frames == 0
    assert jb.expected_seq is None
    assert jb.stats.resets >= 1


def test_max_frames_caps_buffer():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=3)
    for s in range(20):
        jb.push(s, _payload(s))
    assert jb.buffered_frames <= 3


def test_pop_frame_preserves_immutable_metadata_atomically():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=5)
    metadata = JitterFrameMetadata(
        session_token=123,
        client_id=7,
        timestamp_ms=456,
        stream_id=0,
        bus_mask=0b101,
        flags=1,
    )
    jb.push(9, b"packet", metadata)

    frame = jb.pop_frame()
    assert frame is not None
    assert frame.sequence_number == 9
    assert frame.payload == b"packet"
    assert frame.metadata is metadata
    assert frame.metadata.bus_mask == 0b101
    assert frame.concealed is False
    with pytest.raises(FrozenInstanceError):
        frame.metadata.bus_mask = 0
    with pytest.raises(FrozenInstanceError):
        frame.payload = b"changed"


def test_legacy_pop_remains_bytes_only_and_metadata_defaults_fail_closed():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=5)
    jb.push(1, bytearray(b"legacy"))
    assert jb.pop() == b"legacy"

    jb.push(2, b"next")
    frame = jb.pop_frame()
    assert frame is not None
    assert frame.metadata is FAIL_CLOSED_METADATA
    assert frame.metadata.bus_mask == 0


def test_plc_frame_has_expected_sequence_and_fail_closed_metadata():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=10)
    metadata = JitterFrameMetadata(session_token=999, client_id=3, bus_mask=0b111, flags=1)
    jb.push(10, b"p10", metadata)
    jb.push(11, b"p11", metadata)
    assert jb.pop_frame().sequence_number == 10
    assert jb.pop_frame().sequence_number == 11
    jb.push(13, b"p13", metadata)
    jb.push(14, b"p14", metadata)

    concealed = jb.pop_frame()
    assert concealed is not None
    assert concealed.sequence_number == 12
    assert concealed.payload == b""
    assert concealed.concealed is True
    assert concealed.metadata is FAIL_CLOSED_METADATA
    assert concealed.metadata.bus_mask == 0
    assert concealed.metadata.session_token == 0


def test_wraparound_playout_keeps_payload_and_metadata_order():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=5)
    before_wrap = JitterFrameMetadata(timestamp_ms=1, bus_mask=0b001)
    after_wrap = JitterFrameMetadata(timestamp_ms=2, bus_mask=0b010)
    jb.push(0xFFFFFFFF, b"before", before_wrap)
    jb.push(0, b"after", after_wrap)

    first = jb.pop_frame()
    second = jb.pop_frame()
    assert first is not None and second is not None
    assert (first.sequence_number, first.payload, first.metadata) == (
        0xFFFFFFFF,
        b"before",
        before_wrap,
    )
    assert (second.sequence_number, second.payload, second.metadata) == (
        0,
        b"after",
        after_wrap,
    )
    assert jb.expected_seq == 1


def test_overflow_drops_oldest_frame_without_detaching_metadata():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=3)
    jb.push(0, b"initial")
    assert jb.pop_frame().sequence_number == 0
    for sequence in range(1, 5):
        jb.push(
            sequence,
            f"payload-{sequence}".encode(),
            JitterFrameMetadata(timestamp_ms=sequence, bus_mask=1 << sequence),
        )

    assert jb.buffered_frames == 3
    dropped = jb.pop_frame()
    assert dropped is not None
    assert dropped.sequence_number == 1
    assert dropped.concealed is True
    assert dropped.metadata is FAIL_CLOSED_METADATA
    for expected_sequence in (2, 3, 4):
        frame = jb.pop_frame()
        assert frame is not None
        assert frame.sequence_number == expected_sequence
        assert frame.payload == f"payload-{expected_sequence}".encode()
        assert frame.metadata.timestamp_ms == expected_sequence
        assert frame.metadata.bus_mask == 1 << expected_sequence


def test_push_rejects_mutable_or_untyped_metadata():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=3)
    with pytest.raises(TypeError, match="JitterFrameMetadata"):
        jb.push(1, b"payload", {"bus_mask": 1})


def _drifting_sender(jb, *, pops: int, extra_every: int) -> list[int]:
    """Sender ~1/extra_every faster than the receiver; returns depth after each pop."""
    seq = 0
    depths = []
    for _ in range(4):
        jb.push(seq, b"x")
        seq += 1
    for i in range(pops):
        jb.push(seq, b"x")
        seq += 1
        if i % extra_every == 0:
            jb.push(seq, b"x")
            seq += 1
        assert jb.pop_frame() is not None
        depths.append(jb.buffered_frames)
    return depths


def test_without_latency_cap_clock_drift_accumulates_delay() -> None:
    jb = OpusPacketJitterBuffer(start_frames=4, max_frames=30)

    depths = _drifting_sender(jb, pops=2000, extra_every=100)

    assert depths[-1] >= 20


def test_latency_cap_absorbs_clock_drift() -> None:
    jb = OpusPacketJitterBuffer(start_frames=4, max_frames=30, trim_above=5, trim_after=50)

    depths = _drifting_sender(jb, pops=2000, extra_every=100)

    assert max(depths[500:]) <= 6
    assert jb.stats.trimmed > 0


def test_latency_cap_recovers_after_a_network_burst() -> None:
    jb = OpusPacketJitterBuffer(start_frames=4, max_frames=30, trim_above=5, trim_after=50)
    seq = 0
    for _ in range(14):  # pre-buffer plus a 10-frame burst
        jb.push(seq, b"x")
        seq += 1
    for _ in range(600):
        jb.push(seq, b"x")
        seq += 1
        jb.pop_frame()

    assert jb.buffered_frames <= 5


def test_latency_cap_never_trims_a_stable_stream() -> None:
    jb = OpusPacketJitterBuffer(start_frames=4, max_frames=30, trim_above=5, trim_after=50)

    _drifting_sender(jb, pops=2000, extra_every=10**9)

    assert jb.stats.trimmed == 0
