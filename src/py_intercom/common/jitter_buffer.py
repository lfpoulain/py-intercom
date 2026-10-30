from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from .constants import (
    JB_BURST_EXCESS_FRAMES,
    JB_BURST_TRIM_AFTER_FRAMES,
    JB_MAX_CONCEALED_FRAMES,
    JB_MAX_FRAMES,
    JB_START_FRAMES,
    JB_TRIM_ABOVE_FRAMES,
    LATENCY_TRIM_AFTER_FRAMES,
)


def _seq_distance(a: int, b: int) -> int:
    return int(((int(a) - int(b) + (1 << 31)) & 0xFFFFFFFF) - (1 << 31))


@dataclass
class JitterStats:
    late_dropped: int = 0
    missing: int = 0
    concealed: int = 0
    resets: int = 0
    trimmed: int = 0


@dataclass(frozen=True, slots=True)
class JitterFrameMetadata:
    """Routing metadata kept atomically with an Opus payload.

    Zero values are intentional: they are the fail-closed metadata used by
    legacy pushes and locally generated packet-loss concealment frames.
    """

    session_token: int = 0
    bus_mask: int = 0


FAIL_CLOSED_METADATA: Final[JitterFrameMetadata] = JitterFrameMetadata()


@dataclass(frozen=True, slots=True)
class JitterFrame:
    sequence_number: int
    payload: bytes
    metadata: JitterFrameMetadata = FAIL_CLOSED_METADATA
    concealed: bool = False


def _concealed_frame(sequence_number: int) -> JitterFrame:
    """Packet-loss concealment slot: no payload and fail-closed metadata."""
    return JitterFrame(
        sequence_number=sequence_number,
        payload=b"",
        metadata=FAIL_CLOSED_METADATA,
        concealed=True,
    )


def _sequence_order(keys: Iterable[int]) -> list[int]:
    """Return sequence numbers in wrap-safe order for a bounded window."""

    ordered = [int(key) & 0xFFFFFFFF for key in keys]
    if len(ordered) < 2:
        return ordered
    anchor = int(ordered[0])
    return sorted(ordered, key=lambda key: _seq_distance(int(key), anchor))


