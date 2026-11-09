from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from py_intercom.common.constants import (
    JB_BURST_EXCESS_FRAMES,
    JB_BURST_FAST_EXCESS_FRAMES,
    JB_BURST_PATIENCE_AFTER_FRAMES,
    JB_BURST_TRIM_AFTER_FRAMES,
    JB_CATCH_UP_AFTER_FRAMES,
    JB_MAX_CONCEALED_FRAMES,
    JB_MAX_FRAMES,
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
    decode_jitter_frame,
    new_realtime_jitter_buffer,
    skip_silent_slot,
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


def test_plc_emits_empty_payload_when_the_next_packet_is_lost_too():
    jb = OpusPacketJitterBuffer(start_frames=2, max_frames=20)
    jb.push(0, _payload(0))
    jb.push(1, _payload(1))
    jb.pop_frame()  # consume one
    # Seq 2 and 3 are lost: no following packet to rebuild seq 2 from.
    jb.push(4, _payload(4))
    jb.push(5, _payload(5))

    assert jb.pop_frame().sequence_number == 1
    concealed = jb.pop_frame()
    assert concealed.concealed is True
    assert concealed.payload == b""
    assert concealed.fec_payload == b""
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
    # The overflowing frame is skipped, playout moves on past it (see
    # test_a_full_buffer_does_not_stay_stuck_at_its_cap).
    assert jb.stats.trimmed == 1
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
    assert (jb.stats.missing, jb.stats.fec, jb.stats.concealed) == (1, 1, 0)


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

    # A hole longer than the margin plus the concealment plays silence, then
    # playout resumes on the delayed packets.
    concealed = [frame for frame in frames if frame is not None and frame.concealed]
    assert 1 <= len(concealed) <= JB_MAX_CONCEALED_FRAMES
    played = [frame.sequence_number for frame in frames if frame is not None and not frame.concealed]
    assert played == sorted(played) and delayed[0] in played
    # A late packet is still played: nothing was lost, only delayed.
    assert jb.stats.late_dropped == 0
    assert jb.stats.missing == 0
    assert depths[-1] == JB_TRIM_ABOVE_FRAMES


def test_burst_is_trimmed_back_to_target_within_the_patience():
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)

    # The receiver froze for 250 ms: 25 packets wait when it resumes.
    for _ in range(25):
        jb.push(seq, _payload(seq))
        seq += 1
    seq = _run_ticks(jb, seq, ticks=JB_BURST_PATIENCE_AFTER_FRAMES, frames=frames, depths=depths)

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
    # The initial margin absorbs its JB_START_FRAMES - 1 first missing ticks,
    # then each one is concealed once and the late packet still plays.
    assert jb.stats.concealed == 10 - (JB_START_FRAMES - 1)
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


def test_next_talk_spurt_resumes_after_a_short_prebuffer_without_losing_audio():
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
    # Only the short resume pre-buffer, not the full one: the first frame plays
    # on the tick by which the ``resume_frames`` first packets have arrived.
    ready_at = max(arrivals[20 + n] for n in range(jb.resume_frames))
    ready_tick = next(i for i in range(130) if i * 10.0 + 5.0 >= ready_at) - 70
    assert 1 <= jb.resume_frames < jb.start_frames
    assert first_played == ready_tick
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


def test_a_lost_frame_carries_the_next_packet_for_fec():
    jb = new_realtime_jitter_buffer()

    frames, _depths, _seq = _steady_stream(jb, ticks=40, skip={10})

    rebuilt = [frame for frame in frames if frame.concealed]
    assert [frame.sequence_number for frame in rebuilt] == [10]
    assert rebuilt[0].fec_payload == _payload(11)
    assert (jb.stats.fec, jb.stats.concealed, jb.stats.missing) == (1, 0, 1)


