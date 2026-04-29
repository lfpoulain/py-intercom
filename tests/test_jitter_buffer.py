from py_intercom.common.jitter_buffer import OpusPacketJitterBuffer, _seq_distance


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