class OpusPacketJitterBuffer:
    def __init__(
        self,
        *,
        start_frames: int = 3,
        max_frames: int = 50,
        trim_above: int | None = None,
        trim_after: int = 50,
    ) -> None:
        """``trim_above`` enables the latency cap on the depth left after a pop
        (never below ``start_frames - 1``, the steady-state depth). An excess
        that lasts ``trim_after`` consecutive pops loses one frame, which
        absorbs clock drift. An excess above ``JB_BURST_EXCESS_FRAMES`` that
        lasts ``JB_BURST_TRIM_AFTER_FRAMES`` pops (network burst, stalled
        reader) is dropped at once. Latency can therefore only go back down."""
        self.start_frames = int(max(1, start_frames))
        self.max_frames = int(max(self.start_frames + 1, max_frames))
        self.trim_above = None if trim_above is None else int(max(self.start_frames - 1, trim_above))
        self.trim_after = int(max(1, trim_after))

        self._buf: dict[int, JitterFrame] = {}
        self._expected_seq: int | None = None
        self._started: bool = False
        self._excess_pops = 0
        self._burst_pops = 0
        self._underrun_pops = 0
        # Sequence to resume from after an underrun stopped playout.
        self._resume_seq: int | None = None
        self.stats = JitterStats()
        self._lock = threading.Lock()

    @property
    def buffered_frames(self) -> int:
        with self._lock:
            return int(len(self._buf))

    def reset(self) -> None:
        with self._lock:
            self._buf.clear()
            self._restart_locked()
            self.stats.resets += 1

    def _restart_locked(self) -> None:
        """Forget the playout position: playout waits for a new pre-buffer."""
        self._resume_seq = None
        self._expected_seq = None
        self._started = False
        self._excess_pops = 0
        self._burst_pops = 0
        self._underrun_pops = 0

    def push(
        self,
        seq: int,
        payload: bytes,
        metadata: JitterFrameMetadata | None = None,
    ) -> None:
        s = int(seq) & 0xFFFFFFFF
        if metadata is not None and not isinstance(metadata, JitterFrameMetadata):
            raise TypeError("metadata must be a JitterFrameMetadata instance")
        frame = JitterFrame(
            sequence_number=s,
            payload=memoryview(payload).tobytes(),
            metadata=FAIL_CLOSED_METADATA if metadata is None else metadata,
        )
        with self._lock:
            if self._expected_seq is not None:
                dist = _seq_distance(s, int(self._expected_seq))
                if dist < 0:
                    self.stats.late_dropped += 1
                    return
                if dist > int(self.max_frames) * 4:
                    self._buf.clear()
                    self._restart_locked()
                    self.stats.resets += 1

            if s in self._buf:
                return
            self._buf[s] = frame

            if len(self._buf) > int(self.max_frames):
                if self._expected_seq is None:
                    key = _sequence_order(self._buf.keys())[0]
                    del self._buf[key]
                else:
                    exp = int(self._expected_seq)
                    ahead = []
                    for k in self._buf.keys():
                        d = _seq_distance(int(k), exp)
                        if d >= 0:
                            ahead.append((int(d), int(k)))

                    if len(ahead) > 0:
                        # Realtime policy: drop the oldest pending frame first.
                        # Keeping newest frames avoids slowly drifting into high latency.
                        _oldest_dist, oldest_key = min(ahead, key=lambda t: int(t[0]))
                        del self._buf[int(oldest_key)]
                    else:
                        key = _sequence_order(self._buf.keys())[0]
                        del self._buf[int(key)]

    def pop_frame(self) -> JitterFrame | None:
        """Return the frame due now, a concealment frame, or None while pre-buffering.

        Called once per 10 ms tick. Once playout has started, every tick gets
        a frame (received or concealed) until the stream really stops.
        """

        with self._lock:
            if not self._started:
                resume = self._resume_seq
                if resume is not None and resume in self._buf:
                    # The same stream goes on where it stopped (next talk
                    # spurt, delayed packets): play it at once, a new
                    # pre-buffer would add its whole margin as latency.
                    start_key = int(resume)
                elif len(self._buf) < int(self.start_frames):
                    return None
                else:
                    # Realtime startup: do not begin from the oldest enqueued
                    # packet, keep only a minimal safety window.
                    keys_sorted = _sequence_order(self._buf.keys())
                    start_key = int(keys_sorted[len(keys_sorted) - int(self.start_frames)])
                stale = [key for key in self._buf if _seq_distance(int(key), start_key) < 0]
                for stale_key in stale:
                    del self._buf[stale_key]
                if resume is not None:
                    # Older than the resume point: they arrived too late.
                    self.stats.late_dropped += len(stale)
                self._resume_seq = None
                self._expected_seq = start_key
                self._started = True

            exp = int(self._expected_seq) & 0xFFFFFFFF
            frame = self._buf.pop(exp, None)
            if frame is not None:
                self._expected_seq = (exp + 1) & 0xFFFFFFFF
                self._underrun_pops = 0
                self._trim_excess_locked()
                return frame

            if self._buf:
                ahead = [_seq_distance(int(k), exp) for k in self._buf]
                nearest_dist = min(ahead)
                if nearest_dist > int(self.start_frames):
                    # Large gap: fast-forward to the nearest available frame.
                    nearest_key = (exp + nearest_dist) & 0xFFFFFFFF
                    frame = self._buf.pop(nearest_key)
                    self._expected_seq = (nearest_key + 1) & 0xFFFFFFFF
                    self.stats.missing += int(nearest_dist)
                    self._underrun_pops = 0
                    return frame
                if max(ahead) >= int(self.start_frames) - 1:
                    # Later frames already fill the target depth, so the
                    # expected one is lost: conceal it at its deadline.
                    self._expected_seq = (exp + 1) & 0xFFFFFFFF
                    self.stats.missing += 1
                    self.stats.concealed += 1
                    self._underrun_pops = 0
                    return _concealed_frame(exp)

            # Underrun: the expected frame is not here yet. Conceal the tick
            # without skipping it, so a frame that is only late still plays.
            # A longer silence (end of a talk spurt, network outage) stops
            # playout; it resumes from that same frame as soon as it arrives.
            if self._underrun_pops >= JB_MAX_CONCEALED_FRAMES:
                self._restart_locked()
                self._resume_seq = exp
                return None
            self._underrun_pops += 1
            self.stats.concealed += 1
            return _concealed_frame(exp)

    def _trim_excess_locked(self) -> None:
        if self.trim_above is None:
            return
        excess = len(self._buf) - self.trim_above
        if excess <= 0:
            self._excess_pops = 0
            self._burst_pops = 0
            return
        self._excess_pops += 1
        self._burst_pops = self._burst_pops + 1 if excess > JB_BURST_EXCESS_FRAMES else 0
        if self._burst_pops >= JB_BURST_TRIM_AFTER_FRAMES:
            drop = excess
        elif self._excess_pops >= self.trim_after:
            drop = 1
        else:
            return
        self._excess_pops = 0
        self._burst_pops = 0
        self._drop_oldest_locked(drop)

    def _drop_oldest_locked(self, count: int) -> None:
        """Skip the ``count`` oldest buffered frames; playout resumes after them."""
        exp = int(self._expected_seq)
        ordered = sorted(self._buf, key=lambda key: _seq_distance(int(key), exp))
        newest_dropped = int(ordered[count - 1])
        for key in ordered[:count]:
            del self._buf[key]
        self._expected_seq = (newest_dropped + 1) & 0xFFFFFFFF
        self.stats.trimmed += int(count)
        # Holes skipped together with the trimmed frames are lost for good.
        self.stats.missing += _seq_distance(newest_dropped, exp) + 1 - int(count)


def new_realtime_jitter_buffer() -> OpusPacketJitterBuffer:
    """Jitter buffer with the engine's pre-buffer, hard cap and latency cap."""
    return OpusPacketJitterBuffer(
        start_frames=JB_START_FRAMES,
        max_frames=JB_MAX_FRAMES,
        trim_above=JB_TRIM_ABOVE_FRAMES,
        trim_after=LATENCY_TRIM_AFTER_FRAMES,
    )