def test_decode_uses_fec_only_when_the_next_packet_is_there():
    calls = []

    class _Decoder:
        def decode(self, payload, *, fec=False):
            calls.append((payload, fec))

    decode_jitter_frame(_Decoder(), JitterFrame(5, b"own"))
    decode_jitter_frame(_Decoder(), JitterFrame(6, b"", concealed=True, fec_payload=b"next"))
    decode_jitter_frame(_Decoder(), JitterFrame(7, b"", concealed=True))

    assert calls == [(b"own", False), (b"next", True), (b"", False)]


def test_a_full_buffer_does_not_stay_stuck_at_its_cap():
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)

    # A 350 ms hole, then everything arrives at once: more than the hard cap.
    for _ in range(JB_MAX_FRAMES + 5):
        jb.push(seq, _payload(seq))
        seq += 1
    after = []
    seq = _run_ticks(jb, seq, ticks=JB_BURST_PATIENCE_AFTER_FRAMES + 20, frames=after, depths=depths)

    # Skipping past the dropped frame keeps playout on real audio: it used to
    # conceal every tick while the depth stayed at the cap.
    assert all(frame is not None and not frame.concealed for frame in after)
    assert depths[-1] == JB_TRIM_ABOVE_FRAMES
    played = [frame.sequence_number for frame in after]
    assert played == sorted(played)
    assert jb.stats.concealed == 0


def _jb_with_excess(excess_frames: int):
    """A started buffer holding ``excess_frames`` above its steady depth for a while."""
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)
    for _ in range(excess_frames):
        jb.push(seq, _payload(seq))
        seq += 1
    for _ in range(JB_CATCH_UP_AFTER_FRAMES):
        seq = _run_ticks(jb, seq, ticks=1, frames=frames, depths=depths)
    return jb, seq


def _loud(frame):
    return np.full(480, 0.1, dtype=np.float32)


def _quiet(frame):
    return np.full(480, 1e-4, dtype=np.float32)


def test_a_pause_sheds_the_excess_depth_without_cutting_speech():
    jb, _seq = _jb_with_excess(4)
    assert jb.catch_up_due()
    depth = jb.buffered_frames
    popped = jb.pop_frame()

    played, pcm = skip_silent_slot(jb, popped, _quiet(popped), _quiet)

    assert played.sequence_number == popped.sequence_number + 1
    assert not played.concealed
    assert jb.stats.trimmed == 1
    assert jb.buffered_frames == depth - 2  # the popped frame and the skipped one


def test_speech_is_never_skipped_to_catch_up():
    jb, _seq = _jb_with_excess(4)
    popped = jb.pop_frame()

    played, _pcm = skip_silent_slot(jb, popped, _loud(popped), _loud)

    assert played is popped
    assert jb.stats.trimmed == 0


def test_catch_up_ignores_jitter_and_stops_once_the_excess_is_small():
    jb = new_realtime_jitter_buffer()
    frames, depths, seq = _steady_stream(jb, ticks=20)
    # One or two frames above steady for a long time is plain jitter (the depth
    # of a buffer riding out 25-40 ms of it): the adapted cushion stays.
    for _ in range(JB_BURST_EXCESS_FRAMES):
        jb.push(seq, _payload(seq))
        seq += 1
    for _ in range(3 * JB_CATCH_UP_AFTER_FRAMES):
        jb.push(seq, _payload(seq))
        seq += 1
        popped = jb.pop_frame()
        assert not jb.catch_up_due()
        assert skip_silent_slot(jb, popped, _quiet(popped), _quiet)[0] is popped
    assert jb.stats.trimmed == 0

    # A stall leaves more: the silent slots shed it down to the jitter margin.
    jb, seq = _jb_with_excess(4)
    skipped = 0
    for _ in range(10):
        jb.push(seq, _payload(seq))
        seq += 1
        popped = jb.pop_frame()
        played, _pcm = skip_silent_slot(jb, popped, _quiet(popped), _quiet)
        skipped += played is not popped
    assert skipped == 4 - JB_BURST_EXCESS_FRAMES
    assert jb.stats.trimmed == skipped


def test_a_concealed_slot_is_never_skipped():
    jb, _seq = _jb_with_excess(4)
    concealed = JitterFrame(sequence_number=1, payload=b"", concealed=True)

    played, _pcm = skip_silent_slot(jb, concealed, _quiet(concealed), _quiet)

    assert played is concealed


