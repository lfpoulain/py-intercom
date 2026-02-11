from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import float32_to_int16_bytes, int16_bytes_to_float32
from .constants import CHANNELS, FRAME_SAMPLES, OPUS_BITRATE, OPUS_COMPLEXITY, SAMPLE_RATE


@dataclass
class OpusConfig:
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    frame_samples: int = FRAME_SAMPLES
    bitrate: int = OPUS_BITRATE
    complexity: int = OPUS_COMPLEXITY


class OpusEncoder:
    def __init__(self, config: OpusConfig | None = None):
        self.config = config or OpusConfig()
        try:
            import opuslib
        except Exception as e:
            raise RuntimeError("opuslib is required") from e
        try:
            self._encoder = opuslib.Encoder(self.config.sample_rate, self.config.channels, opuslib.APPLICATION_AUDIO)
            self._encoder.bitrate = self.config.bitrate
            self._encoder.complexity = self.config.complexity
        except Exception as e:
            raise RuntimeError("failed to initialize Opus encoder (libopus missing?)") from e

    def encode(self, frame_f32: np.ndarray) -> bytes:
        if frame_f32.shape[0] != self.config.frame_samples:
            raise ValueError("invalid frame size")
        pcm = float32_to_int16_bytes(frame_f32)
        return self._encoder.encode(pcm, self.config.frame_samples)


class OpusDecoder:
    def __init__(self, config: OpusConfig | None = None):
        self.config = config or OpusConfig()
        try:
            import opuslib
        except Exception as e:
            raise RuntimeError("opuslib is required") from e
        try:
            self._decoder = opuslib.Decoder(self.config.sample_rate, self.config.channels)
        except Exception as e:
            raise RuntimeError("failed to initialize Opus decoder (libopus missing?)") from e

    def decode(self, payload: bytes) -> np.ndarray:
        pcm = self._decoder.decode(payload, self.config.frame_samples, decode_fec=False)
        frame = int16_bytes_to_float32(pcm)
        if frame.shape[0] != self.config.frame_samples:
            frame = frame[: self.config.frame_samples]
        return frame
