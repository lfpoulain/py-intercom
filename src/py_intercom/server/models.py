"""Server data model: persistent profiles and live runtime state."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import sounddevice as sd

from ..common import opus_codec
from ..common.audio import clamp_gain_db
from ..common.constants import (
    FRAME_SAMPLES,
    LATENCY_TRIM_AFTER_FRAMES,
    PCM_RING_TRIM_ABOVE_FRAMES,
    SAMPLE_RATE,
    SERVER_OUT_MAX_BUFFER_S,
)
from ..common.ducking import ReturnDucker
from ..common.frame_ring import FrameRingBuffer
from ..common.jitter_buffer import OpusPacketJitterBuffer, new_realtime_jitter_buffer
from ..common.jsonio import JsonFileStatus
from ..common.opus_codec import OpusDecoder, OpusEncoder


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

    def to_preset(self) -> dict:
        return {
            "name": str(self.name or ""),
            "input_gain_db": clamp_gain_db(self.input_gain_db),
        }

    def copy(self) -> ClientProfile:
        return ClientProfile(
            client_uuid=str(self.client_uuid),
            name=str(self.name or ""),
            input_gain_db=clamp_gain_db(self.input_gain_db),
        )


@dataclass(frozen=True, slots=True)
class SavedDevice:
    """A sound card as a preset names it.

    PortAudio indexes move when cards come and go: the name and host API are
    what identify the card, the index only separates identical names.
    """

    index: int | None = None
    name: str = ""
    hostapi: str = ""

    @property
    def is_set(self) -> bool:
        return self.index is not None or bool(self.name)


@dataclass(frozen=True, slots=True)
class PresetOutput:
    """One physical output of a preset.

    ``device`` is its card in the current inventory, None while that card is
    absent: such an output is kept in the preset, never dropped.
    """

    device: int | None
    saved: SavedDevice
    bus_id: int
    gain_db: float

    def to_preset(self) -> dict:
        """The entry as the preset saved it, for an output whose card is absent."""
        return {
            "device": self.saved.index,
            "device_name": self.saved.name,
            "device_hostapi": self.saved.hostapi,
            "bus_id": int(self.bus_id),
            "gain_db": float(self.gain_db),
        }


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
            f"Preset cannot be loaded ({status.value}): {self.path}. Backup: {self.backup_path}. Detail: {self.detail}"
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
    # Owned by the mix worker only: (session_token, bus_mask) of the last real
    # frame. Packet-loss concealment continues that frame, so it inherits this
    # route, and only within the same session.
    last_frame_route: tuple[int, int] = (0, 0)


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
    callback_statuses: int = 0
    vu_dbfs: float = -60.0
    # Last sign of life of the stream: its latest callback, or its start time
    # so a fresh stream gets one full stall window for its first callback.
    last_callback_monotonic: float = 0.0
    stream_error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Serializes (re)opening this output's stream. Never taken by the audio
    # callback, so it may be held across PortAudio calls.
    open_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class LogThrottle:
    """Let one log line per key (e.g. a peer IP) through per time window.

    ``allow`` returns None while the key's window is still closed. Otherwise
    it returns a suffix for the log line counting the lines suppressed since
    the previous one (empty when none). The key table is bounded so a flood
    of spoofed sources cannot grow it.
    """

    def __init__(self, window_s: float, *, max_keys: int = 256) -> None:
        self._window_s = float(window_s)
        self._max_keys = int(max_keys)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, *, now: float) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry[0] < self._window_s:
                self._entries[key] = (entry[0], entry[1] + 1)
                return None
            if entry is None and len(self._entries) >= self._max_keys:
                self._entries.clear()
            self._entries[key] = (float(now), 0)
        suppressed = 0 if entry is None else int(entry[1])
        return f" ({suppressed} similar line(s) suppressed)" if suppressed else ""
