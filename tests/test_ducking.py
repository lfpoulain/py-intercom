from __future__ import annotations

import numpy as np

from py_intercom.common.constants import FRAME_SAMPLES
from py_intercom.common.ducking import ReturnDucker
from py_intercom.common.routing import heard_voice_dbfs

VOICE = -20.0
SILENCE = -90.0


def test_ducks_quickly_when_a_voice_is_heard() -> None:
    ducker = ReturnDucker(depth_db=-15.0, attack_db_per_frame=5.0)

    gains = [ducker.update(VOICE) for _ in range(3)]

    assert gains == [-5.0, -10.0, -15.0]
    assert ducker.update(VOICE) == -15.0


def test_holds_through_short_pauses_then_releases_smoothly() -> None:
    ducker = ReturnDucker(depth_db=-15.0, attack_db_per_frame=5.0, hold_frames=30, release_db_per_frame=0.5)
    for _ in range(5):
        ducker.update(VOICE)

    held = [ducker.update(SILENCE) for _ in range(30)]
    released = [ducker.update(SILENCE) for _ in range(30)]

    assert held == [-15.0] * 30
    assert all(b >= a for a, b in zip(released, released[1:], strict=False))
    assert released[-1] == 0.0


def test_quiet_background_does_not_trigger_ducking() -> None:
    ducker = ReturnDucker(threshold_dbfs=-45.0)

    for _ in range(100):
        assert ducker.update(-50.0) == 0.0


def test_heard_voice_excludes_the_listeners_own_voice() -> None:
    own = np.full(FRAME_SAMPLES, 0.25, dtype=np.float32)

    assert heard_voice_dbfs(own, own, listen_regie=True) < -100.0
    assert heard_voice_dbfs(own, None, listen_regie=True) > -15.0
    assert heard_voice_dbfs(own, None, listen_regie=False) < -100.0