def test_a_talk_spurt_waits_for_the_resume_prebuffer_but_delayed_packets_play_at_once():
    jb = new_realtime_jitter_buffer(wifi=True)
    assert jb.resume_frames == 3
    _frames, _depths, seq = _steady_stream(jb, ticks=30, first_seq=0)
    # The talk ends: concealment, then playout stops and remembers its place.
    for _ in range(40):
        jb.pop_frame()
    assert jb.pop_frame() is None

    # Next spurt: its first packets are the ones expected.
    jb.push(seq, _payload(seq))
    assert jb.pop_frame() is None  # one frame is not enough
    jb.push(seq + 1, _payload(seq + 1))
    assert jb.pop_frame() is None
    jb.push(seq + 2, _payload(seq + 2))
    first = jb.pop_frame()
    assert first is not None and first.sequence_number == seq
    assert jb.stats.late_dropped == 0

    # Packets that were only delayed arrive together: already there, play at once.
    jb2 = new_realtime_jitter_buffer(wifi=True)
    _f, _d, seq2 = _steady_stream(jb2, ticks=30, first_seq=0)
    for _ in range(40):
        jb2.pop_frame()
    assert jb2.pop_frame() is None
    for late in range(seq2, seq2 + jb2.resume_frames):
        jb2.push(late, _payload(late))
    assert jb2.pop_frame().sequence_number == seq2


def _stopped_after_a_spurt(**kwargs):
    """A buffer whose playout stopped after a talk spurt, waiting for sequence ``resume``."""
    jb = new_realtime_jitter_buffer(**kwargs)
    _frames, _depths, seq = _steady_stream(jb, ticks=30)
    while jb.pop_frame() is not None:
        pass
    return jb, seq


def test_a_lone_packet_still_plays_after_the_resume_wait():
    jb, seq = _stopped_after_a_spurt()
    assert jb.resume_frames == 2

    # The tail of the previous spurt arrives alone (or a one-packet tap): the
    # wait for a second packet is bounded, then it plays as it always did.
    jb.push(seq, _payload(seq))
    waits = 0
    frame = jb.pop_frame()
    while frame is None:
        waits += 1
        assert waits <= jb.resume_frames
        frame = jb.pop_frame()
    assert frame.sequence_number == seq
    assert not frame.concealed
    assert waits == jb.resume_frames


def test_frames_older_than_the_resume_point_do_not_count_for_the_resume_wait():
    jb, seq = _stopped_after_a_spurt()

    # A late duplicate of an earlier frame plus the expected one: only one real
    # frame, so playout still waits for the next packet.
    jb.push(seq - 1, _payload(seq - 1))
    jb.push(seq, _payload(seq))
    assert jb.pop_frame() is None
    jb.push(seq + 1, _payload(seq + 1))
    frame = jb.pop_frame()
    assert frame is not None and frame.sequence_number == seq
    assert jb.buffered_frames == 1  # the second real frame is still buffered


def test_a_large_excess_is_trimmed_fast_and_a_moderate_one_waits_for_a_pause():
    for excess, trimmed_after in (
        (JB_BURST_FAST_EXCESS_FRAMES + 3, JB_BURST_TRIM_AFTER_FRAMES),
        (JB_BURST_FAST_EXCESS_FRAMES - 1, JB_BURST_PATIENCE_AFTER_FRAMES),
    ):
        jb = new_realtime_jitter_buffer()
        frames, depths, seq = _steady_stream(jb, ticks=20)
        for _ in range(excess):
            jb.push(seq, _payload(seq))
            seq += 1
        seq = _run_ticks(jb, seq, ticks=trimmed_after - 1, frames=frames, depths=depths)
        assert jb.stats.trimmed == 0
        seq = _run_ticks(jb, seq, ticks=1, frames=frames, depths=depths)
        assert jb.stats.trimmed == excess
        assert depths[-1] == JB_TRIM_ABOVE_FRAMES
