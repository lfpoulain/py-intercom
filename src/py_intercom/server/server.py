from __future__ import annotations

import queue
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import apply_gain_db, db_to_linear, limit_peak, rms_dbfs
from ..common.audio_errors import friendly_audio_open_error, shared_stream_extra_settings
from ..common.buses import (
    FIXED_BUSES,
    FIXED_BUS_IDS,
    PLATEAU_BUS_ID,
    REGIE_BUS_ID,
    VMIX_BUS_ID,
    require_bus,
)
from ..common.constants import (
    AUDIO_UDP_PORT,
    CHANNELS,
    CONTROL_PORT_OFFSET,
    CTRL_LIVENESS_TIMEOUT_S,
    FRAME_SAMPLES,
    JB_MAX_FRAMES,
    JB_START_FRAMES,
    MAX_GAIN_DB,
    SAMPLE_RATE,
    SERVER_MIX_QUEUE_MAX,
    SERVER_OUT_MAX_BUFFER_S,
    SERVER_RETURN_FRAMES_MAX,
)
from ..common.control_protocol import (
    CONTROL_HELLO_TIMEOUT_S,
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_SESSIONS,
    ControlProtocolError,
    JsonLineBuffer,
    encode_control_message,
    format_session_token,
    require_control_protocol_version,
)
from ..common.discovery import DiscoveryBeacon
from ..common.devices import list_devices, resolve_device
from ..common.frame_ring import FrameRingBuffer
from ..common.identity import (
    ClientIdentityError,
    client_id_from_uuid,
    find_client_id_collisions,
    validate_client_identity,
)
from ..common.jitter_buffer import JitterFrameMetadata, OpusPacketJitterBuffer
from ..common.jsonio import JsonFileStatus, atomic_write_json, read_json_file_result
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import (
    AudioPacketDirection,
    AudioPacketValidationError,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)
from ..common.persistence import DebouncedJsonWriter
from ..common.preset_schema import (
    PRESET_SCHEMA_VERSION,
    PresetSchemaError,
    migrate_preset,
    stamp_preset,
)
from ..common.routing import (
    RoutedSource,
    RoutingConfig,
    RoutingError,
    RoutingResult,
    build_destination_mix,
    route_sources,
)
from .models import ClientProfile


