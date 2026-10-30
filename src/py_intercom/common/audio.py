import math

import numpy as np

from .constants import MAX_GAIN_DB

# Bottom of every gain fader (bus, output, client input, Return).
MIN_GAIN_DB = -60.0


def db_to_linear(db: float) -> float:
    return float(math.pow(10.0, db / 20.0))


def clamp_gain_db(value: object, default: float = 0.0) -> float:
    """Return ``value`` as a finite gain bounded to the fader range.

    Unparsable or non-finite input yields ``default``: a hand-edited preset or
    a bad caller must never bring NaN or an absurd gain into the mix.
    """
    try:
        gain = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(gain):
        return float(default)
    return max(MIN_GAIN_DB, min(float(MAX_GAIN_DB), gain))


def apply_gain_db(x: np.ndarray, gain_db: float) -> np.ndarray:
    # Always return a fresh array so callers can mutate the result safely
    # (e.g. accumulate into a bus mix, clip in place) without aliasing the input.
    if gain_db == 0.0:
        return np.array(x, dtype=np.float32, copy=True)
    return (x * db_to_linear(gain_db)).astype(np.float32, copy=False)


def rms_dbfs(x: np.ndarray, floor_db: float = -60.0) -> float:
    """RMS level in dBFS, clamped to [floor_db, 0]. Allocation-free (RT-safe)."""
    if x.size == 0:
        return float(floor_db)
    mean_square = float(np.dot(x, x)) / float(x.size)
    if not math.isfinite(mean_square) or mean_square <= 0.0:
        return float(floor_db)
    return max(float(floor_db), min(0.0, 10.0 * math.log10(mean_square)))


def limit_peak_inplace(x: np.ndarray, limit: float = 0.99) -> np.ndarray:
    """Final destination limiter: scale ``x`` in place so its peak fits.

    Allocation-free, so it is safe in PortAudio callbacks. A frame whose peak
    exceeds full scale is scaled down to ``limit``; a non-finite frame becomes
    silence, so corrupt audio never reaches a sound card or an encoder.
    """
    if x.size == 0:
        return x
    peak = max(float(np.max(x)), -float(np.min(x)))
    if not math.isfinite(peak):
        x.fill(0.0)
    elif peak > 1.0:
        x *= float(limit) / peak
    np.clip(x, -1.0, 1.0, out=x)
    return x


def float32_to_int16_bytes(x: np.ndarray) -> bytes:
    y = np.clip(x, -1.0, 1.0)
    y = (y * 32767.0).astype(np.int16, copy=False)
    return y.tobytes(order="C")


def int16_bytes_to_float32(pcm: bytes) -> np.ndarray:
    y = np.frombuffer(pcm, dtype=np.int16)
    x = (y.astype(np.float32) / 32768.0).astype(np.float32, copy=False)
    return x
