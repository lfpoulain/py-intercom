from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final


def _seq_distance(a: int, b: int) -> int:
    return int(((int(a) - int(b) + (1 << 31)) & 0xFFFFFFFF) - (1 << 31))


@dataclass
class JitterStats:
    received: int = 0
    played: int = 0
    late_dropped: int = 0
    missing: int = 0
    concealed: int = 0
    resets: int = 0


@dataclass(frozen=True, slots=True)
class JitterFrameMetadata:
    """Wire metadata kept atomically with an Opus payload.

    Zero values are intentional: they are the fail-closed metadata used by
    legacy pushes and locally generated packet-loss concealment frames.
    """

    session_token: int = 0
    client_id: int = 0
    timestamp_ms: int = 0
    stream_id: int = 0
    bus_mask: int = 0
    flags: int = 0


FAIL_CLOSED_METADATA: Final[JitterFrameMetadata] = JitterFrameMetadata()


@dataclass(frozen=True, slots=True)
class JitterFrame:
    sequence_number: int
    payload: bytes
    metadata: JitterFrameMetadata = FAIL_CLOSED_METADATA
    concealed: bool = False


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
    ) -> None:
        self.start_frames = int(max(1, start_frames))
        self.max_frames = int(max(self.start_frames + 1, max_frames))

        self._buf: dict[int, JitterFrame] = {}
        self._expected_seq: int | None = None
        self._started: bool = False
        self.stats = JitterStats()
        self._lock = threading.Lock()

    @property
    def buffered_frames(self) -> int:
        with self._lock:
            return int(len(self._buf))

    @property
    def expected_seq(self) -> int | None:
        with self._lock:
            return None if self._expected_seq is None else int(self._expected_seq)

    def reset(self) -> None:
        with self._lock:
            self._buf.clear()
            self._expected_seq = None
            self._started = False
            self.stats.resets += 1

    def push(
        self,
        seq: int,
        payload: bytes,
        metadata: JitterFrameMetadata | None = None,
    ) -> None:
        s = int(seq) & 0xFFFFFFFF
        if metadata is not None and not isinstance(metadata, JitterFrameMetadata):
            raise TypeError("metadata must be a JitterFrameMetadata instance")
        frame_metadata = FAIL_CLOSED_METADATA if metadata is None else metadata
        with self._lock:
            self.stats.received += 1

            if self._expected_seq is not None:
                dist = _seq_distance(s, int(self._expected_seq))
                if dist < 0:
                    self.stats.late_dropped += 1
                    return
                if dist > int(self.max_frames) * 4:
                    self._buf.clear()
                    self._expected_seq = None
                    self._started = False
                    self.stats.resets += 1

            if s in self._buf:
                return

            try:
                encoded_payload = memoryview(payload).tobytes()
            except Exception:
                return
            self._buf[s] = JitterFrame(
                sequence_number=s,
                payload=encoded_payload,
                metadata=frame_metadata,
            )

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

    def pop(self) -> bytes | None:
        """Return only payload bytes for compatibility with existing callers."""

        frame = self.pop_frame()
        return None if frame is None else frame.payload

    def pop_frame(self) -> JitterFrame | None:
        """Return a payload and its immutable, sequence-aligned metadata."""

        with self._lock:
            if not self._started:
                if len(self._buf) < int(self.start_frames):
                    return None
                keys_sorted = _sequence_order(self._buf.keys())
                # Realtime startup: do not begin from the oldest enqueued packet.
                # Keep only a minimal safety window before playout starts.
                start_idx = max(0, int(len(keys_sorted) - int(self.start_frames)))
                key = int(keys_sorted[start_idx])
                for stale_key in keys_sorted[:start_idx]:
                    self._buf.pop(int(stale_key), None)
                self._expected_seq = int(key)
                self._started = True

            if self._expected_seq is None:
                return None

            exp = int(self._expected_seq) & 0xFFFFFFFF

            if exp in self._buf:
                frame = self._buf.pop(exp)
                self._expected_seq = (exp + 1) & 0xFFFFFFFF
                self.stats.played += 1
                return frame

            if len(self._buf) == 0:
                return None

            # Find nearest frame ahead of expected_seq
            nearest_key = None
            nearest_dist = None
            for k in self._buf.keys():
                d = _seq_distance(int(k), exp)
                if d > 0 and (nearest_dist is None or d < nearest_dist):
                    nearest_dist = d
                    nearest_key = int(k)

            if nearest_key is None:
                return None

            if nearest_dist > int(self.start_frames):
                # Large gap: fast-forward to nearest available frame
                frame = self._buf.pop(nearest_key)
                self._expected_seq = (nearest_key + 1) & 0xFFFFFFFF
                self.stats.played += 1
                self.stats.missing += int(nearest_dist)
                return frame

            # Small gap: advance expected seq WITH PLC (Packet Loss Concealment)
            max_ahead = max(_seq_distance(int(k), exp) for k in self._buf.keys())
            if int(max_ahead) >= int(self.start_frames):
                self._expected_seq = (exp + 1) & 0xFFFFFFFF
                self.stats.played += 1
                self.stats.missing += 1
                self.stats.concealed += 1
                return JitterFrame(
                    sequence_number=exp,
                    payload=b"",
                    metadata=FAIL_CLOSED_METADATA,
                    concealed=True,
                )

            return None
