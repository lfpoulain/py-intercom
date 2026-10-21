from __future__ import annotations

import gc
import threading
import time
import weakref

import numpy as np
import pytest

from py_intercom.common.frame_ring import FrameRingBuffer


def _frame(value: float, samples: int = 4) -> np.ndarray:
    return np.full(samples, value, dtype=np.float32)


def test_preserves_order_and_aligned_metadata() -> None:
    ring = FrameRingBuffer(capacity=4, frame_samples=4)
    output = np.empty(4, dtype=np.float32)

    ring.push(_frame(1.0), metadata=101)
    ring.push(_frame(2.0), metadata=-202)

    assert ring.pop_into(output) == 101
    np.testing.assert_array_equal(output, _frame(1.0))
    assert ring.pop_into(output) == -202
    np.testing.assert_array_equal(output, _frame(2.0))

    assert ring.stats.depth == 0
    assert ring.stats.pushed == 2
    assert ring.stats.popped == 2


def test_full_ring_drops_oldest_frame() -> None:
    ring = FrameRingBuffer(capacity=3, frame_samples=4)
    output = np.empty(4, dtype=np.float32)

    for value in range(5):
        ring.push(_frame(float(value)), metadata=value)

    assert ring.depth == 3
    assert ring.stats.pushed == 5
    assert ring.stats.dropped == 2

    for expected in (2, 3, 4):
        assert ring.pop_into(output) == expected
        np.testing.assert_array_equal(output, _frame(float(expected)))

    unchanged = output.copy()
    assert ring.pop_into(output) is None
    np.testing.assert_array_equal(output, unchanged)
    assert ring.stats.underflows == 1


def test_push_and_pop_do_not_alias_caller_arrays() -> None:
    ring = FrameRingBuffer(capacity=2, frame_samples=4)
    source = _frame(3.0)
    source_reference = weakref.ref(source)

    ring.push(source, metadata=7)
    source.fill(99.0)
    del source
    gc.collect()

    # The ring copied the samples and did not retain the producer's ndarray.
    assert source_reference() is None

    output = np.empty(4, dtype=np.float32)
    assert ring.pop_into(output) == 7
    np.testing.assert_array_equal(output, _frame(3.0))

    output.fill(-1.0)
    ring.push(_frame(8.0), metadata=8)
    assert ring.pop_into(output) == 8
    np.testing.assert_array_equal(output, _frame(8.0))


def test_clear_discards_pending_frames_but_preserves_counters() -> None:
    ring = FrameRingBuffer(capacity=3, frame_samples=4)
    output = np.full(4, 123.0, dtype=np.float32)

    ring.push(_frame(1.0), metadata=1)
    ring.push(_frame(2.0), metadata=2)

    assert ring.clear() == 2
    assert ring.depth == 0
    assert ring.stats.pushed == 2
    assert ring.stats.dropped == 0
    assert ring.pop_into(output) is None
    np.testing.assert_array_equal(output, _frame(123.0))

    ring.push(_frame(4.0), metadata=4)
    assert ring.pop_into(output) == 4
    np.testing.assert_array_equal(output, _frame(4.0))


def test_wait_pop_is_interruptible_without_recording_underflow() -> None:
    ring = FrameRingBuffer(capacity=2, frame_samples=4)
    stop_event = threading.Event()
    waiter_started = threading.Event()
    result: list[object] = []

    def consume() -> None:
        output = np.empty(4, dtype=np.float32)
        waiter_started.set()
        result.append(
            ring.wait_pop_into(output, stop_event=stop_event, timeout=None)
        )

    thread = threading.Thread(target=consume)
    thread.start()
    assert waiter_started.wait(timeout=1.0)
    time.sleep(0.02)

    stop_event.set()
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert result == [None]
    assert ring.stats.underflows == 0


