from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .audio import rms_dbfs
from .constants import (
    JB_BURST_EXCESS_FRAMES,
    JB_BURST_FAST_EXCESS_FRAMES,
    JB_BURST_PATIENCE_AFTER_FRAMES,
    JB_BURST_TRIM_AFTER_FRAMES,
    JB_CATCH_UP_AFTER_FRAMES,
    JB_CATCH_UP_QUIET_DBFS,
    JB_MAX_CONCEALED_FRAMES,
    JB_MAX_FRAMES,
    JB_RESUME_FRACTION,
    JB_START_FRAMES,
    LATENCY_TRIM_AFTER_FRAMES,
    WIFI_JB_MAX_CONCEALED_FRAMES,
    WIFI_JB_START_FRAMES,
)

if TYPE_CHECKING:
    import numpy as np

    from .opus_codec import OpusDecoder


def _seq_distance(a: int, b: int) -> int:
    return int(((int(a) - int(b) + (1 << 31)) & 0xFFFFFFFF) - (1 << 31))


@dataclass
class JitterStats:
    late_dropped: int = 0
    missing: int = 0
    concealed: int = 0
    fec: int = 0
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
    """One playout slot.

    A ``concealed`` slot has no payload of its own. When the packet that
    follows the lost one is already buffered, ``fec_payload`` holds it: the
    decoder rebuilds the lost frame from its in-band FEC instead of running
    PLC.
    """

    sequence_number: int
    payload: bytes
    metadata: JitterFrameMetadata = FAIL_CLOSED_METADATA
    concealed: bool = False
    fec_payload: bytes = b""


def _concealed_frame(sequence_number: int, fec_payload: bytes = b"") -> JitterFrame:
    """Packet-loss concealment slot: no payload and fail-closed metadata."""
    return JitterFrame(
        sequence_number=sequence_number,
        payload=b"",
        metadata=FAIL_CLOSED_METADATA,
        concealed=True,
        fec_payload=fec_payload,
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
        max_concealed: int = JB_MAX_CONCEALED_FRAMES,
        burst_trim_after: int = JB_BURST_TRIM_AFTER_FRAMES,
        resume_frames: int = 1,
    ) -> None:
        """``trim_above`` enables the latency cap on the depth left after a pop
        (never below ``start_frames - 1``, the steady-state depth). An excess
        that lasts ``trim_after`` consecutive pops loses one frame, which
        absorbs clock drift. An excess above ``JB_BURST_EXCESS_FRAMES`` that
        lasts ``JB_BURST_TRIM_AFTER_FRAMES`` pops (network burst, stalled
        reader) is dropped at once. Latency can therefore only go back down.
        An underrun is concealed for at most ``max_concealed`` ticks before
        playout stops. The burst trim waits ``burst_trim_after`` pops, or
        JB_BURST_TRIM_AFTER_FRAMES when the excess is above
        JB_BURST_FAST_EXCESS_FRAMES; ``catch_up_skip`` lets the reader shed a
        smaller excess in a pause meanwhile. A stream that goes on after playout
        stopped waits up to ``resume_frames`` pops for that many buffered frames
        (a talk spurt starting with an empty buffer), then plays anyway."""
        self.start_frames = int(max(1, start_frames))
        self.max_frames = int(max(self.start_frames + 1, max_frames))
        self.trim_above = None if trim_above is None else int(max(self.start_frames - 1, trim_above))
        self.trim_after = int(max(1, trim_after))
        self.max_concealed = int(max(0, max_concealed))
        self.burst_trim_after = int(max(1, burst_trim_after))
        self.resume_frames = int(min(self.start_frames, max(1, resume_frames)))

        self._buf: dict[int, JitterFrame] = {}
        self._expected_seq: int | None = None
        self._started: bool = False
        self._excess_pops = 0
        self._burst_pops = 0
        self._underrun_pops = 0
        # Sequence to resume from after an underrun stopped playout.
        self._resume_seq: int | None = None
        self._resume_wait = 0
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
        self._resume_wait = 0
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
                    # Realtime policy: skip the oldest pending frame, playout
                    # moves on past it. Deleting it in place would leave a
                    # hole at the playout position: every later tick would
                    # conceal, the depth would never go down (a trim only
                    # runs after a frame was played) and the stream would
                    # stay degraded at the full cap.
                    self._drop_oldest_locked(1)

    def pop_frame(self) -> JitterFrame | None:
        """Return the frame due now, a concealment frame, or None while pre-buffering.

        Called once per 10 ms tick. Once playout has started, every tick gets
        a frame (received or concealed) until the stream really stops.
        """

        with self._lock:
            if not self._started:
                resume = self._resume_seq
                start_key = None
                if resume is not None and resume in self._buf:
                    # The same stream goes on where it stopped (next talk
                    # spurt, delayed packets): play it from that frame, a full
                    # new pre-buffer would add its whole margin as latency.
                    # Delayed packets arrive together, so they are already
                    # there; a talk spurt waits for ``resume_frames`` frames
                    # from the resume point on (older ones will be dropped),
                    # but never longer than that many ticks: a lone packet,
                    # the tail of the previous spurt, plays as it always did.
                    ready = sum(1 for key in self._buf if _seq_distance(int(key), int(resume)) >= 0)
                    if ready >= self.resume_frames or self._resume_wait >= self.resume_frames:
                        start_key = int(resume)
                    else:
                        self._resume_wait += 1
                        return None
                if start_key is None:
                    if len(self._buf) < int(self.start_frames):
                        return None
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
                self._resume_wait = 0
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
                    self._underrun_pops = 0
                    following = self._buf.get((exp + 1) & 0xFFFFFFFF)
                    if following is not None and following.payload:
                        self.stats.fec += 1
                        return _concealed_frame(exp, following.payload)
                    self.stats.concealed += 1
                    return _concealed_frame(exp)

            # Underrun: the expected frame is not here yet. Conceal the tick
            # without skipping it, so a frame that is only late still plays.
            # A longer silence (end of a talk spurt, network outage) stops
            # playout; it resumes from that same frame as soon as it arrives.
            if self._underrun_pops >= self.max_concealed:
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
        fast = excess > JB_BURST_FAST_EXCESS_FRAMES and self._burst_pops >= JB_BURST_TRIM_AFTER_FRAMES
        if self._burst_pops >= self.burst_trim_after or fast:
            drop = excess
        elif self._excess_pops >= self.trim_after:
            drop = 1
        else:
            return
        self._excess_pops = 0
        self._burst_pops = 0
        self._drop_oldest_locked(drop)

    def _catch_up_wanted_locked(self) -> bool:
        """A real excess (a stall, not jitter) that has lasted a few pops."""
        if not self._started or self.trim_above is None:
            return False
        excess = len(self._buf) - self.trim_above
        return excess > JB_BURST_EXCESS_FRAMES and self._excess_pops >= JB_CATCH_UP_AFTER_FRAMES

    def catch_up_due(self) -> bool:
        """True when the depth has stayed well above its steady level for a while."""
        with self._lock:
            return self._catch_up_wanted_locked()

    def catch_up_skip(self) -> JitterFrame | None:
        """Shed one frame of excess depth: the slot after the one just popped.

        The caller found the frame it popped silent and asks for the next one
        instead, so the latency drops by 10 ms where nobody can hear it.
        Returns None, and changes nothing, unless the excess is still there and
        the next frame is really there.
        """
        with self._lock:
            if self._expected_seq is None or not self._catch_up_wanted_locked():
                return None
            exp = int(self._expected_seq)
            frame = self._buf.pop(exp, None)
            if frame is None:
                return None
            self._expected_seq = (exp + 1) & 0xFFFFFFFF
            self.stats.trimmed += 1
            return frame

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


