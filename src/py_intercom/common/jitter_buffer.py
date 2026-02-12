from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np


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


class JitterBuffer:
    def __init__(
        self,
        *,
        frame_samples: int,
        start_frames: int = 5,
        max_frames: int = 50,
        conceal_attenuation: float = 0.98,
    ) -> None:
        self.frame_samples = int(frame_samples)
        self.start_frames = int(max(1, start_frames))
        self.max_frames = int(max(self.start_frames + 1, max_frames))
        self.conceal_attenuation = float(conceal_attenuation)

        self._buf: Dict[int, np.ndarray] = {}
        self._expected_seq: Optional[int] = None
        self._started: bool = False
        self._last_frame: np.ndarray = np.zeros((self.frame_samples,), dtype=np.float32)
        self.stats = JitterStats()

    @property
    def buffered_frames(self) -> int:
        return int(len(self._buf))

    @property
    def expected_seq(self) -> Optional[int]:
        return None if self._expected_seq is None else int(self._expected_seq)

    def reset(self) -> None:
        self._buf.clear()
        self._expected_seq = None
        self._started = False
        self._last_frame = np.zeros((self.frame_samples,), dtype=np.float32)
        self.stats.resets += 1

    def push(self, seq: int, frame: np.ndarray) -> None:
        s = int(seq) & 0xFFFFFFFF
        self.stats.received += 1

        if frame.shape[0] != self.frame_samples:
            try:
                frame = frame[: self.frame_samples]
            except Exception:
                return

        f = frame.astype(np.float32, copy=False)

        if self._expected_seq is not None:
            dist = _seq_distance(s, int(self._expected_seq))
            if dist < 0:
                self.stats.late_dropped += 1
                return
            if dist > int(self.max_frames) * 4:
                self.reset()

        if s in self._buf:
            return

        self._buf[s] = f

        if len(self._buf) > int(self.max_frames):
            if self._expected_seq is None:
                key = sorted(self._buf.keys())[0]
                del self._buf[key]
            else:
                exp = int(self._expected_seq)
                farthest = max(self._buf.keys(), key=lambda k: _seq_distance(int(k), exp))
                del self._buf[int(farthest)]

    def pop(self) -> Optional[np.ndarray]:
        if not self._started:
            if len(self._buf) < int(self.start_frames):
                return None
            key = sorted(self._buf.keys())[0]
            self._expected_seq = int(key)
            self._started = True

        if self._expected_seq is None:
            return None

        exp = int(self._expected_seq) & 0xFFFFFFFF

        if exp in self._buf:
            out = self._buf.pop(exp)
            self._last_frame = out
            self._expected_seq = (exp + 1) & 0xFFFFFFFF
            self.stats.played += 1
            return out

        if len(self._buf) == 0:
            return None

        max_ahead = max(_seq_distance(int(k), exp) for k in self._buf.keys())
        if int(max_ahead) >= int(self.start_frames):
            out = (self._last_frame * float(self.conceal_attenuation)).astype(np.float32, copy=False)
            self._last_frame = out
            self._expected_seq = (exp + 1) & 0xFFFFFFFF
            self.stats.played += 1
            self.stats.missing += 1
            self.stats.concealed += 1
            return out

        return None