def test_wait_pop_timeout_records_underflow_and_keeps_output_unchanged() -> None:
    ring = FrameRingBuffer(capacity=2, frame_samples=4)
    output = _frame(42.0)

    assert ring.wait_pop_into(output, timeout=0.01) is None
    np.testing.assert_array_equal(output, _frame(42.0))
    assert ring.stats.underflows == 1


def test_spsc_concurrency_preserves_every_frame_and_metadata() -> None:
    frame_count = 1_000
    ring = FrameRingBuffer(capacity=frame_count, frame_samples=8)
    start = threading.Event()
    producer_done = threading.Event()
    consumed: list[tuple[int, float]] = []

    def produce() -> None:
        source = np.empty(8, dtype=np.float32)
        start.wait(timeout=1.0)
        for sequence in range(frame_count):
            source.fill(float(sequence))
            ring.push(source, metadata=sequence)
        producer_done.set()

    def consume() -> None:
        output = np.empty(8, dtype=np.float32)
        start.wait(timeout=1.0)
        while len(consumed) < frame_count:
            metadata = ring.wait_pop_into(output, timeout=1.0)
            if metadata is not None:
                consumed.append((metadata, float(output[0])))

    producer = threading.Thread(target=produce)
    consumer = threading.Thread(target=consume)
    producer.start()
    consumer.start()
    start.set()

    producer.join(timeout=2.0)
    consumer.join(timeout=2.0)

    assert producer_done.is_set()
    assert not producer.is_alive()
    assert not consumer.is_alive()
    assert consumed == [(value, float(value)) for value in range(frame_count)]
    assert ring.stats.pushed == frame_count
    assert ring.stats.popped == frame_count
    assert ring.stats.dropped == 0
    assert ring.stats.underflows == 0


@pytest.mark.parametrize(
    ("frame", "exception"),
    [
        (np.zeros(4, dtype=np.float64), TypeError),
        (np.zeros((4, 1), dtype=np.float32), ValueError),
        (np.zeros(3, dtype=np.float32), ValueError),
    ],
)
def test_rejects_frames_with_incompatible_layout(
    frame: np.ndarray,
    exception: type[Exception],
) -> None:
    ring = FrameRingBuffer(capacity=2, frame_samples=4)

    with pytest.raises(exception):
        ring.push(frame)


def test_rejects_metadata_outside_signed_int64() -> None:
    ring = FrameRingBuffer(capacity=2, frame_samples=4)

    with pytest.raises(OverflowError):
        ring.push(_frame(1.0), metadata=1 << 63)


def test_stop_event_takes_priority_over_queued_audio() -> None:
    ring = FrameRingBuffer(capacity=2, frame_samples=4)
    stop_event = threading.Event()
    output = np.empty(4, dtype=np.float32)
    ring.push(_frame(1.0), metadata=1)
    stop_event.set()

    assert ring.wait_pop_into(output, stop_event=stop_event) is None
    assert ring.depth == 1
    assert ring.stats.underflows == 0


def test_latency_cap_bounds_a_slightly_faster_producer() -> None:
    ring = FrameRingBuffer(capacity=20, frame_samples=4, trim_above=2, trim_after=50)
    frame = np.zeros(4, dtype=np.float32)
    out = np.empty(4, dtype=np.float32)
    depths = []
    for i in range(3000):
        ring.push(frame)
        if i % 100 == 0:
            ring.push(frame)
        ring.pop_into(out)
        depths.append(ring.depth)

    assert max(depths[500:]) <= 3
    assert ring.stats.trimmed > 0
    assert ring.stats.dropped == 0


def test_latency_cap_is_disabled_by_default() -> None:
    ring = FrameRingBuffer(capacity=20, frame_samples=4)
    frame = np.zeros(4, dtype=np.float32)
    out = np.empty(4, dtype=np.float32)
    for _ in range(5):
        ring.push(frame)
    for _ in range(200):
        ring.push(frame)
        ring.pop_into(out)

    assert ring.depth == 5
    assert ring.stats.trimmed == 0
