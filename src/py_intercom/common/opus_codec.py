from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import float32_to_int16_bytes, int16_bytes_to_float32
from .constants import (
    CHANNELS,
    FRAME_SAMPLES,
    OPUS_BITRATE,
    OPUS_COMPLEXITY,
    OPUS_DECODER_COMPLEXITY,
    SAMPLE_RATE,
)

_OPUS_SAMPLE_RATES = frozenset((8_000, 12_000, 16_000, 24_000, 48_000))
_MAX_OPUS_PACKET_BYTES = 1_275


def _valid_frame_sizes(sample_rate: int) -> frozenset[int]:
    """Return the frame sizes accepted by Opus for one coding frame.

    Opus accepts 2.5, 5, 10, 20, 40 and 60 ms frames.  Keeping this check in
    the wrapper makes a duration mismatch fail before it reaches the native
    library, where the resulting exception otherwise depends on the backend.
    """

    return frozenset(
        (
            sample_rate // 400,
            sample_rate // 200,
            sample_rate // 100,
            sample_rate // 50,
            sample_rate // 25,
            sample_rate * 3 // 50,
        )
    )


def _validate_config(config: OpusConfig) -> None:
    if isinstance(config.sample_rate, bool) or not isinstance(config.sample_rate, int):
        raise TypeError("sample_rate must be an integer")
    if config.sample_rate not in _OPUS_SAMPLE_RATES:
        raise ValueError("unsupported Opus sample rate")
    if isinstance(config.channels, bool) or not isinstance(config.channels, int):
        raise TypeError("channels must be an integer")
    if config.channels != 1:
        raise ValueError("Py-Intercom Opus streams must be mono")
    if isinstance(config.frame_samples, bool) or not isinstance(config.frame_samples, int):
        raise TypeError("frame_samples must be an integer")
    if config.frame_samples not in _valid_frame_sizes(config.sample_rate):
        raise ValueError("invalid Opus frame duration")
    if isinstance(config.bitrate, bool) or not isinstance(config.bitrate, int):
        raise TypeError("bitrate must be an integer")
    if not 500 <= config.bitrate <= 512_000:
        raise ValueError("Opus bitrate must be between 500 and 512000 bit/s")
    if isinstance(config.complexity, bool) or not isinstance(config.complexity, int):
        raise TypeError("complexity must be an integer")
    if not 0 <= config.complexity <= 10:
        raise ValueError("Opus complexity must be between 0 and 10")
    if isinstance(config.decoder_complexity, bool) or not isinstance(
        config.decoder_complexity, int
    ):
        raise TypeError("decoder_complexity must be an integer")
    if not 0 <= config.decoder_complexity <= 10:
        raise ValueError("Opus decoder complexity must be between 0 and 10")


@dataclass
class OpusConfig:
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    frame_samples: int = FRAME_SAMPLES
    bitrate: int = OPUS_BITRATE
    complexity: int = OPUS_COMPLEXITY
    decoder_complexity: int = OPUS_DECODER_COMPLEXITY


class OpusEncoder:
    def __init__(self, config: OpusConfig | None = None):
        self.config = config or OpusConfig()
        _validate_config(self.config)
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
        frame = np.asarray(frame_f32)
        if frame.ndim != 1 or frame.shape != (self.config.frame_samples,):
            raise ValueError(
                f"Opus input must be mono with shape ({self.config.frame_samples},)"
            )
        if frame.dtype.kind != "f":
            raise TypeError("Opus input samples must use a floating-point dtype")
        if not np.isfinite(frame).all():
            raise ValueError("Opus input samples must all be finite")

        pcm = float32_to_int16_bytes(frame.astype(np.float32, copy=False))
        payload = self._encoder.encode(pcm, self.config.frame_samples)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("Opus encoder returned a non-bytes payload")
        payload = bytes(payload)
        if not payload:
            raise RuntimeError("Opus encoder returned an empty payload")
        if len(payload) > _MAX_OPUS_PACKET_BYTES:
            raise RuntimeError("Opus encoder returned an oversized payload")
        return payload


class OpusDecoder:
    def __init__(self, config: OpusConfig | None = None):
        self.config = config or OpusConfig()
        _validate_config(self.config)
        try:
            import opuslib
            import opuslib.api.ctl as opus_ctl_api
            import opuslib.api.decoder as opus_decoder_api
        except Exception as e:
            raise RuntimeError("opuslib is required") from e
        try:
            self._decoder = opuslib.Decoder(self.config.sample_rate, self.config.channels)
            self._opus_decoder_api = opus_decoder_api
            decoder_state = getattr(self._decoder, "decoder_state", None)
            if decoder_state is None:
                raise RuntimeError("opuslib decoder does not expose its native state")
            opus_decoder_api.decoder_ctl(
                decoder_state,
                opus_ctl_api.set_complexity,
                self.config.decoder_complexity,
            )
            configured_complexity = opus_decoder_api.decoder_ctl(
                decoder_state,
                opus_ctl_api.get_complexity,
            )
            if int(configured_complexity) != int(self.config.decoder_complexity):
                raise RuntimeError(
                    "libopus did not apply the requested decoder complexity"
                )
        except Exception as e:
            raise RuntimeError(
                "failed to initialize the Deep PLC Opus decoder "
                "(libopus 1.5+ built with Deep PLC is required)"
            ) from e

    def decode(self, payload: bytes) -> np.ndarray:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("Opus payload must be bytes-like")
        payload = bytes(payload)
        if len(payload) > _MAX_OPUS_PACKET_BYTES:
            raise ValueError("Opus payload exceeds the 1275-byte codec limit")

        if not payload:
            dec_state = getattr(self._decoder, "decoder_state", None)
            if dec_state is not None:
                pcm = self._opus_decoder_api.decode(
                    dec_state,
                    None,
                    0,
                    self.config.frame_samples,
                    decode_fec=False,
                    channels=self.config.channels,
                )
            else:
                pcm = self._decoder.decode(b"", self.config.frame_samples, decode_fec=False)
        else:
            pcm = self._decoder.decode(payload, self.config.frame_samples, decode_fec=False)

        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise TypeError("Opus decoder returned non-bytes PCM")
        pcm = bytes(pcm)
        if len(pcm) % 2:
            raise ValueError("Opus decoder returned an incomplete int16 sample")

        decoded = int16_bytes_to_float32(pcm)
        # Native bindings return the decoded packet duration, which can be
        # shorter than the configured playout frame after a peer mismatch.
        # Always expose the fixed engine contract: truncate excess samples and
        # pad a short frame with digital silence.
        frame = np.zeros((self.config.frame_samples,), dtype=np.float32)
        copy_samples = min(decoded.shape[0], self.config.frame_samples)
        if copy_samples:
            frame[:copy_samples] = decoded[:copy_samples]
        return frame