def new_realtime_jitter_buffer(*, wifi: bool = False) -> OpusPacketJitterBuffer:
    """Jitter buffer with the engine's pre-buffer, hard cap and latency cap.

    ``wifi`` selects the deeper fixed margin of a "Liaison Wi-Fi" station.
    """
    start_frames = WIFI_JB_START_FRAMES if wifi else JB_START_FRAMES
    return OpusPacketJitterBuffer(
        start_frames=start_frames,
        max_frames=JB_MAX_FRAMES,
        trim_above=start_frames - 1,
        trim_after=LATENCY_TRIM_AFTER_FRAMES,
        max_concealed=WIFI_JB_MAX_CONCEALED_FRAMES if wifi else JB_MAX_CONCEALED_FRAMES,
        burst_trim_after=JB_BURST_PATIENCE_AFTER_FRAMES,
        resume_frames=round(start_frames * JB_RESUME_FRACTION),
    )


def decode_jitter_frame(decoder: OpusDecoder, frame: JitterFrame) -> np.ndarray:
    """Decode one playout slot: its own packet, the next packet's FEC, or PLC."""
    if frame.fec_payload:
        return decoder.decode(frame.fec_payload, fec=True)
    return decoder.decode(frame.payload)


def skip_silent_slot(
    jb: OpusPacketJitterBuffer,
    frame: JitterFrame,
    decoded: np.ndarray,
    decode: Callable[[JitterFrame], np.ndarray],
) -> tuple[JitterFrame, np.ndarray]:
    """Latency catch-up in a pause: play the next slot instead of a silent one.

    ``frame`` was just popped and decoded to ``decoded``. When the buffer holds
    more than its steady depth and that frame is below JB_CATCH_UP_QUIET_DBFS,
    it is never played: ``decode`` turns the next one into what is played
    instead. The skipped frame was decoded, so the Opus state stays continuous.
    Anything else returns its arguments unchanged.
    """
    if frame.concealed or not jb.catch_up_due():
        return frame, decoded
    if rms_dbfs(decoded) >= JB_CATCH_UP_QUIET_DBFS:
        return frame, decoded
    following = jb.catch_up_skip()
    if following is None:
        return frame, decoded
    return following, decode(following)
