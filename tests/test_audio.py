import math

import numpy as np

from py_intercom.common.audio import (
    apply_gain_db,
    db_to_linear,
    float32_to_int16_bytes,
    int16_bytes_to_float32,
    limit_peak,
    rms_dbfs,
)


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


def test_limit_peak_passthrough_when_below_one():
    x = np.array([0.5, -0.7, 0.99], dtype=np.float32)
    y = limit_peak(x)
    assert np.array_equal(x, y)


def test_limit_peak_attenuates_when_above_one():
    x = np.array([2.0, -2.0], dtype=np.float32)
    y = limit_peak(x, limit=0.99)
    assert float(np.max(np.abs(y))) <= 0.99 + 1e-6


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
