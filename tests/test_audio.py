import math

import numpy as np
import pytest

from py_intercom.common.audio import (
    MIN_GAIN_DB,
    apply_gain_db,
    clamp_gain_db,
    db_to_linear,
    float32_to_int16_bytes,
    int16_bytes_to_float32,
    limit_peak_inplace,
    rms_dbfs,
)
from py_intercom.common.constants import MAX_GAIN_DB


def test_db_to_linear_zero():
    assert db_to_linear(0.0) == 1.0


def test_db_to_linear_known_values():
    assert math.isclose(db_to_linear(20.0), 10.0, rel_tol=1e-9)
    assert math.isclose(db_to_linear(-20.0), 0.1, rel_tol=1e-9)
    assert math.isclose(db_to_linear(-6.0), 0.5011872336272722, rel_tol=1e-6)


def test_apply_gain_db_zero_returns_copy_not_alias():
    """Regression: apply_gain_db(x, 0.0) used to return x by reference,
    so callers mutating the result silently corrupted the caller's array."""
    x = np.array([0.5, -0.5], dtype=np.float32)
    y = apply_gain_db(x, 0.0)
    assert np.array_equal(x, y)
    y[0] = 99.0
    assert x[0] == 0.5  # x must NOT have been mutated through y


def test_apply_gain_db_scales():
    x = np.array([1.0, -1.0], dtype=np.float32)
    y = apply_gain_db(x, -6.0)
    assert math.isclose(float(y[0]), 0.5011872, rel_tol=1e-4)
    assert math.isclose(float(y[1]), -0.5011872, rel_tol=1e-4)


def test_rms_dbfs_silence_floor():
    x = np.zeros(480, dtype=np.float32)
    assert rms_dbfs(x) == -60.0


def test_rms_dbfs_full_scale_sine():
    n = 4800
    t = np.arange(n, dtype=np.float32) / 48000.0
    x = np.sin(2 * np.pi * 1000.0 * t).astype(np.float32)
    db = rms_dbfs(x)
    # Full-scale sine -> RMS = 1/sqrt(2) -> -3.01 dBFS
    assert -3.5 < db < -2.5


def test_rms_dbfs_clamped_to_floor():
    x = np.full(480, 1e-9, dtype=np.float32)
    assert rms_dbfs(x, floor_db=-60.0) == -60.0


def test_rms_dbfs_matches_mean_square_and_ignores_non_finite_input():
    x = np.full(480, 0.5, dtype=np.float32)
    assert math.isclose(rms_dbfs(x), 20.0 * math.log10(0.5), abs_tol=1e-4)
    x[0] = np.nan
    assert rms_dbfs(x) == -60.0
    assert rms_dbfs(np.zeros(0, dtype=np.float32), floor_db=-120.0) == -120.0


def test_limit_peak_inplace_passthrough_when_below_one():
    x = np.array([0.5, -0.7, 0.99], dtype=np.float32)
    expected = x.copy()
    y = limit_peak_inplace(x)
    assert y is x
    assert np.array_equal(x, expected)


def test_limit_peak_inplace_scales_the_whole_frame_without_hard_clipping():
    x = np.array([2.0, -1.0, 0.5], dtype=np.float32)
    limit_peak_inplace(x, limit=0.99)
    # Scaled as a whole: the waveform shape is kept, nothing is flattened.
    np.testing.assert_allclose(x, [0.99, -0.495, 0.2475], rtol=1e-6)


def test_limit_peak_inplace_silences_non_finite_frames():
    for bad in (np.nan, np.inf, -np.inf):
        x = np.array([0.5, bad, -0.5], dtype=np.float32)
        limit_peak_inplace(x)
        assert np.array_equal(x, np.zeros(3, dtype=np.float32))


@pytest.mark.parametrize(
    "value, expected",
    [
        (3.0, 3.0),
        ("6", 6.0),
        (MAX_GAIN_DB + 40.0, MAX_GAIN_DB),
        (-500.0, MIN_GAIN_DB),
        (float("nan"), 0.0),
        (float("inf"), 0.0),
        (None, 0.0),
        ("loud", 0.0),
    ],
)
def test_clamp_gain_db_is_finite_and_bounded(value, expected):
    assert clamp_gain_db(value) == expected


def test_clamp_gain_db_uses_the_given_default():
    assert clamp_gain_db(float("nan"), default=-12.0) == -12.0


def test_int16_roundtrip_preserves_unit_samples():
    x = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    raw = float32_to_int16_bytes(x)
    y = int16_bytes_to_float32(raw)
    # Quantization noise should be tiny for these values
    assert np.allclose(x, y, atol=1e-3)


def test_int16_clips_out_of_range():
    x = np.array([2.0, -2.0], dtype=np.float32)
    raw = float32_to_int16_bytes(x)
    y = int16_bytes_to_float32(raw)
    assert float(np.max(y)) <= 1.0 + 1e-3
    assert float(np.min(y)) >= -1.0 - 1e-3
