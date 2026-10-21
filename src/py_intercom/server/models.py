"""Server data model: persistent profiles and live runtime state."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd

from ..common import opus_codec
from ..common.constants import (
    FRAME_SAMPLES,
    LATENCY_TRIM_AFTER_FRAMES,
    MAX_GAIN_DB,
    PCM_RING_TRIM_ABOVE_FRAMES,
    SAMPLE_RATE,
    SERVER_OUT_MAX_BUFFER_S,
)
from ..common.ducking import ReturnDucker
from ..common.frame_ring import FrameRingBuffer
from ..common.jitter_buffer import OpusPacketJitterBuffer, new_realtime_jitter_buffer
from ..common.jsonio import JsonFileStatus
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.routing import RoutingResult


def _profile_gain(value: Any) -> float:
    try:
        gain = float(value)
    except (TypeError, ValueError):
        gain = 0.0
    return max(-60.0, min(float(MAX_GAIN_DB), gain))


@dataclass
class ClientProfile:
    """Persistent client identity and operator-controlled preferences.

    Audio codecs, jitter buffers, sockets and liveness belong to ``ClientState``
    and are deliberately absent here.  Loading a preset can therefore restore
    known operators without allocating any live audio resources.
    """

    client_uuid: str
    name: str = ""
    input_gain_db: float = 0.0

    @classmethod
    def from_preset(cls, client_uuid: str, value: Any) -> ClientProfile:
        raw = value if isinstance(value, dict) else {}
        return cls(
            client_uuid=str(client_uuid),
            name=str(raw.get("name") or ""),
            input_gain_db=_profile_gain(raw.get("input_gain_db", 0.0)),
        )

    def to_preset(self) -> dict:
        return {
            "name": str(self.name or ""),
            "input_gain_db": _profile_gain(self.input_gain_db),
        }

    def copy(self) -> ClientProfile:
        return ClientProfile(
            client_uuid=str(self.client_uuid),
            name=str(self.name or ""),
            input_gain_db=_profile_gain(self.input_gain_db),
        )


_OUTPUT_RING_CAPACITY_FRAMES = max(
    1,
    int(float(SERVER_OUT_MAX_BUFFER_S) * float(SAMPLE_RATE) / float(FRAME_SAMPLES)),
)


class PresetLoadError(RuntimeError):
    def __init__(self, path: Path, status: JsonFileStatus, detail: str) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.status = status
        self.detail = str(detail or "unknown error")
        super().__init__(
            f"Preset cannot be loaded ({status.value}): {self.path}. "
            f"Backup: {self.backup_path}. Detail: {self.detail}"
        )


@dataclass
class ClientState:
    addr: tuple[str, int]
    last_packet_monotonic: float
    # Resolve codec classes when a live session is created. Besides keeping
    # construction lazy, this lets diagnostics replace a broken codec backend
    # without retaining a class object captured at import time.
    decoder: OpusDecoder = field(default_factory=lambda: opus_codec.OpusDecoder(), repr=False)
    encoder: OpusEncoder = field(default_factory=lambda: opus_codec.OpusEncoder(), repr=False)
    jb: OpusPacketJitterBuffer = field(
        default_factory=new_realtime_jitter_buffer,
        repr=False,
    )
    vu_dbfs: float = -60.0
    last_timestamp_ms: int = 0
    last_sequence_number: int = 0
    name: str = ""
    client_uuid: str = ""
    control_connected: bool = False
    last_control_monotonic: float = 0.0
    input_gain_db: float = 0.0
    applies_input_gain_locally: bool = False
    session_token: int = 0

    ptt_buses: dict[int, bool] = field(default_factory=dict)
    listen_return_bus: bool = False
    listen_regie: bool = True
    return_gain_db: float = 0.0
    duck_return: bool = False
    # Owned by the broadcast worker only.
    ducker: ReturnDucker = field(default_factory=ReturnDucker, repr=False)


@dataclass
class AudioBus:
    bus_id: int
    name: str
    gain_db: float = 0.0
    feed_to_regie: bool = True


@dataclass
class OutputState:
    output_id: int
    device: int
    bus_id: int
    gain_db: float = 0.0
    stream: sd.OutputStream | None = None
    samplerate: int = int(SAMPLE_RATE)
    ring: FrameRingBuffer = field(
        default_factory=lambda: FrameRingBuffer(
            capacity=_OUTPUT_RING_CAPACITY_FRAMES,
            frame_samples=FRAME_SAMPLES,
            trim_above=PCM_RING_TRIM_ABOVE_FRAMES,
            trim_after=LATENCY_TRIM_AFTER_FRAMES,
        ),
        repr=False,
    )
    callback_scratch: np.ndarray = field(
        default_factory=lambda: np.zeros((FRAME_SAMPLES,), dtype=np.float32),
        repr=False,
    )
    underflows: int = 0
    vu_dbfs: float = -60.0
    last_callback_monotonic: float = 0.0
    stream_error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True, slots=True)
class _RoutingEnvelope:
    routing_epoch: int
    result: RoutingResult