_FIXED_BUS_MASK = sum(1 << int(bus_id) for bus_id in FIXED_BUS_IDS if 0 <= int(bus_id) < 16)
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
    addr: Tuple[str, int]
    last_packet_monotonic: float
    # Resolve codec classes when a live session is created. Besides keeping
    # construction lazy, this lets diagnostics replace a broken codec backend
    # without retaining a class object captured at import time.
    decoder: OpusDecoder = field(default_factory=lambda: OpusDecoder(), repr=False)
    encoder: OpusEncoder = field(default_factory=lambda: OpusEncoder(), repr=False)
    jb: OpusPacketJitterBuffer = field(
        default_factory=lambda: OpusPacketJitterBuffer(start_frames=JB_START_FRAMES, max_frames=JB_MAX_FRAMES),
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

    ptt_buses: Dict[int, bool] = field(default_factory=dict)
    listen_return_bus: bool = False
    listen_regie: bool = True
    return_gain_db: float = 0.0


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
    stream: Optional[sd.OutputStream] = None
    samplerate: int = int(SAMPLE_RATE)
    ring: FrameRingBuffer = field(
        default_factory=lambda: FrameRingBuffer(
            capacity=_OUTPUT_RING_CAPACITY_FRAMES,
            frame_samples=FRAME_SAMPLES,
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


class IntercomServer:
    def __init__(
        self,
        bind_ip: str = "0.0.0.0",
        port: int = AUDIO_UDP_PORT,
        output_device: Optional[int] = None,
        outputs: Optional[list[dict]] = None,
        preset_path: Optional[str] = None,
        server_name: str = "py-intercom",
        discovery_enabled: bool = True,
        return_input_device: Optional[int] = None,
        return_enabled: bool = False,
    ):
        self.bind_ip = bind_ip
        self.port = port
        self.output_device = output_device

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        except Exception:
            pass
        self._sock.bind((self.bind_ip, self.port))
        self._sock.settimeout(0.5)

        self._opus_ok: bool = True
        self._opus_err: str = ""
        self._opuslib_version: str = ""
        try:
            import opuslib  # type: ignore

            self._opuslib_version = str(getattr(opuslib, "__version__", ""))
            _test_enc = OpusEncoder()
            _test_dec = OpusDecoder()
            test_payload = _test_enc.encode(np.zeros((FRAME_SAMPLES,), dtype=np.float32))
            test_frame = _test_dec.decode(test_payload)
            self._opus_ok = bool(getattr(test_frame, "shape", None) is not None and int(test_frame.shape[0]) == int(FRAME_SAMPLES))
        except Exception as e:
            self._opus_ok = False
            self._opus_err = str(e)

        self._lock = threading.Lock()
        self._clients: Dict[int, ClientState] = {}

        # Fixed today, data-driven so the audio engine no longer duplicates
        # bus identities that belong to the shared catalog.
        self._buses: Dict[int, AudioBus] = {
            definition.bus_id: AudioBus(
                bus_id=definition.bus_id,
                name=definition.name,
                gain_db=0.0,
                feed_to_regie=definition.default_feed_to_regie,
            )
            for definition in FIXED_BUSES
        }

        # Routing envelopes carry a server-wide epoch. Any session replacement
        # invalidates already queued audio before it can reach another client.
        self._mix_queue: queue.Queue[object] = queue.Queue(maxsize=SERVER_MIX_QUEUE_MAX)
        self._stop = threading.Event()
        self._seq_out = 0
        self._routing_epoch = 1
        # Serializes the final epoch check with UDP sendto(). Session changes
        # take this gate while advancing the epoch, closing the check/send
        # TOCTOU window without holding it during encoding or packet building.
        self._routing_io_lock = threading.Lock()

        self._rx_packets = 0
        self._rx_datagrams = 0
        self._rx_bytes = 0
        self._rx_socket_errors = 0
        self._tx_packets = 0
        self._rx_decode_errors = 0
        self._rx_invalid_frame_sizes = 0
        self._rx_unknown_clients = 0
        self._rx_inactive_clients = 0
        self._rx_wrong_source = 0
        self._rx_invalid_packets = 0
        self._rx_invalid_tokens = 0
        self._rx_invalid_directions = 0
        self._rx_invalid_route_masks = 0
        self._last_valid_udp_monotonic = 0.0
        self._tx_encode_errors = 0
        self._tx_socket_errors = 0
        self._mix_queue_drops = 0
        self._stale_mix_epochs_dropped = 0
        # Tracks clients we've already warned about as having no UDP port,
        # so the broadcast loop logs at most once per stale state.
        self._silent_dest_logged: Dict[int, bool] = {}
        self._last_stats_log = time.monotonic()

        self._outputs: Dict[int, OutputState] = {}
        self._next_output_id: int = 0

        if outputs is not None:
            diffusion_fallback = self._default_output_bus_id()
            for o in outputs:
                try:
                    dev = int(o.get("device"))
                    bid = int(o.get("bus_id", 0))
                except Exception:
                    continue
                if int(bid) not in (PLATEAU_BUS_ID, VMIX_BUS_ID):
                    bid = int(diffusion_fallback)
                oid = int(self._next_output_id)
                self._next_output_id += 1
                try:
                    output_gain_db = float(o.get("gain_db", 0.0))
                except Exception:
                    output_gain_db = 0.0
                self._outputs[oid] = OutputState(output_id=oid, device=dev, bus_id=bid, gain_db=float(output_gain_db))
        elif output_device is not None:
            oid = int(self._next_output_id)
            self._next_output_id += 1
            self._outputs[oid] = OutputState(output_id=oid, device=int(output_device), bus_id=self._default_output_bus_id())

        self._mix_thread: Optional[threading.Thread] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None
        self._retry_thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._worker_lock = threading.Lock()
        self._worker_threads: Dict[str, threading.Thread] = {}
        self._worker_errors: Dict[str, str] = {}
        self._ctrl_session_threads: set[threading.Thread] = set()

        self._return_input_device: Optional[int] = int(return_input_device) if return_input_device is not None else None
        self._return_enabled: bool = bool(return_enabled)
        self._return_stream: Optional[sd.InputStream] = None
        self._return_stream_gate: Optional[threading.Event] = None
        self._return_stream_error: str = ""
        self._return_lock = threading.Lock()
        self._return_in_samplerate: int = int(SAMPLE_RATE)
        self._return_frames = FrameRingBuffer(
            capacity=SERVER_RETURN_FRAMES_MAX,
            frame_samples=FRAME_SAMPLES,
        )
        self._return_input_scratch = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
        self._return_vu_dbfs: float = -60.0
        self._regie_vu_dbfs: float = -60.0

        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_thread: Optional[threading.Thread] = None
        self._ctrl_lock = threading.Lock()
        self._ctrl_sessions: Dict[int, socket.socket] = {}
        self._ctrl_send_locks: Dict[int, threading.Lock] = {}
        self._ctrl_rejected_sessions = 0
        self._ctrl_hello_timeouts = 0
        self._ctrl_invalid_identities = 0
        self._ctrl_identity_collisions = 0
        self._ctrl_codec_init_errors = 0

        default_path = Path.home() / "py-intercom" / "server_preset.json"
        self._preset_path = Path(preset_path) if preset_path else default_path
        self._preset_status_lock = threading.Lock()
        self._preset_write_blocked = False
        self._preset_load_error: Optional[PresetLoadError] = None
        self._preset_loaded_source_version: Optional[int] = None
        self._client_profiles: Dict[str, ClientProfile] = {}
        self._identity_collisions: Dict[int, tuple[str, ...]] = {}
        self._preset_writer = DebouncedJsonWriter(
            self._preset_path,
            delay_s=0.35,
            data_factory=self._build_preset_data,
            # Resolve the module global at call time so tests and integrations
            # can replace the atomic writer without creating a second path.
            write_func=lambda path, data: atomic_write_json(path, data),
            on_error=lambda exc: logger.error("server preset write failed: {}", exc),
            thread_name="server-preset-writer",
        )

        self._server_name = server_name
        self._discovery_enabled = discovery_enabled
        self._beacon: Optional[DiscoveryBeacon] = None

    def get_preset_paths_snapshot(self) -> dict:
        return {
            "server_preset": str(self._preset_path),
        }

    def _block_preset_writes(self, error: PresetLoadError) -> None:
        with self._preset_status_lock:
            self._preset_write_blocked = True
            self._preset_load_error = error

    def _clear_preset_write_block(self) -> None:
        with self._preset_status_lock:
            self._preset_write_blocked = False
            self._preset_load_error = None

    def _preset_write_error(self) -> Optional[PresetLoadError]:
        with self._preset_status_lock:
            return self._preset_load_error if self._preset_write_blocked else None

    def _refresh_identity_collisions_locked(self) -> None:
        self._identity_collisions = find_client_id_collisions(self._client_profiles)

    def _identity_conflicts_locked(
        self,
        client_id: int,
        client_uuid: str,
    ) -> tuple[str, ...]:
        conflicts = {
            str(known_uuid)
            for known_uuid in self._client_profiles
            if str(known_uuid) != str(client_uuid)
            and int(client_id_from_uuid(known_uuid)) == int(client_id)
        }
        live = self._clients.get(int(client_id))
        if (
            live is not None
            and str(live.client_uuid or "")
            and str(live.client_uuid) != str(client_uuid)
        ):
            conflicts.add(str(live.client_uuid))
        return tuple(sorted(conflicts))

    def load_preset(self) -> bool:
        p = self._preset_path
        result = read_json_file_result(p)
        if result.status is JsonFileStatus.MISSING:
            self._preset_loaded_source_version = None
            self._clear_preset_write_block()
            return False
        if result.status is not JsonFileStatus.OK:
            error = PresetLoadError(p, result.status, result.error)
            self._block_preset_writes(error)
            logger.error("{}", error)
            raise error
        try:
            migration = migrate_preset(result.value, preset_type="server")
        except PresetSchemaError as exc:
            error = PresetLoadError(p, JsonFileStatus.INVALID, str(exc))
            self._block_preset_writes(error)
            logger.error("{}", error)
            raise error
        data = migration.data

        outputs = data.get("outputs") if isinstance(data.get("outputs"), list) else []
        buses = data.get("buses") if isinstance(data.get("buses"), dict) else {}
        clients = data.get("clients") if isinstance(data.get("clients"), dict) else {}
        profiles = {
            str(client_uuid): ClientProfile.from_preset(str(client_uuid), value)
            for client_uuid, value in clients.items()
            if str(client_uuid or "")
        }
        identity_collisions = find_client_id_collisions(profiles)
        return_enabled = bool(data.get("return_enabled", False))
        return_input_device = data.get("return_input_device")
        try:
            return_input_device = int(return_input_device) if return_input_device is not None else None
        except Exception:
            return_input_device = None
        return_input_device_name = str(data.get("return_input_device_name") or "") if isinstance(data, dict) else ""
        return_input_device_hostapi = str(data.get("return_input_device_hostapi") or "") if isinstance(data, dict) else ""

        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=True, validate=False)
        except Exception:
            devices = []

        return_input_device = resolve_device(
            return_input_device_name,
            return_input_device_hostapi,
            return_input_device,
            devices,
            direction="input",
        )

        feed_by_id = {
            definition.bus_id: bool(definition.default_feed_to_regie)
            for definition in FIXED_BUSES
        }
        gain_by_id = {definition.bus_id: 0.0 for definition in FIXED_BUSES}
        if isinstance(buses, dict):
            for bus_id_str, b in buses.items():
                try:
                    bid = int(bus_id_str)
                except Exception:
                    continue
                if bid not in FIXED_BUS_IDS or not isinstance(b, dict):
                    continue
                definition = require_bus(bid)
                if int(bid) != int(REGIE_BUS_ID):
                    feed_by_id[int(bid)] = bool(
                        b.get("feed_to_regie", definition.default_feed_to_regie)
                    )
                try:
                    gain_by_id[int(bid)] = float(b.get("gain_db", 0.0))
                except Exception:
                    gain_by_id[int(bid)] = 0.0

        logger.debug("load_preset: return_enabled={} return_input_device={}", bool(return_enabled), return_input_device)
        with self._lock:
            self._buses = {
                definition.bus_id: AudioBus(
                    bus_id=definition.bus_id,
                    name=definition.name,
                    gain_db=float(gain_by_id[definition.bus_id]),
                    feed_to_regie=(
                        False
                        if definition.bus_id == REGIE_BUS_ID
                        else bool(feed_by_id[definition.bus_id])
                    ),
                )
                for definition in FIXED_BUSES
            }
            self._return_enabled = bool(return_enabled)
            self._return_input_device = return_input_device

            if isinstance(outputs, list):
                fallback = self._default_output_bus_id()
                for out in list(self._outputs.values()):
                    try:
                        if out.stream is not None:
                            out.stream.stop()
                            out.stream.close()
                    except Exception:
                        pass
                self._outputs.clear()
                self._next_output_id = 0
                for o in outputs:
                    if not isinstance(o, dict):
                        continue
                    try:
                        bid = int(o.get("bus_id", 0))
                    except Exception:
                        continue
                    if int(bid) not in FIXED_BUS_IDS:
                        bid = int(fallback)

                    dev = None
                    try:
                        raw_dev = o.get("device")
                        dev = int(raw_dev) if raw_dev is not None else None
                    except Exception:
                        dev = None
                    dev_name = str(o.get("device_name") or "")
                    dev_hostapi = str(o.get("device_hostapi") or "")
                    dev = resolve_device(
                        dev_name,
                        dev_hostapi,
                        dev,
                        devices,
                        direction="output",
                    )
                    if dev is None:
                        continue

                    oid = int(self._next_output_id)
                    self._next_output_id += 1
                    try:
                        output_gain_db = float(o.get("gain_db", 0.0))
                    except Exception:
                        output_gain_db = 0.0
                    self._outputs[oid] = OutputState(output_id=oid, device=int(dev), bus_id=bid, gain_db=float(output_gain_db))

            # Profiles are persistent configuration only.  Loading them must
            # not allocate codecs, jitter buffers or fake live clients.
            self._client_profiles = {uuid: profile.copy() for uuid, profile in profiles.items()}
            self._identity_collisions = dict(identity_collisions)

        self._preset_loaded_source_version = int(migration.source_version)
        if migration.migrated:
            logger.info(
                "server preset schema migrated in memory: v{} -> v{} ({})",
                int(migration.source_version),
                int(PRESET_SCHEMA_VERSION),
                p,
            )
        if identity_collisions:
            logger.error(
                "server preset contains {} client identity collision(s): {}",
                len(identity_collisions),
                identity_collisions,
            )

        self._clear_preset_write_block()
        return True

    def _build_preset_data(self) -> dict:
        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=False, validate=False)
        except Exception:
            devices = []
        device_map = {int(d.index): d for d in devices}

        with self._lock:
            output_states = [
                {
                    "device": int(o.device),
                    "bus_id": int(o.bus_id),
                    "gain_db": float(o.gain_db),
                }
                for o in self._outputs.values()
            ]
            return_input_device = self._return_input_device
            return_enabled = bool(self._return_enabled)
            bus_snapshot = {
                definition.bus_id: self._buses.get(
                    definition.bus_id,
                    AudioBus(
                        definition.bus_id,
                        definition.name,
                        feed_to_regie=definition.default_feed_to_regie,
                    ),
                )
                for definition in FIXED_BUSES
            }
            profiles = {
                uuid: profile.copy()
                for uuid, profile in self._client_profiles.items()
            }
            for st in self._clients.values():
                u = str(st.client_uuid or "")
                if not u:
                    continue
                previous = profiles.get(u)
                profiles[u] = ClientProfile(
                    client_uuid=u,
                    name=str(st.name or (previous.name if previous is not None else "")),
                    input_gain_db=float(st.input_gain_db),
                )
            self._client_profiles = {
                uuid: profile.copy()
                for uuid, profile in profiles.items()
            }

        clients = {
            uuid: profile.to_preset()
            for uuid, profile in profiles.items()
        }

        outputs = []
        for o in output_states:
            dev_info = device_map.get(int(o["device"]))
            outputs.append(
                {
                    "device": int(o["device"]),
                    "device_name": str(getattr(dev_info, "name", "")) if dev_info is not None else "",
                    "device_hostapi": str(getattr(dev_info, "hostapi", "")) if dev_info is not None else "",
                    "bus_id": int(o["bus_id"]),
                    "gain_db": float(o["gain_db"]),
                }
            )

        return_dev = device_map.get(int(return_input_device)) if return_input_device is not None else None

        buses: Dict[str, dict] = {
            str(definition.bus_id): {
                "name": definition.name,
                "feed_to_regie": (
                    False
                    if definition.bus_id == REGIE_BUS_ID
                    else bool(bus_snapshot[definition.bus_id].feed_to_regie)
                ),
                "gain_db": float(bus_snapshot[definition.bus_id].gain_db),
            }
            for definition in FIXED_BUSES
        }

        server_data = {
            "outputs": outputs,
            "buses": buses,
            "clients": clients,
            "return_enabled": bool(return_enabled),
            "return_input_device": int(return_input_device) if return_input_device is not None else None,
            "return_input_device_name": str(getattr(return_dev, "name", "")) if return_dev is not None else "",
            "return_input_device_hostapi": str(getattr(return_dev, "hostapi", "")) if return_dev is not None else "",
        }

        return stamp_preset(server_data, preset_type="server")

    def save_preset(self) -> None:
        error = self._preset_write_error()
        if error is not None:
            raise error
        self._preset_writer.request()
        self._preset_writer.flush(raise_on_error=True)

    def _autosave_preset(self) -> None:
        error = self._preset_write_error()
        if error is not None:
            logger.warning(
                "server preset autosave blocked because the loaded file is invalid: {}",
                error.path,
            )
            return
        try:
            self._preset_writer.request()
        except Exception as e:
            logger.warning("autosave preset scheduling failed: {}", e)

    def _default_output_bus_id(self) -> int:
        if PLATEAU_BUS_ID in self._buses:
            return PLATEAU_BUS_ID
        if VMIX_BUS_ID in self._buses:
            return VMIX_BUS_ID
        return REGIE_BUS_ID

    def get_buses_snapshot(self) -> Dict[int, dict]:
        with self._lock:
            snapshot: Dict[int, dict] = {}
            for definition in FIXED_BUSES:
                bus = self._buses.get(definition.bus_id)
                if bus is None:
                    bus = AudioBus(
                        bus_id=definition.bus_id,
                        name=definition.name,
                        feed_to_regie=definition.default_feed_to_regie,
                    )
                snapshot[definition.bus_id] = {
                    "bus_id": definition.bus_id,
                    "name": definition.name,
                    "gain_db": float(bus.gain_db),
                    "feed_to_regie": (
                        False
                        if definition.bus_id == REGIE_BUS_ID
                        else bool(bus.feed_to_regie)
                    ),
                }
            return snapshot

    def get_outputs_snapshot(self) -> Dict[int, dict]:
        with self._lock:
            out: Dict[int, dict] = {}
            for output_id, st in self._outputs.items():
                with st.lock:
                    samplerate = int(st.samplerate) if int(st.samplerate) > 0 else int(SAMPLE_RATE)
                    ring_stats = st.ring.stats
                    queued_ms = (
                        1000.0
                        * float(ring_stats.depth * st.ring.frame_samples)
                        / float(samplerate)
                    )
                    stream_active = self._stream_is_active(st.stream)
                    stream_error = str(st.stream_error or "")
                    underflows = int(st.underflows)
                    vu_dbfs = float(st.vu_dbfs)
                    if not stream_active and not stream_error and self._running and not self._stop.is_set():
                        stream_error = "output stream is inactive"
                out[int(output_id)] = {
                    "output_id": int(st.output_id),
                    "device": int(st.device),
                    "bus_id": int(st.bus_id),
                    "gain_db": float(st.gain_db),
                    "samplerate": int(samplerate),
                    "queued_ms": float(queued_ms),
                    "queue_depth": int(ring_stats.depth),
                    "queue_capacity": int(st.ring.capacity),
                    "queue_pushed": int(ring_stats.pushed),
                    "queue_popped": int(ring_stats.popped),
                    "queue_dropped": int(ring_stats.dropped),
                    "queue_underflows": int(ring_stats.underflows),
                    "underflows": underflows,
                    "vu_dbfs": vu_dbfs,
                    "stream_active": bool(stream_active),
                    "stream_error": stream_error,
                }
            return out

    def get_clients_snapshot(self) -> Dict[int, dict]:
        now = time.monotonic()
        with self._lock:
            out: Dict[int, dict] = {}
            identity_collisions = dict(self._identity_collisions)
            for client_uuid, profile in sorted(self._client_profiles.items()):
                client_id = int(client_id_from_uuid(client_uuid))
                if client_id == 0 or client_id in out:
                    continue
                collision_uuids = identity_collisions.get(client_id, ())
                out[client_id] = {
                    "client_id": client_id,
                    "name": str(profile.name or ""),
                    "client_uuid": str(client_uuid),
                    "input_gain_db": float(profile.input_gain_db),
                    "addr": None,
                    "age_s": None,
                    "vu_dbfs": -60.0,
                    "last_timestamp_ms": 0,
                    "last_sequence_number": 0,
                    "jb_buf": 0,
                    "jb_missing": 0,
                    "jb_late_dropped": 0,
                    "jb_concealed": 0,
                    "control_connected": False,
                    "control_age_s": None,
                    "ptt_buses": {},
                    "listen_return_bus": False,
                    "listen_regie": True,
                    "identity_collision": bool(collision_uuids),
                    "identity_collision_uuids": list(collision_uuids),
                }
            for client_id, st in self._clients.items():
                try:
                    jb_buf = int(st.jb.buffered_frames)
                    jb_stats = st.jb.stats
                except Exception:
                    jb_buf = 0
                    jb_stats = None
                collision_uuids = identity_collisions.get(int(client_id), ())
                out[client_id] = {
                    "client_id": client_id,
                    "name": st.name,
                    "client_uuid": st.client_uuid,
                    "input_gain_db": float(st.input_gain_db),
                    "addr": st.addr,
                    "age_s": max(0.0, now - st.last_packet_monotonic),
                    "vu_dbfs": st.vu_dbfs,
                    "last_timestamp_ms": st.last_timestamp_ms,
                    "last_sequence_number": st.last_sequence_number,
                    "jb_buf": int(jb_buf),
                    "jb_missing": 0 if jb_stats is None else int(jb_stats.missing),
                    "jb_late_dropped": 0 if jb_stats is None else int(jb_stats.late_dropped),
                    "jb_concealed": 0 if jb_stats is None else int(jb_stats.concealed),
                    "control_connected": bool(st.control_connected),
                    "control_age_s": max(0.0, now - st.last_control_monotonic) if st.control_connected else None,
                    "ptt_buses": dict(st.ptt_buses),
                    "listen_return_bus": bool(st.listen_return_bus),
                    "listen_regie": bool(st.listen_regie),
                    "identity_collision": bool(collision_uuids),
                    "identity_collision_uuids": list(collision_uuids),
                }
            return out

    def get_stats_snapshot(self) -> dict:
        with self._lock:
            clients = len(self._clients)
            outputs = len(self._outputs)
            running = bool(self._running)
            identity_collision_count = len(self._identity_collisions)
            preset_loaded_source_version = self._preset_loaded_source_version
            stale_mix_epochs_dropped = int(self._stale_mix_epochs_dropped)
            control_connected = 0
            waiting_udp = 0
            for st in self._clients.values():
                if bool(st.control_connected):
                    control_connected += 1
                    try:
                        if int(st.addr[1]) <= 0:
                            waiting_udp += 1
                    except Exception:
                        waiting_udp += 1
            try:
                underflows_total = int(sum(int(o.underflows) for o in self._outputs.values()))
            except Exception:
                underflows_total = 0
            udp_rx_age_s = (
                max(0.0, time.monotonic() - float(self._last_valid_udp_monotonic))
                if float(self._last_valid_udp_monotonic) > 0.0
                else None
            )
        try:
            mix_q = int(self._mix_queue.qsize())
        except Exception:
            mix_q = 0
        with self._worker_lock:
            worker_errors = dict(self._worker_errors)
            worker_alive = {
                str(name): bool(thread.is_alive())
                for name, thread in self._worker_threads.items()
            }
        with self._return_lock:
            return_stream_active = self._stream_is_active(self._return_stream)
            return_stream_error = str(self._return_stream_error or "")
            if (
                self._return_enabled
                and self._running
                and not self._stop.is_set()
                and not return_stream_active
                and not return_stream_error
            ):
                return_stream_error = "return input stream is inactive"
            return_ring_stats = self._return_frames.stats
        return {
            "running": bool(running),
            "clients": int(clients),
            "control_connected_clients": int(control_connected),
            "clients_waiting_udp": int(waiting_udp),
            "outputs": int(outputs),
            "mix_q": int(mix_q),
            "underflows": int(underflows_total),
            "return_enabled": bool(self._return_enabled),
            "return_stream_active": bool(return_stream_active),
            "return_stream_error": return_stream_error,
            "return_vu_dbfs": float(self._return_vu_dbfs),
            "regie_vu_dbfs": float(self._regie_vu_dbfs),
            "return_q": int(return_ring_stats.depth) if self._return_enabled else 0,
            "return_queue_capacity": int(self._return_frames.capacity),
            "return_queue_pushed": int(return_ring_stats.pushed),
            "return_queue_popped": int(return_ring_stats.popped),
            "return_queue_dropped": int(return_ring_stats.dropped),
            "return_queue_underflows": int(return_ring_stats.underflows),
            "rx_datagrams": int(self._rx_datagrams),
            "rx_bytes": int(self._rx_bytes),
            "rx_socket_errors": int(self._rx_socket_errors),
            "rx_packets": int(self._rx_packets),
            "tx_packets": int(self._tx_packets),
            "rx_decode_errors": int(self._rx_decode_errors),
            "rx_invalid_frame_sizes": int(self._rx_invalid_frame_sizes),
            "rx_unknown_clients": int(self._rx_unknown_clients),
            "rx_inactive_clients": int(self._rx_inactive_clients),
            "rx_wrong_source": int(self._rx_wrong_source),
            "rx_invalid_packets": int(self._rx_invalid_packets),
            "rx_invalid_tokens": int(self._rx_invalid_tokens),
            "rx_invalid_directions": int(self._rx_invalid_directions),
            "rx_invalid_route_masks": int(self._rx_invalid_route_masks),
            "udp_rx_age_s": udp_rx_age_s,
            "tx_encode_errors": int(self._tx_encode_errors),
            "tx_socket_errors": int(self._tx_socket_errors),
            "mix_queue_drops": int(self._mix_queue_drops),
            "stale_mix_epochs_dropped": int(stale_mix_epochs_dropped),
            "worker_alive": worker_alive,
            "worker_errors": worker_errors,
            "control_rejected_sessions": int(self._ctrl_rejected_sessions),
            "control_hello_timeouts": int(self._ctrl_hello_timeouts),
            "control_invalid_identities": int(self._ctrl_invalid_identities),
            "control_identity_collisions": int(self._ctrl_identity_collisions),
            "control_codec_init_errors": int(self._ctrl_codec_init_errors),
            "identity_collision_count": int(identity_collision_count),
            "preset_schema_version": int(PRESET_SCHEMA_VERSION),
            "preset_loaded_source_version": preset_loaded_source_version,
            "opus_ok": bool(self._opus_ok),
            "opus_err": str(self._opus_err),
            "opuslib_version": str(self._opuslib_version),
        }

    def forget_client(self, client_id: int) -> None:
        cid = int(client_id)

        # Detach control ownership and live audio state as one transaction.
        # Otherwise a reconnect between the two removals can leave a control
        # socket owning no ClientState, while queued audio from the forgotten
        # session remains eligible for broadcast.
        with self._ctrl_lock:
            sock = self._ctrl_sessions.pop(int(cid), None)
            send_lock = self._ctrl_send_locks.pop(int(cid), None)
            with self._lock:
                st = self._clients.pop(int(cid), None)
                u = str(st.client_uuid or "") if st is not None else ""
                if st is not None:
                    self._expire_client_session(st)
                if not u:
                    for client_uuid in self._client_profiles:
                        if int(client_id_from_uuid(client_uuid)) == cid:
                            u = str(client_uuid)
                            break
                if u:
                    self._client_profiles.pop(u, None)
                    self._refresh_identity_collisions_locked()

        if sock is not None:
            try:
                if send_lock is None:
                    self._control_send(sock, {"type": "kick", "message": "Tu as été kick"})
                else:
                    with send_lock:
                        self._control_send(sock, {"type": "kick", "message": "Tu as été kick"})
            except Exception as e:
                logger.debug("kick send failed to {}: {}", int(cid), e)
            try:
                sock.close()
            except Exception as e:
                logger.debug("kick socket close failed for {}: {}", int(cid), e)

        self._autosave_preset()

    def set_output_gain(self, output_id: int, gain_db: float) -> None:
        next_gain = max(-60.0, min(float(MAX_GAIN_DB), float(gain_db)))
        changed = False
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            if abs(float(out.gain_db) - float(next_gain)) >= 0.01:
                out.gain_db = float(next_gain)
                changed = True

        if not changed:
            return

        self._autosave_preset()

    def _stream_is_active(self, stream: Optional[sd.OutputStream]) -> bool:
        if stream is None:
            return False
        try:
            return bool(stream.active)
        except Exception:
            return False

    def _output_stream_needs_reopen(self, out: OutputState, *, now: Optional[float] = None) -> bool:
        with out.lock:
            stream = out.stream
            last_callback = float(out.last_callback_monotonic)
        if stream is None or not self._stream_is_active(stream):
            return True
        if now is None:
            now = time.monotonic()
        return last_callback <= 0.0 or (float(now) - float(last_callback)) > 1.0

    @staticmethod
    def _close_stream(stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _close_output_stream(self, out: OutputState) -> None:
        with out.lock:
            stream = out.stream
            out.stream = None
            out.last_callback_monotonic = 0.0
            out.vu_dbfs = -60.0
        self._close_stream(stream)
        out.ring.clear()

    def _replace_output_state(self, out: OutputState, *, device: int) -> OutputState:
        candidate = OutputState(
            output_id=int(out.output_id),
            device=int(device),
            bus_id=int(out.bus_id),
            gain_db=float(out.gain_db),
        )
        self._open_output_stream(candidate)

        swapped = False
        try:
            with self._lock:
                if self._outputs.get(int(out.output_id)) is not out:
                    raise RuntimeError("output changed while opening replacement device")
                if self._stop.is_set() or not self._running:
                    raise RuntimeError("output replacement cancelled during shutdown")
                with out.lock:
                    latest_bus_id = int(out.bus_id)
                    latest_gain_db = float(out.gain_db)
                with candidate.lock:
                    candidate.bus_id = latest_bus_id
                    candidate.gain_db = latest_gain_db
                self._outputs[int(out.output_id)] = candidate
                swapped = True
        finally:
            if not swapped:
                self._close_output_stream(candidate)

        # The old stream remains fully operational until after the atomic swap.
        self._close_output_stream(out)
        return candidate

    def _restart_output_stream(self, out: OutputState) -> OutputState:
        try:
            return self._replace_output_state(out, device=int(out.device))
        except Exception as exc:
            with out.lock:
                out.stream_error = str(exc)
            raise

    def _ensure_output_retry_thread(self) -> None:
        with self._worker_lock:
            t = self._retry_thread
            if t is not None and t.is_alive():
                return
            t = threading.Thread(
                target=self._worker_entry,
                args=("output-retry", self._retry_output_streams),
                kwargs={"critical": False},
                name="output-retry",
                daemon=True,
            )
            self._retry_thread = t
            self._worker_threads["output-retry"] = t
        t.start()

    def reopen_outputs(self, *, force: bool = False) -> None:
        if not self._running or self._stop.is_set():
            return
        with self._lock:
            outputs = list(self._outputs.values())

        for out in outputs:
            if not force and not self._output_stream_needs_reopen(out):
                continue
            try:
                self._restart_output_stream(out)
            except Exception as e:
                logger.warning("output {} reopen failed: {}", int(out.output_id), e)
        if outputs:
            self._ensure_output_retry_thread()

    def reopen_return_input(self) -> None:
        if not bool(self._running) or self._stop.is_set():
            return
        if not bool(self._return_enabled) or self._return_input_device is None:
            self._close_return_input_stream()
            return
        try:
            self._open_return_input_stream(int(self._return_input_device))
            logger.debug("reopen_return_input: stream reopened on device {}", self._return_input_device)
        except Exception as e:
            logger.warning("reopen_return_input failed: {}", e)

    def set_bus_feed_to_regie(self, bus_id: int, enabled: bool) -> None:
        bid = int(bus_id)
        if bid == REGIE_BUS_ID or bid not in FIXED_BUS_IDS:
            return
        changed = False
        with self._lock:
            bus = self._buses.get(bid)
            if bus is not None and bool(bus.feed_to_regie) != bool(enabled):
                bus.feed_to_regie = bool(enabled)
                changed = True
                
        if not changed:
            return
            
        self._control_push_all_configs()
        self._autosave_preset()

    def set_return_enabled(self, enabled: bool) -> None:
        changed = False
        with self._lock:
            if self._return_enabled != bool(enabled):
                self._return_enabled = bool(enabled)
                changed = True
        
        if not changed:
            return

        # Remise à zéro du VU mètre si désactivé
        if not bool(enabled):
            with self._return_lock:
                self._return_vu_dbfs = -60.0

        # Si le serveur tourne, on rouvre (ou ferme) le stream
        if self._running:
            self.reopen_return_input()

        self._autosave_preset()

    def set_return_input_device(self, device: Optional[int]) -> None:
        dev_val = int(device) if device is not None else None
        with self._lock:
            old_device = self._return_input_device
        if old_device == dev_val:
            return

        candidate_stream = None
        candidate_gate = None
        if self._running and not self._stop.is_set() and self._return_enabled and dev_val is not None:
            # Candidate starts gated and cannot enter the Return ring before the
            # device selection and stream are committed together.
            candidate_stream, candidate_gate = self._create_return_input_stream(dev_val)

        candidate_adopted = False
        selection_committed = False
        try:
            with self._lock:
                if self._return_input_device != old_device:
                    raise RuntimeError("return input changed while opening replacement device")
                wants_live_stream = (
                    self._running
                    and not self._stop.is_set()
                    and self._return_enabled
                    and dev_val is not None
                )
                if (
                    wants_live_stream
                    and candidate_stream is not None
                    and candidate_gate is not None
                ):
                    self._commit_return_input_stream(candidate_stream, candidate_gate)
                    candidate_adopted = True
                self._return_input_device = dev_val
                selection_committed = True
        finally:
            if not candidate_adopted and candidate_stream is not None:
                if candidate_gate is not None:
                    candidate_gate.clear()
                self._close_stream(candidate_stream)

        if (
            selection_committed
            and self._running
            and self._return_enabled
            and dev_val is None
        ):
            self._close_return_input_stream()

        self._autosave_preset()

    def set_bus_gain(self, bus_id: int, gain_db: float) -> None:
        bid = int(bus_id)
        if bid not in FIXED_BUS_IDS:
            return
        next_gain = max(-60.0, min(float(MAX_GAIN_DB), float(gain_db)))
        changed = False
        with self._lock:
            bus = self._buses.get(int(bid))
            if bus is None:
                return
            if abs(float(bus.gain_db) - float(next_gain)) >= 0.01:
                bus.gain_db = float(next_gain)
                changed = True

        if not changed:
            return

        self._control_push_all_configs()
        self._autosave_preset()

    def set_client_input_gain(self, client_id: int, gain_db: float) -> None:
        cid = int(client_id)
        next_gain = max(-60.0, min(float(MAX_GAIN_DB), float(gain_db)))
        changed = False
        with self._lock:
            st = self._clients.get(int(cid))
            if st is None:
                return
            if abs(float(st.input_gain_db) - float(next_gain)) >= 0.01:
                st.input_gain_db = float(next_gain)
                u = str(st.client_uuid or "")
                if u:
                    profile = self._client_profiles.get(u)
                    if profile is None:
                        profile = ClientProfile(client_uuid=u, name=str(st.name or ""))
                        self._client_profiles[u] = profile
                    profile.input_gain_db = float(next_gain)
                    if st.name:
                        profile.name = str(st.name)
                changed = True

        if not changed:
            return

        self._control_push_config(int(cid))
        self._autosave_preset()

    @staticmethod
    def _control_is_live(
        control_connected: bool,
        last_control_monotonic: float,
        *,
        now: Optional[float] = None,
    ) -> bool:
        if not bool(control_connected):
            return False
        current = time.monotonic() if now is None else float(now)
        try:
            last = float(last_control_monotonic)
        except (TypeError, ValueError):
            return False
        return bool(last > 0.0 and 0.0 <= current - last <= float(CTRL_LIVENESS_TIMEOUT_S))

    @staticmethod
    def _new_session_token() -> int:
        token = 0
        while token == 0:
            token = int(secrets.randbits(64)) & 0xFFFFFFFFFFFFFFFF
        return token

    def _advance_routing_epoch_locked(self) -> int:
        # Callers hold self._lock. Broadcast never acquires self._lock while it
        # owns this gate, so the state -> routing-I/O lock order cannot invert.
        with self._routing_io_lock:
            self._routing_epoch = (int(self._routing_epoch) + 1) & 0x7FFFFFFFFFFFFFFF
            if self._routing_epoch == 0:
                self._routing_epoch = 1

            # Physical outputs have their own bounded queues. Clear them
            # together with network mixes so audio prepared under a revoked
            # session cannot leak for the remaining buffered duration.
            for output in self._outputs.values():
                output.ring.clear()
            while True:
                try:
                    self._mix_queue.get_nowait()
                    self._stale_mix_epochs_dropped += 1
                except queue.Empty:
                    break
            return int(self._routing_epoch)

    def _expire_client_session(self, st: ClientState) -> None:
        st.control_connected = False
        st.session_token = 0
        st.ptt_buses = {}
        st.jb.reset()
        self._advance_routing_epoch_locked()

    def _count_invalid_audio_packet(self, reason: str) -> None:
        if reason == "invalid_token":
            self._rx_invalid_tokens += 1
        elif reason == "invalid_direction":
            self._rx_invalid_directions += 1
        elif reason == "invalid_route":
            self._rx_invalid_route_masks += 1
        else:
            self._rx_invalid_packets += 1

    @classmethod
    def _live_frame_bus_mask(
        cls,
        frame_bus_mask: int,
        control_connected: bool,
        last_control_monotonic: float,
        *,
        now: Optional[float] = None,
    ) -> int:
        # Routing is carried atomically by this audio frame. TCP liveness is a
        # second fail-closed gate, never the source of the route itself.
        if not cls._control_is_live(control_connected, last_control_monotonic, now=now):
            return 0
        try:
            mask = int(frame_bus_mask)
        except (TypeError, ValueError):
            return 0
        if mask < 0 or mask & ~int(_FIXED_BUS_MASK):
            return 0
        return int(mask)

    def _normalize_decoded_frame(self, frame: object) -> Optional[np.ndarray]:
        try:
            normalized = np.asarray(frame, dtype=np.float32)
        except Exception:
            self._rx_invalid_frame_sizes += 1
            return None
        if normalized.ndim != 1 or int(normalized.shape[0]) != int(FRAME_SAMPLES):
            self._rx_invalid_frame_sizes += 1
            return None
        if not bool(np.isfinite(normalized).all()):
            normalized = np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=-1.0)
        return normalized.astype(np.float32, copy=False)

    def _enqueue_latest_mix(self, item: object) -> None:
        try:
            self._mix_queue.put_nowait(item)
            return
        except queue.Full:
            pass

        # Realtime policy: discard stale audio, never the newest frame.
        try:
            self._mix_queue.get_nowait()
            self._mix_queue_drops += 1
        except queue.Empty:
            pass
        try:
            self._mix_queue.put_nowait(item)
        except queue.Full:
            # A consumer/producer race is harmless; account for the dropped
            # newest item if the queue filled again before this retry.
            self._mix_queue_drops += 1

    def _worker_entry(self, name: str, target: Callable[[], None], *, critical: bool) -> None:
        error: Optional[str] = None
        try:
            target()
            if bool(critical) and not self._stop.is_set():
                error = "worker exited unexpectedly"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("{} worker crashed", name)

        if error is not None:
            with self._worker_lock:
                self._worker_errors[str(name)] = str(error)
            if bool(critical):
                self._running = False
                self._stop.set()
                logger.error("critical worker {} stopped: {}", name, error)

    def _new_worker(self, name: str, target: Callable[[], None], *, critical: bool = True) -> threading.Thread:
        thread = threading.Thread(
            target=self._worker_entry,
            args=(str(name), target),
            kwargs={"critical": bool(critical)},
            name=str(name),
            daemon=True,
        )
        with self._worker_lock:
            self._worker_threads[str(name)] = thread
        return thread

    @staticmethod
    def _join_threads(threads: list[threading.Thread], timeout_s: float = 3.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        current = threading.current_thread()
        for thread in threads:
            if thread is current or not thread.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            thread.join(timeout=remaining)

    def start(self) -> None:
        logger.info("server listening on {}:{}", self.bind_ip, self.port)

        if self._running:
            return
        if self._stop.is_set():
            raise RuntimeError("server instances cannot be restarted after stop")
        if not bool(self._opus_ok):
            raise RuntimeError(f"Opus unavailable: {self._opus_err or 'codec self-test failed'}")

        self._running = True

        try:
            self._open_control_socket()

            with self._worker_lock:
                self._worker_errors.clear()
            self._rx_thread = self._new_worker("udp-rx", self._rx_loop)
            self._tx_thread = self._new_worker("udp-tx", self._broadcast_loop)
            self._mix_thread = self._new_worker("mix", self._mix_loop)
            self._ctrl_thread = self._new_worker("ctrl-accept", self._ctrl_accept_loop)

            self._rx_thread.start()
            self._tx_thread.start()
            self._mix_thread.start()
            self._ctrl_thread.start()

            if self._discovery_enabled:
                self._beacon = DiscoveryBeacon(
                    server_name=self._server_name,
                    audio_port=self.port,
                    bind_ip=self.bind_ip,
                )
                self._beacon.start()

            with self._lock:
                outputs = list(self._outputs.values())

            ok = 0
            last_err: Optional[Exception] = None
            for out in outputs:
                try:
                    self._open_output_stream(out)
                    ok += 1
                except Exception as e:
                    last_err = e
                    logger.warning("output {} failed to start: {}", int(out.output_id), e)

            if ok == 0 and last_err is not None:
                raise last_err

            if outputs:
                self._ensure_output_retry_thread()

            try:
                logger.info("start: return_enabled={} return_input_device={}", bool(self._return_enabled), self._return_input_device)
                if self._return_enabled and self._return_input_device is not None:
                    self._open_return_input_stream(int(self._return_input_device))
                    logger.info("start: return input stream opened on device {}", self._return_input_device)
                else:
                    logger.info("start: return input stream NOT opened (enabled={} device={})", bool(self._return_enabled), self._return_input_device)
            except Exception as e:
                logger.error("return input failed to start: {}", e)
        except Exception:
            try:
                self.stop()
            except Exception:
                pass
            raise

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._beacon is not None:
            self._beacon.stop()
            self._beacon = None
        try:
            try:
                if self._ctrl_sock is not None:
                    self._ctrl_sock.close()
            except Exception:
                pass
            self._ctrl_sock = None

            try:
                self._sock.close()
            except Exception:
                pass

            with self._ctrl_lock:
                sessions = list(self._ctrl_sessions.items())
                self._ctrl_sessions.clear()
                self._ctrl_send_locks.clear()
            for _cid, s in sessions:
                try:
                    s.close()
                except Exception:
                    pass

            with self._lock:
                outs = list(self._outputs.values())
                for st in self._clients.values():
                    self._expire_client_session(st)

            for out in outs:
                self._close_output_stream(out)

            self._close_return_input_stream()

            with self._worker_lock:
                workers = list(self._worker_threads.values())
                control_sessions = list(self._ctrl_session_threads)
            workers.extend(control_sessions)
            self._join_threads(workers)
        finally:
            with self._worker_lock:
                self._ctrl_session_threads.clear()
            try:
                self._preset_writer.close(raise_on_error=True)
            except Exception as exc:
                logger.error("server preset flush failed during stop: {}", exc)

    def _retry_output_streams(self) -> None:
        retries = 3
        delay_s = 1.0
        try:
            for attempt in range(retries):
                if self._stop.is_set():
                    return
                if self._stop.wait(delay_s):
                    return
                with self._lock:
                    outputs = list(self._outputs.values())
                now = time.monotonic()
                pending = [out for out in outputs if self._output_stream_needs_reopen(out, now=now)]
                if not pending:
                    return
                for out in pending:
                    try:
                        self._restart_output_stream(out)
                    except Exception as e:
                        logger.warning(
                            "output {} retry {}/{} failed: {}",
                            int(out.output_id),
                            attempt + 1,
                            retries,
                            e,
                        )
        finally:
            with self._worker_lock:
                if self._retry_thread is threading.current_thread():
                    self._retry_thread = None

    def _close_return_input_stream(self) -> None:
        with self._return_lock:
            stream = self._return_stream
            gate = self._return_stream_gate
            if gate is not None:
                gate.clear()
            self._return_stream = None
            self._return_stream_gate = None
            self._return_stream_error = ""
        self._close_stream(stream)
        with self._return_lock:
            self._return_vu_dbfs = -60.0
            self._return_input_scratch.fill(0.0)
            self._return_frames.clear()

    def _create_return_input_stream(self, device: int) -> tuple[sd.InputStream, threading.Event]:
        in_sr = int(SAMPLE_RATE)
        blocksize_frame = int(FRAME_SAMPLES)
        gate = threading.Event()

        # Pre-check the device against the requested format. This surfaces a
        # clear error before allocating the stream and gives a friendly message.
        dev_info = None
        try:
            dev_info = sd.query_devices(int(device))
        except Exception:
            dev_info = None
        extra_settings = shared_stream_extra_settings(dev_info)
        try:
            sd.check_input_settings(
                device=int(device),
                channels=1,
                dtype="float32",
                samplerate=int(in_sr),
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=int(device),
                sr=int(in_sr),
                ch=1,
            )
            raise RuntimeError(msg) from e

        st: Optional[sd.InputStream] = None
        try:
            st = sd.InputStream(
                samplerate=float(in_sr),
                channels=1,
                dtype="float32",
                blocksize=blocksize_frame,
                device=int(device),
                latency="low",
                extra_settings=extra_settings,
                callback=lambda indata, frames, time_info, status: self._return_in_callback(
                    indata,
                    frames,
                    time_info,
                    status,
                    active_gate=gate,
                ),
            )
            st.start()
        except Exception as e:
            try:
                if st is not None:
                    st.stop()
                    st.close()
            except Exception:
                pass
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=int(device),
                sr=int(in_sr),
                ch=1,
            )
            raise RuntimeError(msg) from e
        return st, gate

    def _commit_return_input_stream(
        self,
        stream: sd.InputStream,
        gate: threading.Event,
    ) -> None:
        with self._return_lock:
            # stop() sets its event before detaching streams. If it wins this
            # lock, a late candidate must never be committed after shutdown;
            # if this commit wins, stop() will subsequently close the new one.
            if self._stop.is_set() or not self._running or not self._return_enabled:
                raise RuntimeError("return input stream commit cancelled")
            old_stream = self._return_stream
            old_gate = self._return_stream_gate
            if old_gate is not None:
                old_gate.clear()
            self._return_stream = stream
            self._return_stream_gate = gate
            self._return_stream_error = ""
            self._return_in_samplerate = int(SAMPLE_RATE)
            self._return_vu_dbfs = -60.0
            self._return_input_scratch.fill(0.0)
            self._return_frames.clear()
            gate.set()
        self._close_stream(old_stream)

    def _open_return_input_stream(self, device: int) -> None:
        stream = None
        gate = None
        try:
            stream, gate = self._create_return_input_stream(int(device))
            self._commit_return_input_stream(stream, gate)
        except Exception as exc:
            if gate is not None:
                gate.clear()
            if stream is not None:
                with self._return_lock:
                    is_current = self._return_stream is stream
                if not is_current:
                    self._close_stream(stream)
            with self._return_lock:
                if (
                    self._return_stream is None
                    and self._running
                    and not self._stop.is_set()
                    and self._return_enabled
                ):
                    self._return_stream_error = str(exc)
            raise

    def _return_in_callback(
        self,
        indata,
        frames,
        time_info,
        status,
        *,
        active_gate: Optional[threading.Event] = None,
    ) -> None:
        if active_gate is not None and not active_gate.is_set():
            return
        if status:
            logger.debug("return in status: {}", status)

        if self._stop.is_set() or not bool(self._return_enabled):
            return

        if int(frames) != int(FRAME_SAMPLES):
            logger.debug("return in frame mismatch: got={} expected={}", int(frames), int(FRAME_SAMPLES))
            return

        with self._return_lock:
            # A previous stream may have entered the callback just before its
            # gate was cleared during a device swap. Re-check after acquiring
            # the shared scratch lock so it cannot contaminate the new stream's
            # ring after the atomic commit.
            if active_gate is not None and not active_gate.is_set():
                return
            scratch = self._return_input_scratch
            try:
                if indata.ndim == 1:
                    np.copyto(scratch, indata, casting="unsafe")
                elif indata.shape[1] == 1:
                    np.copyto(scratch, indata[:, 0], casting="unsafe")
                else:
                    np.mean(indata, axis=1, dtype=np.float32, out=scratch)
            except Exception:
                scratch.fill(0.0)
                self._return_vu_dbfs = -60.0
                return

            peak = max(float(np.max(scratch)), -float(np.min(scratch)))
            if peak > 1.0:
                scratch *= 0.99 / peak
            np.clip(scratch, -1.0, 1.0, out=scratch)
            try:
                self._return_vu_dbfs = float(rms_dbfs(scratch))
            except Exception:
                self._return_vu_dbfs = -60.0
            self._return_frames.push(scratch)

    def _control_send(self, sock: socket.socket, msg: dict) -> None:
        sock.sendall(encode_control_message(msg))

    def _control_send_to_client(self, client_id: int, sock: socket.socket, msg: dict) -> None:
        with self._ctrl_lock:
            send_lock = self._ctrl_send_locks.get(int(client_id))
        if send_lock is None:
            self._control_send(sock, msg)
            return
        with send_lock:
            self._control_send(sock, msg)

    def _control_push_all_configs(self) -> None:
        with self._lock:
            client_ids = [int(cid) for cid in self._clients.keys()]
        for cid in client_ids:
            self._control_push_config(int(cid))

    def _control_push_config(self, client_id: int) -> None:
        with self._lock:
            st = self._clients.get(int(client_id))
            if st is None:
                return
            cfg = {
                "client_id": int(client_id),
                "input_gain_db": float(st.input_gain_db),
                "return_vu_dbfs": float(self._return_vu_dbfs),
                "buses": [
                    {
                        "bus_id": int(definition.bus_id),
                        "name": str(definition.name),
                        "gain_db": float(bus.gain_db),
                        "feed_to_regie": bool(bus.feed_to_regie),
                    }
                    for definition in FIXED_BUSES
                    if (bus := self._buses.get(definition.bus_id)) is not None
                ],
            }

        with self._ctrl_lock:
            sock = self._ctrl_sessions.get(int(client_id))
        if sock is None:
            return
        try:
            self._control_send_to_client(int(client_id), sock, {"type": "update", "config": cfg})
        except Exception as e:
            logger.debug("control push config failed for {}: {}", int(client_id), e)
            try:
                sock.close()
            except Exception:
                pass
            with self._ctrl_lock:
                if self._ctrl_sessions.get(int(client_id)) is sock:
                    self._ctrl_sessions.pop(int(client_id), None)
                    self._ctrl_send_locks.pop(int(client_id), None)
                    with self._lock:
                        st2 = self._clients.get(int(client_id))
                        if st2 is not None:
                            self._expire_client_session(st2)

    def _open_control_socket(self) -> socket.socket:
        port = int(self.port) + int(CONTROL_PORT_OFFSET)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.bind_ip, port))
            s.listen(min(32, int(MAX_CONTROL_SESSIONS)))
            s.settimeout(0.5)
            self._ctrl_sock = s
            logger.info("control listening on {}:{}", self.bind_ip, port)
            return s
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            raise

    def _ctrl_accept_loop(self) -> None:
        s = self._ctrl_sock
        if s is None:
            try:
                s = self._open_control_socket()
            except Exception as e:
                logger.error("control socket failed to start: {}", e)
                self._stop.set()
                return

        while not self._stop.is_set():
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self._stop.is_set():
                try:
                    conn.close()
                except Exception:
                    pass
                return
            reject = False
            with self._worker_lock:
                self._ctrl_session_threads = {
                    thread for thread in self._ctrl_session_threads if thread.is_alive()
                }
                if len(self._ctrl_session_threads) >= int(MAX_CONTROL_SESSIONS):
                    self._ctrl_rejected_sessions += 1
                    reject = True
                else:
                    t = threading.Thread(
                        target=self._ctrl_session_entry,
                        args=(conn, addr),
                        name=f"ctrl-session-{addr[0]}:{addr[1]}",
                        daemon=True,
                    )
                    self._ctrl_session_threads.add(t)
            if reject:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            try:
                t.start()
            except Exception:
                with self._worker_lock:
                    self._ctrl_session_threads.discard(t)
                try:
                    conn.close()
                except Exception:
                    pass
                raise

    def _ctrl_session_entry(self, conn: socket.socket, addr) -> None:
        try:
            self._ctrl_session_loop(conn, addr)
        except Exception:
            logger.exception("control session crashed for {}", addr)
        finally:
            with self._worker_lock:
                self._ctrl_session_threads.discard(threading.current_thread())

    def _ctrl_session_loop(self, conn: socket.socket, addr) -> None:
        conn.settimeout(0.5)
        parser = JsonLineBuffer()
        client_id: Optional[int] = None
        session_token = 0
        hello_deadline = time.monotonic() + float(CONTROL_HELLO_TIMEOUT_S)

        try:
            while not self._stop.is_set():
                if client_id is None and time.monotonic() >= hello_deadline:
                    self._ctrl_hello_timeouts += 1
                    raise ControlProtocolError(
                        f"hello not received within {float(CONTROL_HELLO_TIMEOUT_S):.1f}s"
                    )
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                except socket.timeout:
                    continue
                except Exception:
                    break

                for msg in parser.feed(chunk):
                    mtype = str(msg.get("type") or "").lower()
                    if client_id is None and mtype != "hello":
                        raise ControlProtocolError(
                            f"expected hello as first control message, received {mtype or '<missing type>'}"
                        )
                    if client_id is not None and mtype == "hello":
                        raise ControlProtocolError("received duplicate hello")

                    if client_id is not None:
                        with self._ctrl_lock:
                            if self._ctrl_sessions.get(int(client_id)) is not conn:
                                # This socket was superseded by a newer hello for
                                # the same client. It no longer owns any state.
                                return
                        with self._lock:
                            owned_state = self._clients.get(int(client_id))
                            if (
                                owned_state is None
                                or int(session_token) == 0
                                or int(owned_state.session_token) != int(session_token)
                            ):
                                raise ControlProtocolError(
                                    "control session token expired; a new hello is required"
                                )
                    if mtype == "ping":
                        if client_id is not None:
                            now = time.monotonic()
                            with self._lock:
                                st = self._clients.get(int(client_id))
                                if (
                                    st is None
                                    or int(st.session_token) != int(session_token)
                                ):
                                    return
                                st.control_connected = True
                                st.last_control_monotonic = now
                        try:
                            with self._lock:
                                return_vu_dbfs = float(self._return_vu_dbfs)
                            pong_msg = {
                                "type": "pong",
                                "t": msg.get("t"),
                                "return_vu_dbfs": float(return_vu_dbfs),
                            }
                            if client_id is None:
                                self._control_send(conn, pong_msg)
                            else:
                                self._control_send_to_client(int(client_id), conn, pong_msg)
                        except Exception as e:
                            logger.debug("pong send failed: {}", e)
                        continue

                    if mtype == "state":
                        cid = int(client_id)
                        raw_ptt_buses = msg.get("ptt_buses")
                        parsed_ptt_buses: Optional[Dict[int, bool]] = None
                        if raw_ptt_buses is not None:
                            if not isinstance(raw_ptt_buses, dict):
                                raise ControlProtocolError("state ptt_buses must be an object")
                            parsed_ptt_buses = {}
                            for raw_bus_id, raw_active in raw_ptt_buses.items():
                                if isinstance(raw_bus_id, bool):
                                    raise ControlProtocolError("state bus id must be an integer")
                                try:
                                    bus_id = int(raw_bus_id)
                                except (TypeError, ValueError) as exc:
                                    raise ControlProtocolError(
                                        "state bus id must be an integer"
                                    ) from exc
                                if bus_id not in FIXED_BUS_IDS:
                                    raise ControlProtocolError("state contains an unknown bus id")
                                if not isinstance(raw_active, bool):
                                    raise ControlProtocolError(
                                        "state PTT values must be booleans"
                                    )
                                parsed_ptt_buses[int(bus_id)] = bool(raw_active)

                        parsed_flags: Dict[str, Optional[bool]] = {}
                        for field_name in (
                            "listen_return_bus",
                            "listen_regie",
                            "applies_input_gain_locally",
                        ):
                            raw_value = msg.get(field_name)
                            if raw_value is not None and not isinstance(raw_value, bool):
                                raise ControlProtocolError(
                                    f"state {field_name} must be a boolean"
                                )
                            parsed_flags[field_name] = raw_value

                        parsed_gains: Dict[str, Optional[float]] = {}
                        for field_name in ("return_gain_db", "input_gain_db"):
                            raw_value = msg.get(field_name)
                            if raw_value is None:
                                parsed_gains[field_name] = None
                                continue
                            if isinstance(raw_value, bool) or not isinstance(
                                raw_value, (int, float)
                            ):
                                raise ControlProtocolError(
                                    f"state {field_name} must be a finite number"
                                )
                            gain = float(raw_value)
                            if not bool(np.isfinite(gain)):
                                raise ControlProtocolError(
                                    f"state {field_name} must be a finite number"
                                )
                            parsed_gains[field_name] = max(
                                -60.0,
                                min(float(MAX_GAIN_DB), gain),
                            )

                        listen_return_bus = parsed_flags["listen_return_bus"]
                        listen_regie = parsed_flags["listen_regie"]
                        applies_input_gain_locally = parsed_flags[
                            "applies_input_gain_locally"
                        ]
                        return_gain_db = parsed_gains["return_gain_db"]
                        input_gain_db = parsed_gains["input_gain_db"]
                        changed = False
                        # Fires on every PTT toggle / slider move; keep at TRACE to avoid log spam.
                        logger.trace("state from cid={}: listen_return_bus={} listen_regie={}", cid, listen_return_bus, listen_regie)
                        with self._lock:
                            st = self._clients.get(int(cid))
                            if (
                                st is None
                                or int(st.session_token) != int(session_token)
                            ):
                                return
                            if st is not None:
                                if parsed_ptt_buses is not None:
                                    st.ptt_buses = dict(parsed_ptt_buses)
                                if listen_return_bus is not None:
                                    st.listen_return_bus = listen_return_bus
                                if listen_regie is not None:
                                    st.listen_regie = listen_regie
                                if return_gain_db is not None:
                                    st.return_gain_db = float(return_gain_db)
                                if input_gain_db is not None:
                                    next_input_gain = float(input_gain_db)
                                    if abs(float(st.input_gain_db) - float(next_input_gain)) >= 0.01:
                                        st.input_gain_db = float(next_input_gain)
                                        u = str(st.client_uuid or "")
                                        if u:
                                            profile = self._client_profiles.get(u)
                                            if profile is None:
                                                profile = ClientProfile(
                                                    client_uuid=u,
                                                    name=str(st.name or ""),
                                                )
                                                self._client_profiles[u] = profile
                                            profile.input_gain_db = float(next_input_gain)
                                            if st.name:
                                                profile.name = str(st.name)
                                        changed = True
                                if applies_input_gain_locally is not None:
                                    next_applies = applies_input_gain_locally
                                    if bool(st.applies_input_gain_locally) != bool(next_applies):
                                        st.applies_input_gain_locally = bool(next_applies)
                                        changed = True

                                st.control_connected = True
                                st.last_control_monotonic = time.monotonic()
                        if changed:
                            self._autosave_preset()
                        continue

                    if mtype != "hello":
                        continue

                    require_control_protocol_version(msg.get("version"))

                    try:
                        client_id, client_uuid = validate_client_identity(
                            msg.get("client_id"),
                            msg.get("client_uuid"),
                        )
                    except ClientIdentityError as exc:
                        self._ctrl_invalid_identities += 1
                        raise ControlProtocolError(f"invalid client identity: {exc}") from exc

                    raw_name = msg.get("name", "")
                    if not isinstance(raw_name, str):
                        raise ControlProtocolError("hello name must be a string")
                    name = raw_name.strip()
                    if len(name) > 128:
                        raise ControlProtocolError("hello name exceeds 128 characters")
                    raw_applies_input_gain_locally = msg.get(
                        "applies_input_gain_locally",
                        False,
                    )
                    if not isinstance(raw_applies_input_gain_locally, bool):
                        raise ControlProtocolError(
                            "hello applies_input_gain_locally must be a boolean"
                        )
                    applies_input_gain_locally = raw_applies_input_gain_locally
                    raw_udp_port = msg.get("udp_port")
                    if isinstance(raw_udp_port, bool) or not isinstance(raw_udp_port, int):
                        raise ControlProtocolError("hello udp_port must be an integer")
                    if not 1 <= raw_udp_port <= 65535:
                        raise ControlProtocolError("hello udp_port must be between 1 and 65535")
                    udp_port = int(raw_udp_port)

                    with self._lock:
                        identity_conflicts = self._identity_conflicts_locked(
                            int(client_id),
                            client_uuid,
                        )
                    if identity_conflicts:
                        self._ctrl_identity_collisions += 1
                        raise ControlProtocolError(
                            "client identity collides with existing UUID(s): "
                            + ", ".join(identity_conflicts)
                        )

                    # Construct a complete fresh codec epoch before revoking a
                    # live session. A transient allocation/DLL failure must not
                    # cut audio that is currently working or reuse predictive
                    # state from the previous session.
                    try:
                        next_decoder = OpusDecoder()
                        next_encoder = OpusEncoder()
                        next_jitter = OpusPacketJitterBuffer(
                            start_frames=JB_START_FRAMES,
                            max_frames=JB_MAX_FRAMES,
                        )
                    except Exception as exc:
                        self._ctrl_codec_init_errors += 1
                        raise ControlProtocolError(
                            f"failed to initialize a fresh audio session: {exc}"
                        ) from exc

                    session_token = self._new_session_token()

                    now = time.monotonic()
                    profile_changed = False
                    prev = None
                    # Control ownership and its audio epoch are one atomic
                    # commit. A second concurrent hello can therefore observe
                    # either the complete old session or the complete new one,
                    # never an owner socket paired with another token/codec.
                    # The project-wide lock order is control -> server state.
                    with self._ctrl_lock:
                        with self._lock:
                            if self._stop.is_set():
                                raise ControlProtocolError("server is stopping")

                            # Codec construction happens outside the locks, so
                            # revalidate identity immediately before commit.
                            identity_conflicts = self._identity_conflicts_locked(
                                int(client_id),
                                client_uuid,
                            )
                            if identity_conflicts:
                                self._ctrl_identity_collisions += 1
                                raise ControlProtocolError(
                                    "client identity collides with existing UUID(s): "
                                    + ", ".join(identity_conflicts)
                                )

                            profile = self._client_profiles.get(client_uuid) if client_uuid else None
                            if client_uuid and profile is None:
                                profile = ClientProfile(
                                    client_uuid=client_uuid,
                                    name=str(name or ""),
                                    input_gain_db=0.0,
                                )
                                self._client_profiles[client_uuid] = profile
                                self._refresh_identity_collisions_locked()
                                profile_changed = True
                            elif profile is not None and name and str(profile.name) != name:
                                profile.name = name
                                profile_changed = True

                            st = self._clients.get(int(client_id))
                            if st is None:
                                st = ClientState(
                                    addr=(str(addr[0]), 0),
                                    last_packet_monotonic=now,
                                    decoder=next_decoder,
                                    encoder=next_encoder,
                                    jb=next_jitter,
                                )
                                self._clients[int(client_id)] = st
                            else:
                                st.decoder = next_decoder
                                st.encoder = next_encoder
                                st.jb = next_jitter
                            st.last_timestamp_ms = 0
                            st.last_sequence_number = 0
                            st.name = str(name or (profile.name if profile is not None else ""))
                            st.client_uuid = client_uuid
                            st.input_gain_db = float(profile.input_gain_db) if profile is not None else 0.0
                            st.applies_input_gain_locally = bool(applies_input_gain_locally)
                            st.ptt_buses = {}
                            st.vu_dbfs = -60.0
                            st.addr = (str(addr[0]), int(udp_port) if udp_port is not None else 0)
                            st.control_connected = True
                            st.session_token = int(session_token)
                            st.last_control_monotonic = now
                            st.last_packet_monotonic = now
                            self._advance_routing_epoch_locked()

                            prev = self._ctrl_sessions.get(int(client_id))
                            self._ctrl_sessions[int(client_id)] = conn
                            self._ctrl_send_locks.setdefault(int(client_id), threading.Lock())

                    if prev is not None and prev is not conn:
                        try:
                            prev.close()
                        except Exception:
                            pass

                    if profile_changed:
                        self._autosave_preset()

                    self._control_send_to_client(
                        int(client_id),
                        conn,
                        {
                            "type": "welcome",
                            "version": int(CONTROL_PROTOCOL_VERSION),
                            "session_token": format_session_token(session_token),
                            "server_time_ms": int(time.time() * 1000),
                        },
                    )
                    self._control_push_config(int(client_id))

        except ControlProtocolError as e:
            if not self._stop.is_set():
                logger.warning("control protocol error from {}: {}", addr, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

            if client_id is not None:
                with self._ctrl_lock:
                    if self._ctrl_sessions.get(int(client_id)) is conn:
                        self._ctrl_sessions.pop(int(client_id), None)
                        self._ctrl_send_locks.pop(int(client_id), None)
                        # Keep removal and token revocation atomic with respect
                        # to a replacement hello. The token guard is a second
                        # line of defence for any stale finalizer.
                        with self._lock:
                            st = self._clients.get(int(client_id))
                            if (
                                st is not None
                                and int(session_token) != 0
                                and int(st.session_token) == int(session_token)
                            ):
                                self._expire_client_session(st)

    def create_output(self, device: int, bus_id: int) -> int:
        definition = require_bus(bus_id)
        with self._lock:
            out = OutputState(output_id=0, device=int(device), bus_id=int(definition.bus_id))
            oid = int(self._next_output_id)
            self._next_output_id += 1
            out.output_id = oid
            self._outputs[oid] = out

        if self._running and not self._stop.is_set():
            try:
                self._open_output_stream(out)
            except Exception:
                with self._lock:
                    self._outputs.pop(int(oid), None)
                raise
        self._autosave_preset()
        return int(oid)

    def remove_output(self, output_id: int) -> None:
        with self._lock:
            out = self._outputs.pop(int(output_id), None)

        if out is None:
            return
        self._close_output_stream(out)
        self._autosave_preset()

    def set_output_bus(self, output_id: int, bus_id: int) -> None:
        try:
            definition = require_bus(bus_id)
        except ValueError:
            return
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            out.bus_id = int(definition.bus_id)

        self._autosave_preset()

    def set_output_device(self, output_id: int, device: int) -> None:
        next_device = int(device)
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            if int(out.device) == next_device:
                return

        if self._running and not self._stop.is_set():
            # Candidate is fully preflighted, opened and started before the
            # live state is swapped. Any exception leaves `out` untouched.
            self._replace_output_state(out, device=next_device)
        else:
            with self._lock:
                if self._outputs.get(int(output_id)) is not out:
                    return
                out.device = next_device
                out.stream_error = ""

        self._autosave_preset()

    def _open_output_stream(self, out: OutputState) -> None:
        dev = None
        try:
            dev = sd.query_devices(int(out.device))
            logger.debug(
                "output device: idx={} name={} hostapi={} max_out={} default_sr={}",
                int(out.device),
                dev.get("name"),
                dev.get("hostapi"),
                dev.get("max_output_channels"),
                dev.get("default_samplerate"),
            )
        except Exception as e:
            logger.warning("failed to query output device {}: {}", int(out.device), e)

        sr = int(SAMPLE_RATE)
        blocksize = int(FRAME_SAMPLES)
        extra_settings = shared_stream_extra_settings(dev)

        try:
            sd.check_output_settings(
                device=int(out.device),
                channels=int(CHANNELS),
                dtype="float32",
                samplerate=int(sr),
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev,
                device=int(out.device),
                sr=int(sr),
                ch=int(CHANNELS),
            )
            with out.lock:
                out.stream = None
                out.stream_error = str(msg)
            raise RuntimeError(msg) from e

        stream: Optional[sd.OutputStream] = None
        try:
            with out.lock:
                out.samplerate = int(sr)
                out.ring.clear()
                out.callback_scratch.fill(0.0)
                out.last_callback_monotonic = 0.0

            stream = sd.OutputStream(
                samplerate=float(sr),
                channels=CHANNELS,
                dtype="float32",
                blocksize=blocksize,
                device=int(out.device),
                latency="low",
                extra_settings=extra_settings,
                callback=lambda outdata, frames, time_info, status, state=out: self._output_state_callback(
                    state, outdata, frames, time_info, status
                ),
            )
            stream.start()
            with out.lock:
                # stop() signals before it detaches output streams. Checking
                # under the same state lock prevents a stream from being
                # assigned just after shutdown already tried to close it.
                if self._stop.is_set():
                    raise RuntimeError("output stream opening cancelled during shutdown")
                out.stream = stream
                out.stream_error = ""
            logger.debug(
                "audio output stream started: out_id={} bus_id={} sr={} ch={} blocksize={} latency=low",
                int(out.output_id),
                int(out.bus_id),
                sr,
                CHANNELS,
                blocksize,
            )
        except Exception as e:
            try:
                if stream is not None:
                    stream.stop()
                    stream.close()
            except Exception:
                pass
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev,
                device=int(out.device),
                sr=int(sr),
                ch=int(CHANNELS),
            )
            with out.lock:
                out.stream = None
                out.stream_error = str(msg)
            raise RuntimeError(msg) from e

    def _mix_loop(self) -> None:
        tick_s = float(FRAME_SAMPLES) / float(SAMPLE_RATE)
        next_t = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(0.005, next_t - now))
                continue

            # Resync after a clock jump (system suspend/resume, GC pause, freeze).
            # Without this, the loop would burn CPU producing thousands of stale
            # frames trying to "catch up" several seconds of missed ticks.
            if now > next_t + 0.1:
                logger.debug("mix loop resync: skipping {:.0f} ms of missed ticks", (now - next_t) * 1000.0)
                next_t = now

            produced = 0
            while now >= next_t and produced < 5 and not self._stop.is_set():
                next_t += tick_s
                produced += 1

                with self._lock:
                    buses = {
                        bus_id: (float(bus.gain_db), bool(bus.feed_to_regie))
                        for bus_id, bus in self._buses.items()
                    }
                    clients_snapshot: list[
                        tuple[
                            int,
                            ClientState,
                            OpusPacketJitterBuffer,
                            OpusDecoder,
                            Dict[int, bool],
                            bool,
                            float,
                            float,
                            bool,
                            int,
                        ]
                    ] = []
                    for client_id, st in list(self._clients.items()):
                        try:
                            clients_snapshot.append(
                                (
                                    int(client_id),
                                    st,
                                    st.jb,
                                    st.decoder,
                                    dict(st.ptt_buses),
                                    bool(st.control_connected),
                                    float(st.last_control_monotonic),
                                    float(st.input_gain_db),
                                    bool(st.applies_input_gain_locally),
                                    int(st.session_token),
                                )
                            )
                        except Exception:
                            clients_snapshot.append(
                                (
                                    int(client_id),
                                    st,
                                    st.jb,
                                    st.decoder,
                                    {},
                                    False,
                                    0.0,
                                    0.0,
                                    False,
                                    0,
                                )
                            )
                    outputs = list(self._outputs.values())
                    routing_epoch = int(self._routing_epoch)

                routed_sources: list[RoutedSource] = []
                vu_updates: Dict[int, float] = {}

                for (
                    client_id,
                    st,
                    jitter_buffer,
                    decoder,
                    ptt_buses,
                    ctrl_ok,
                    last_ctrl,
                    input_gain_db,
                    applies_input_gain_locally,
                    session_token,
                ) in clients_snapshot:
                    try:
                        jitter_frame = jitter_buffer.pop_frame()
                    except Exception:
                        jitter_frame = None
                    if jitter_frame is None:
                        if bool(ctrl_ok) and not any(bool(v) for v in ptt_buses.values()):
                            vu_updates[int(client_id)] = -60.0
                        continue
                    payload = bytes(jitter_frame.payload)
                    try:
                        frame_bus_mask = int(jitter_frame.metadata.bus_mask)
                        if int(jitter_frame.metadata.session_token) != int(session_token):
                            frame_bus_mask = 0
                    except Exception:
                        frame_bus_mask = 0
                    frame_bus_mask = self._live_frame_bus_mask(
                        frame_bus_mask,
                        bool(ctrl_ok),
                        float(last_ctrl),
                        now=now,
                    )
                    try:
                        frame = decoder.decode(payload)
                    except Exception:
                        self._rx_decode_errors += 1
                        continue

                    frame = self._normalize_decoded_frame(frame)
                    if frame is None:
                        vu_updates[int(client_id)] = -60.0
                        continue

                    if not bool(applies_input_gain_locally):
                        try:
                            frame = apply_gain_db(frame, float(input_gain_db))
                        except Exception:
                            pass

                    vu = rms_dbfs(frame)
                    vu_updates[int(client_id)] = float(vu)

                    try:
                        routed_sources.append(
                            RoutedSource(
                                client_id=int(client_id),
                                frame=frame,
                                bus_mask=int(frame_bus_mask),
                            )
                        )
                    except (RoutingError, TypeError, ValueError):
                        self._rx_invalid_frame_sizes += 1
                        vu_updates[int(client_id)] = -60.0

                with self._lock:
                    if int(self._routing_epoch) != int(routing_epoch):
                        self._stale_mix_epochs_dropped += 1
                        continue
                    if vu_updates:
                        for cid, vu in vu_updates.items():
                            st2 = self._clients.get(int(cid))
                            if st2 is not None:
                                st2.vu_dbfs = float(vu)

                routing_config = RoutingConfig(
                    bus_gain_db={bus_id: values[0] for bus_id, values in buses.items()},
                    feed_to_regie={bus_id: values[1] for bus_id, values in buses.items()},
                )
                routing_result = route_sources(routed_sources, routing_config)
                regie_monitor_mix = build_destination_mix(
                    routing_result.regie_linear,
                    None,
                    listen_regie=True,
                )

                try:
                    regie_vu_dbfs = float(rms_dbfs(regie_monitor_mix))
                except Exception:
                    regie_vu_dbfs = -60.0

                with self._lock:
                    if int(self._routing_epoch) != int(routing_epoch):
                        self._stale_mix_epochs_dropped += 1
                        continue
                    self._regie_vu_dbfs = float(regie_vu_dbfs)
                    self._enqueue_latest_mix(
                        _RoutingEnvelope(
                            routing_epoch=int(routing_epoch),
                            result=routing_result,
                        )
                    )

                    for out in outputs:
                        linear_mix = routing_result.bus_frames.get(int(out.bus_id))
                        if linear_mix is None:
                            linear_mix = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
                        # A physical output is a destination: preserve the shared
                        # bus sum and limit only the frame copied into this output.
                        mix = limit_peak(np.array(linear_mix, dtype=np.float32, copy=True))
                        out.ring.push(mix.astype(np.float32, copy=False))


    def _output_callback(self, output_id: int, outdata, frames, time_info, status) -> None:
        try:
            out = self._outputs.get(int(output_id))
        except Exception:
            out = None

        if out is None:
            outdata.fill(0.0)
            return

        self._output_state_callback(out, outdata, frames, time_info, status)

    def _output_state_callback(self, out: OutputState, outdata, frames, time_info, status) -> None:
        if status:
            logger.debug("audio status: {}", status)

        now = time.monotonic()
        with out.lock:
            out.last_callback_monotonic = float(now)
            if int(out.samplerate) != int(SAMPLE_RATE) or int(frames) != int(FRAME_SAMPLES):
                out.underflows += 1
                out.vu_dbfs = -60.0
                outdata.fill(0.0)
                return

            scratch = out.callback_scratch
            if out.ring.pop_into(scratch) is None:
                out.underflows += 1
                out.vu_dbfs = -60.0
                outdata.fill(0.0)
                return

            np.clip(scratch, -1.0, 1.0, out=scratch)
            scratch *= db_to_linear(float(out.gain_db))
            np.clip(scratch, -1.0, 1.0, out=scratch)
            try:
                out.vu_dbfs = rms_dbfs(scratch)
            except Exception:
                out.vu_dbfs = -60.0

            if outdata.ndim == 1:
                np.copyto(outdata, scratch, casting="unsafe")
            else:
                outdata.fill(0.0)
                np.copyto(outdata[:, 0], scratch, casting="unsafe")

    def _accept_udp_packet(self, pkt, addr: Tuple[str, int], *, now: Optional[float] = None) -> bool:
        received_at = time.monotonic() if now is None else float(now)
        if int(getattr(pkt, "flags", 0)) != int(AudioPacketDirection.CLIENT_TO_SERVER):
            self._rx_invalid_directions += 1
            return False
        client_id = int(pkt.client_id) & 0xFFFFFFFF
        seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
        payload = bytes(getattr(pkt, "payload", b""))
        stream_id = int(getattr(pkt, "stream_id", -1))
        bus_mask = int(getattr(pkt, "bus_mask", -1))
        if stream_id != 0 or bus_mask < 0 or bus_mask & ~int(_FIXED_BUS_MASK):
            self._rx_invalid_route_masks += 1
            return False

        with self._lock:
            st = self._clients.get(int(client_id))
            if st is None:
                self._rx_unknown_clients += 1
                return False

            if not self._control_is_live(
                st.control_connected,
                st.last_control_monotonic,
                now=received_at,
            ):
                self._rx_inactive_clients += 1
                self._expire_client_session(st)
                return False

            session_token = int(getattr(pkt, "session_token", 0))
            if (
                session_token == 0
                or int(st.session_token) == 0
                or session_token != int(st.session_token)
            ):
                self._rx_invalid_tokens += 1
                return False

            try:
                expected_addr = (str(st.addr[0]), int(st.addr[1]))
                source_addr = (str(addr[0]), int(addr[1]))
            except (IndexError, TypeError, ValueError):
                self._rx_wrong_source += 1
                return False

            # The UDP endpoint is declared by the TCP hello. UDP is never
            # allowed to learn or overwrite it, otherwise a forged datagram can
            # hijack both the jitter stream and return audio.
            if expected_addr[1] <= 0 or source_addr != expected_addr:
                self._rx_wrong_source += 1
                return False

            try:
                if received_at - float(st.last_packet_monotonic) > 2.0:
                    st.jb.reset()
            except (TypeError, ValueError):
                st.jb.reset()

            st.last_packet_monotonic = received_at
            st.last_timestamp_ms = int(pkt.timestamp_ms)
            st.last_sequence_number = int(pkt.sequence_number)
            st.jb.push(
                seq,
                payload,
                JitterFrameMetadata(
                    session_token=session_token,
                    client_id=client_id,
                    timestamp_ms=int(pkt.timestamp_ms),
                    stream_id=stream_id,
                    bus_mask=bus_mask,
                    flags=int(pkt.flags),
                ),
            )
            self._rx_packets += 1
            self._last_valid_udp_monotonic = received_at
            return True

    def _active_broadcast_destinations(self, *, now: Optional[float] = None) -> list[tuple]:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            destinations = []
            for client_id, st in self._clients.items():
                if not self._control_is_live(
                    st.control_connected,
                    st.last_control_monotonic,
                    now=current,
                ):
                    if st.control_connected:
                        self._expire_client_session(st)
                    continue
                if int(st.session_token) == 0:
                    continue
                destinations.append(
                    (
                        client_id,
                        st.addr,
                        st.encoder,
                        bool(st.listen_return_bus),
                        bool(st.listen_regie),
                        float(st.return_gain_db),
                        int(st.session_token),
                    )
                )
            return destinations

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                self._rx_socket_errors += 1
                # WinError 10054 (WSAECONNRESET): Windows reports ICMP
                # "port unreachable" when a previous sendto() targeted a
                # closed client port.  This is harmless — just retry
                # immediately so we don't block reception from other clients.
                err_code = getattr(e, 'winerror', None) or getattr(e, 'errno', None)
                if err_code == 10054:
                    continue
                logger.debug("_rx_loop OSError: {} sock_err_count={}", e, self._rx_socket_errors)
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue

            self._rx_datagrams += 1
            try:
                self._rx_bytes += int(len(data))
            except Exception:
                pass

            try:
                pkt = unpack_audio_packet_v2(data)
            except AudioPacketValidationError as e:
                self._count_invalid_audio_packet(e.reason)
                logger.warning("bad packet from {}: {}", addr, e)
                continue
            except Exception as e:
                self._rx_invalid_packets += 1
                logger.warning("bad packet from {}: {}", addr, e)
                continue

            try:
                self._accept_udp_packet(pkt, addr)
            except Exception as e:
                self._rx_decode_errors += 1
                logger.debug("UDP packet rejected after parsing from {}: {}", addr, e)

            now2 = time.monotonic()
            if now2 - self._last_stats_log >= 5.0:
                self._last_stats_log = now2
                with self._lock:
                    clients = len(self._clients)
                    mix_q = self._mix_queue.qsize()
                logger.debug(
                    "stats: clients={} rx_pkts={} tx_pkts={} rx_dec_err={} rx_unknown={} rx_inactive={} "
                    "rx_wrong_source={} rx_invalid={} rx_token={} rx_direction={} rx_route={} tx_enc_err={} tx_sock_err={} mix_q={}",
                    clients,
                    self._rx_packets,
                    self._tx_packets,
                    self._rx_decode_errors,
                    self._rx_unknown_clients,
                    self._rx_inactive_clients,
                    self._rx_wrong_source,
                    self._rx_invalid_packets,
                    self._rx_invalid_tokens,
                    self._rx_invalid_directions,
                    self._rx_invalid_route_masks,
                    self._tx_encode_errors,
                    self._tx_socket_errors,
                    mix_q,
                )

    def _broadcast_loop(self) -> None:
        _zero_frame = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
        _return_scratch = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
        while not self._stop.is_set():
            try:
                item = self._mix_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if not isinstance(item, _RoutingEnvelope):
                continue
            routing_epoch = int(item.routing_epoch)
            with self._lock:
                if routing_epoch != int(self._routing_epoch):
                    self._stale_mix_epochs_dropped += 1
                    continue
            raw_mix = item.result.regie_linear
            contrib = item.result.regie_contribution_by_client

            ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
            self._seq_out = (self._seq_out + 1) & 0xFFFFFFFF

            dests = self._active_broadcast_destinations()
            with self._lock:
                if routing_epoch != int(self._routing_epoch):
                    self._stale_mix_epochs_dropped += 1
                    continue

            return_frame = _zero_frame
            if bool(self._return_enabled):
                if self._return_frames.pop_into(_return_scratch) is not None:
                    if bool(np.isfinite(_return_scratch).all()):
                        return_frame = _return_scratch

            for client_id, addr, enc, listen_return, listen_regie, return_gain_db, session_token in dests:
                try:
                    udp_port = int(addr[1])
                except Exception:
                    udp_port = 0
                if udp_port <= 0:
                    # The TCP hello did not declare a valid UDP endpoint. Log
                    # once per client instead of silently dropping forever.
                    if not self._silent_dest_logged.get(int(client_id), False):
                        self._silent_dest_logged[int(client_id)] = True
                        logger.info(
                            "client {} declared no valid UDP port; broadcast skipped",
                            int(client_id),
                        )
                    continue
                else:
                    if self._silent_dest_logged.get(int(client_id), False):
                        self._silent_dest_logged.pop(int(client_id), None)
                try:
                    c = contrib.get(int(client_id))
                    mix_minus = build_destination_mix(
                        raw_mix,
                        c,
                        listen_regie=bool(listen_regie),
                        return_frame=return_frame,
                        listen_return=bool(listen_return),
                        return_gain_db=float(return_gain_db),
                    )

                    payload = enc.encode(mix_minus)
                except Exception as e:
                    self._tx_encode_errors += 1
                    logger.debug("broadcast encode failed for {}: {}", addr, e)
                    continue
                try:
                    pkt = pack_audio_packet_v2(
                        session_token=int(session_token),
                        client_id=int(client_id),
                        timestamp_ms=ts_ms,
                        sequence_number=self._seq_out,
                        stream_id=0,
                        bus_mask=0,
                        direction=AudioPacketDirection.SERVER_TO_CLIENT,
                        payload=payload,
                    )
                    stale_epoch = False
                    # Keep the final validation and OS send indivisible with
                    # respect to session revocation. Encoding and packet
                    # construction deliberately stay outside this short gate.
                    with self._routing_io_lock:
                        if routing_epoch != int(self._routing_epoch):
                            stale_epoch = True
                        else:
                            self._sock.sendto(pkt, addr)
                            self._tx_packets += 1
                    if stale_epoch:
                        self._stale_mix_epochs_dropped += 1
                        break
                except Exception as e:
                    self._tx_socket_errors += 1
                    logger.debug("broadcast send failed to {}: {}", addr, e)
