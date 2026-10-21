"""Return ducking: lower the program feed while someone is talking to you.

One ``ReturnDucker`` exists per destination and is driven once per 10 ms
frame by the broadcast worker. It ducks fast, holds through short pauses
between words, then releases slowly, so the program does not "pump".
"""

from __future__ import annotations

from .constants import (
    RETURN_DUCK_ATTACK_DB_PER_FRAME,
    RETURN_DUCK_DEPTH_DB,
    RETURN_DUCK_HOLD_FRAMES,
    RETURN_DUCK_RELEASE_DB_PER_FRAME,
    RETURN_DUCK_THRESHOLD_DBFS,
)


class ReturnDucker:
    def __init__(
        self,
        *,
        depth_db: float = RETURN_DUCK_DEPTH_DB,
        threshold_dbfs: float = RETURN_DUCK_THRESHOLD_DBFS,
        attack_db_per_frame: float = RETURN_DUCK_ATTACK_DB_PER_FRAME,
        release_db_per_frame: float = RETURN_DUCK_RELEASE_DB_PER_FRAME,
        hold_frames: int = RETURN_DUCK_HOLD_FRAMES,
    ) -> None:
        self.depth_db = -abs(float(depth_db))
        self.threshold_dbfs = float(threshold_dbfs)
        self.attack_db_per_frame = abs(float(attack_db_per_frame))
        self.release_db_per_frame = abs(float(release_db_per_frame))
        self.hold_frames = max(0, int(hold_frames))
        self._gain_db = 0.0
        self._hold = 0

    @property
    def gain_db(self) -> float:
        """Current attenuation applied to the Return, in dB (0 or negative)."""
        return self._gain_db

    def reset(self) -> None:
        self._gain_db = 0.0
        self._hold = 0

    def update(self, voice_dbfs: float) -> float:
        """Advance one frame given the level of the voices heard; return the gain."""
        if voice_dbfs >= self.threshold_dbfs:
            self._hold = self.hold_frames
            self._gain_db = max(self.depth_db, self._gain_db - self.attack_db_per_frame)
        elif self._hold > 0:
            self._hold -= 1
        else:
            self._gain_db = min(0.0, self._gain_db + self.release_db_per_frame)
        return self._gain_db
