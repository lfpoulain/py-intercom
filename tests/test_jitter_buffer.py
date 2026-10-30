from dataclasses import FrozenInstanceError

import pytest

from py_intercom.common.constants import (
    JB_BURST_TRIM_AFTER_FRAMES,
    JB_MAX_CONCEALED_FRAMES,
    JB_START_FRAMES,
    JB_TRIM_ABOVE_FRAMES,
    LATENCY_TRIM_AFTER_FRAMES,
)
from py_intercom.common.jitter_buffer import (
    FAIL_CLOSED_METADATA,
    JitterFrame,
    JitterFrameMetadata,
    OpusPacketJitterBuffer,
    _seq_distance,
    new_realtime_jitter_buffer,
)


def _payload(seq: int) -> bytes:
    return f"p{seq}".encode("ascii")


def _payload_of(frame: JitterFrame | None) -> bytes | None:
    return None if frame is None else frame.payload


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
    assert jb.pop_frame() is None
    assert jb.pop_frame() is None


def test_pop_starts_on_the_most_recent_window_and_advances():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=10)
    jb.push(10, _payload(10))
    jb.push(11, _payload(11))
    jb.push(12, _payload(12))

    assert _payload_of(jb.pop_frame()) == _payload(11)
    assert _payload_of(jb.pop_frame()) == _payload(12)


def test_late_packet_dropped():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=10)
    jb.push(50, _payload(50))
    assert jb.pop_frame().sequence_number == 50
    jb.push(40, _payload(40))  # well in the past
    assert jb.stats.late_dropped == 1
    assert jb.buffered_frames == 0


def test_far_future_resets_buffer():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=4)
    jb.push(0, _payload(0))
    jb.pop_frame()  # the next expected sequence is now 1
    # Reset triggers when the distance to the expected sequence exceeds
    # max_frames * 4 == 16.
    far = 1 + (4 * 4) + 2
    jb.push(far, _payload(far))
    assert jb.stats.resets == 1
    assert jb.pop_frame().sequence_number == far


def test_plc_emits_empty_payload_on_small_gap():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=20)
    jb.push(0, _payload(0))
    jb.push(1, _payload(1))
    jb.pop_frame()  # consume one
    # Skip seq 2, push seq 3 and 4: seq 2 is lost and must be concealed.
    jb.push(3, _payload(3))
    jb.push(4, _payload(4))

    assert jb.pop_frame().sequence_number == 1
    concealed = jb.pop_frame()
    assert concealed.concealed is True
    assert concealed.payload == b""
    assert concealed.sequence_number == 2
    assert jb.stats.concealed == 1
    assert jb.stats.missing == 1


def test_reset_clears_state():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=5)
    jb.push(7, _payload(7))
    assert jb.pop_frame().sequence_number == 7
    jb.push(8, _payload(8))
    jb.reset()
    assert jb.buffered_frames == 0
    assert jb.stats.resets == 1

    # The playout position is forgotten: an older sequence starts a new stream.
    jb.push(3, _payload(3))
    assert jb.pop_frame().sequence_number == 3
    assert jb.stats.late_dropped == 0


def test_max_frames_caps_buffer():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=3)
    for s in range(20):
        jb.push(s, _payload(s))
    assert jb.buffered_frames <= 3


def test_pop_frame_preserves_immutable_metadata_atomically():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=5)
    metadata = JitterFrameMetadata(session_token=123, bus_mask=0b101)
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


def test_payload_is_copied_to_bytes_and_metadata_defaults_fail_closed():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=5)
    jb.push(1, bytearray(b"legacy"))

    frame = jb.pop_frame()
    assert frame is not None
    assert frame.payload == b"legacy"
    assert type(frame.payload) is bytes
    assert frame.metadata is FAIL_CLOSED_METADATA
    assert frame.metadata.bus_mask == 0


def test_plc_frame_has_expected_sequence_and_fail_closed_metadata():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=10)
    metadata = JitterFrameMetadata(session_token=999, bus_mask=0b111)
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
    before_wrap = JitterFrameMetadata(session_token=1, bus_mask=0b001)
    after_wrap = JitterFrameMetadata(session_token=2, bus_mask=0b010)
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
    jb.push(1, b"next")
    third = jb.pop_frame()
    assert (third.sequence_number, third.concealed) == (1, False)


