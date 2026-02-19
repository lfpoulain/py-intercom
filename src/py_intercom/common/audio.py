import math

import numpy as np


def db_to_linear(db: float) -> float:
    return float(math.pow(10.0, db / 20.0))


def apply_gain_db(x: np.ndarray, gain_db: float) -> np.ndarray:
    if gain_db == 0.0:
        return x
    return x * db_to_linear(gain_db)


def rms_dbfs(x: np.ndarray, floor_db: float = -60.0) -> float:
    if x.size == 0:
        return floor_db
    rms = float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))
    if rms <= 0.0 or math.isnan(rms):
        return floor_db
    db = 20.0 * math.log10(rms)
    return max(floor_db, min(0.0, db))


def float32_to_int16_bytes(x: np.ndarray) -> bytes:
    y = np.clip(x, -1.0, 1.0)
    y = (y * 32767.0).astype(np.int16, copy=False)
    return y.tobytes(order="C")


def int16_bytes_to_float32(pcm: bytes) -> np.ndarray:
    y = np.frombuffer(pcm, dtype=np.int16)
    x = (y.astype(np.float32) / 32768.0).astype(np.float32, copy=False)
    return x


def limit_peak(x: np.ndarray, limit: float = 0.99) -> np.ndarray:
    try:
        peak = float(np.max(np.abs(x)))
    except Exception:
        return x
    if peak > 1.0 and peak > 0.0:
        x = x * (float(limit) / peak)
    return x