def test_overflow_drops_oldest_frame_without_detaching_metadata():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=3)
    jb.push(0, b"initial")
    assert jb.pop_frame().sequence_number == 0
    for sequence in range(1, 5):
        jb.push(
            sequence,
            f"payload-{sequence}".encode(),
            JitterFrameMetadata(session_token=sequence, bus_mask=1 << sequence),
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
        assert frame.metadata.session_token == expected_sequence
        assert frame.metadata.bus_mask == 1 << expected_sequence


def test_push_rejects_mutable_or_untyped_metadata_and_payload():
    jb = OpusPacketJitterBuffer(start_frames=1, max_frames=3)
    with pytest.raises(TypeError, match="JitterFrameMetadata"):
        jb.push(1, b"payload", {"bus_mask": 1})
    with pytest.raises(TypeError):
        jb.push(1, 12345)
    assert jb.buffered_frames == 0


# --- Realtime behaviour (one push per sender tick, one pop per reader tick) --


def _steady_stream(jb, *, ticks: int, first_seq: int = 0, skip=()) -> tuple[list, list[int], int]:
    """Pre-buffer then push one packet and pop one frame per tick.

    Returns the popped frames, the depth left after each pop and the next
    sequence number. Sequences in ``skip`` are lost on the network.
    """
    seq = first_seq
    for _ in range(JB_START_FRAMES):
        jb.push(seq, _payload(seq))
        seq += 1
    frames, depths = [], []
    for _ in range(ticks):
        if seq not in skip:
            jb.push(seq, _payload(seq))
        seq += 1
        frames.append(jb.pop_frame())
        depths.append(jb.buffered_frames)
    return frames, depths, seq


def _run_ticks(jb, seq: int, *, ticks: int, frames: list, depths: list) -> int:
    for _ in range(ticks):
        jb.push(seq, _payload(seq))
        seq += 1
        frames.append(jb.pop_frame())
        depths.append(jb.buffered_frames)
    return seq


def test_steady_stream_holds_the_target_depth_without_trimming():
    jb = new_realtime_jitter_buffer()

    frames, depths, _seq = _steady_stream(jb, ticks=2000)

    assert all(frame is not None and not frame.concealed for frame in frames)
    assert set(depths) == {JB_TRIM_ABOVE_FRAMES}
    assert jb.stats.trimmed == 0
    assert jb.stats.concealed == 0


def test_single_loss_is_concealed_at_its_deadline_without_adding_delay():
    jb = new_realtime_jitter_buffer()

    frames, depths, _seq = _steady_stream(jb, ticks=40, skip={10})

    assert all(frame is not None for frame in frames)
    concealed = [frame.sequence_number for frame in frames if frame.concealed]
    assert concealed == [10]
    # At tick n the newest packet sent is JB_START_FRAMES + n: the delay
    # between it and the frame played never moves.
    lags = [JB_START_FRAMES + tick - frame.sequence_number for tick, frame in enumerate(frames)]
    assert set(lags) == {JB_TRIM_ABOVE_FRAMES}
    assert depths[-1] == JB_TRIM_ABOVE_FRAMES
    assert (jb.stats.missing, jb.stats.concealed) == (1, 1)


def test_short_network_hole_is_concealed_and_depth_returns_to_target():
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)

    # 50 ms without any packet, then the delayed packets arrive together.
    delayed = list(range(seq, seq + 5))
    for _ in delayed:
        frames.append(jb.pop_frame())
        depths.append(jb.buffered_frames)
    for late_seq in delayed:
        jb.push(late_seq, _payload(late_seq))
    seq = _run_ticks(jb, seq + 5, ticks=3 * LATENCY_TRIM_AFTER_FRAMES, frames=frames, depths=depths)

    assert all(frame is not None for frame in frames)
    concealed = [frame for frame in frames if frame.concealed]
    assert 1 <= len(concealed) <= JB_MAX_CONCEALED_FRAMES
    # A late packet is still played: nothing was lost, only delayed.
    assert jb.stats.late_dropped == 0
    assert jb.stats.missing == 0
    assert depths[-1] == JB_TRIM_ABOVE_FRAMES


def test_burst_is_trimmed_back_to_target_within_a_hundred_ms():
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)

    # The receiver froze for 250 ms: 25 packets wait when it resumes.
    for _ in range(25):
        jb.push(seq, _payload(seq))
        seq += 1
    seq = _run_ticks(jb, seq, ticks=JB_BURST_TRIM_AFTER_FRAMES, frames=frames, depths=depths)

    assert depths[-1] == JB_TRIM_ABOVE_FRAMES
    assert jb.stats.trimmed == 25
    assert jb.stats.missing == 0
    assert all(frame is not None and not frame.concealed for frame in frames)
    played = [frame.sequence_number for frame in frames]
    assert played == sorted(played)


def test_reader_catch_up_is_never_trimmed():
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)

    # The reader stalled 100 ms while packets kept arriving, then catches up
    # with back-to-back pops, as the server mixer does after a late wake-up.
    for _ in range(10):
        jb.push(seq, _payload(seq))
        seq += 1
    for _ in range(10):
        frames.append(jb.pop_frame())
    seq = _run_ticks(jb, seq, ticks=20, frames=frames, depths=depths)

    assert jb.stats.trimmed == 0
    assert all(frame is not None and not frame.concealed for frame in frames)
    assert depths[-1] == JB_TRIM_ABOVE_FRAMES


def test_slower_sender_is_concealed_without_dropping_audio():
    jb = new_realtime_jitter_buffer()
    seq = 0
    for _ in range(JB_START_FRAMES):
        jb.push(seq, _payload(seq))
        seq += 1
    frames = []
    for tick in range(1000):
        if tick % 100 != 50:  # the sender's clock is 1 % slower
            jb.push(seq, _payload(seq))
            seq += 1
        frames.append(jb.pop_frame())

    assert all(frame is not None for frame in frames)
    # The initial margin absorbs three missing ticks, then each one is
    # concealed once and the late packet still plays.
    assert jb.stats.concealed == 7
    assert jb.stats.missing == 0
    assert jb.stats.late_dropped == 0


def _replay(jb, arrivals_ms: dict[int, float], *, ticks: int) -> list:
    """Pop every 10 ms (5 ms phase); ``arrivals_ms`` maps sequence to arrival."""
    pending = sorted(arrivals_ms.items(), key=lambda item: item[1])
    frames = []
    for tick in range(ticks):
        now_ms = tick * 10.0 + 5.0
        while pending and pending[0][1] <= now_ms:
            seq, _arrival = pending.pop(0)
            jb.push(seq, _payload(seq))
        frames.append(jb.pop_frame())
    return frames


def test_next_talk_spurt_resumes_at_once_without_losing_audio():
    jb = new_realtime_jitter_buffer()

    def jitter_ms(seq: int) -> float:
        return float((seq * 7) % 9)  # 0 to 8 ms

    # Two talk spurts 0.5 s apart; the sender's sequence numbers are contiguous.
    arrivals = {seq: seq * 10.0 + jitter_ms(seq) for seq in range(20)}
    arrivals.update({seq: 500.0 + seq * 10.0 + jitter_ms(seq) for seq in range(20, 70)})
    frames = _replay(jb, arrivals, ticks=130)

    first_spurt_end = max(i for i, f in enumerate(frames[:60]) if f is not None and not f.concealed)
    tail = frames[first_spurt_end + 1 : 70]
    assert [f is not None and f.concealed for f in tail[:JB_MAX_CONCEALED_FRAMES]] == [True] * JB_MAX_CONCEALED_FRAMES
    assert all(f is None for f in tail[JB_MAX_CONCEALED_FRAMES:])

    second = frames[70:]
    first_played = next(i for i, f in enumerate(second) if f is not None)
    # No new pre-buffer: the first frame plays on the tick it arrives.
    arrival_tick = next(i for i in range(130) if i * 10.0 + 5.0 >= arrivals[20]) - 70
    assert first_played == arrival_tick
    assert second[first_played].sequence_number == 20
    # Jitter is concealed without skipping: every frame of the spurt plays.
    spurt = [f.sequence_number for f in second[first_played:] if f is not None and not f.concealed]
    assert spurt == list(range(20, 70))
    assert jb.stats.late_dropped == 0
    assert jb.stats.resets == 0


def test_packets_delayed_past_the_concealment_still_play():
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)

    # 80 ms without any packet, then the delayed packets arrive together.
    delayed = list(range(seq, seq + 8))
    for _ in delayed:
        frames.append(jb.pop_frame())
    for late_seq in delayed:
        jb.push(late_seq, _payload(late_seq))
    seq = _run_ticks(jb, seq + 8, ticks=3 * LATENCY_TRIM_AFTER_FRAMES, frames=frames, depths=depths)

    played = [f.sequence_number for f in frames if f is not None and not f.concealed]
    assert played[: len(played)] == sorted(played)
    assert delayed[0] in played
    assert jb.stats.late_dropped == 0
    # The burst excess is then trimmed back to the target.
    assert depths[-1] == JB_TRIM_ABOVE_FRAMES


def _faster_sender(jb, *, ticks: int) -> list[int]:
    """Sender clock 1 % faster than the reader; returns the depth after each pop."""
    seq = 0
    for _ in range(JB_START_FRAMES):
        jb.push(seq, b"x")
        seq += 1
    depths = []
    for tick in range(ticks):
        jb.push(seq, b"x")
        seq += 1
        if tick % 100 == 0:
            jb.push(seq, b"x")
            seq += 1
        assert jb.pop_frame() is not None
        depths.append(jb.buffered_frames)
    return depths


def test_without_latency_cap_clock_drift_accumulates_delay() -> None:
    jb = OpusPacketJitterBuffer(start_frames=JB_START_FRAMES, max_frames=30)

    depths = _faster_sender(jb, ticks=2000)

    assert depths[-1] >= 20


def test_latency_cap_absorbs_clock_drift() -> None:
    jb = new_realtime_jitter_buffer()

    depths = _faster_sender(jb, ticks=2000)

    assert max(depths[500:]) <= JB_TRIM_ABOVE_FRAMES + 1
    assert jb.stats.trimmed > 0
