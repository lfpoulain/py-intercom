from __future__ import annotations

import queue
import secrets
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common import opus_codec
from ..common.audio import clamp_gain_db
from ..common.buses import (
    FIXED_BUS_IDS,
    FIXED_BUSES,
    PLATEAU_BUS_ID,
    REGIE_BUS_ID,
)
from ..common.constants import (
    AUDIO_UDP_PORT,
    CTRL_LIVENESS_TIMEOUT_S,
    FRAME_SAMPLES,
    LATENCY_TRIM_AFTER_FRAMES,
    PCM_RING_TRIM_ABOVE_FRAMES,
    SAMPLE_RATE,
    SERVER_MIX_QUEUE_MAX,
    SERVER_RETURN_FRAMES_MAX,
)
from ..common.control_protocol import KICK_MESSAGE
from ..common.discovery import DiscoveryBeacon
from ..common.frame_ring import FrameRingBuffer
from ..common.identity import (
    client_id_from_uuid,
)
from ..common.jsonio import atomic_write_json
from ..common.logging import best_effort
from ..common.persistence import DebouncedJsonWriter
from ..common.preset_schema import (
    PRESET_SCHEMA_VERSION,
)
from ..common.realtime import configure_udp_socket, run_as_audio_thread
from .control import ControlMixin
from .mixer import MixerMixin
from .models import (
    AudioBus,
    ClientProfile,
    ClientState,
    LogThrottle,
    OutputState,
    PresetLoadError,
    PresetOutput,
    SavedDevice,
)
from .outputs import OutputsMixin
from .presets import PresetsMixin
from .return_input import ReturnInputMixin


class IntercomServer(PresetsMixin, OutputsMixin, ReturnInputMixin, ControlMixin, MixerMixin):
    """LAN intercom server engine, independent of Qt.

    The behaviour is split by concern into mixins; this class owns the shared
    state, worker lifecycle and operator-facing snapshots.
    """

    def __init__(
        self,
        bind_ip: str = "0.0.0.0",
        port: int = AUDIO_UDP_PORT,
        output_device: int | None = None,
        outputs: list[dict] | None = None,
        preset_path: str | None = None,
        server_name: str = "py-intercom",
        discovery_enabled: bool = True,
        return_input_device: int | None = None,
        return_enabled: bool = False,
    ):
        self.bind_ip = bind_ip
        self.port = port

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with best_effort("_sock.setsockopt()"):
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        configure_udp_socket(self._sock)
        self._sock.bind((self.bind_ip, self.port))
        self._sock.settimeout(0.5)

        self._opus_ok: bool = True
        self._opus_err: str = ""
        self._opuslib_version: str = ""
        try:
            import opuslib  # type: ignore

            self._opuslib_version = str(getattr(opuslib, "__version__", ""))
            _test_enc = opus_codec.OpusEncoder()
            _test_dec = opus_codec.OpusDecoder()
            test_payload = _test_enc.encode(np.zeros((FRAME_SAMPLES,), dtype=np.float32))
            test_frame = _test_dec.decode(test_payload)
            self._opus_ok = bool(
                getattr(test_frame, "shape", None) is not None and int(test_frame.shape[0]) == int(FRAME_SAMPLES)
            )
        except Exception as e:
            self._opus_ok = False
            self._opus_err = str(e)

        self._lock = threading.Lock()
        self._clients: dict[int, ClientState] = {}

        # Fixed today, data-driven so the audio engine no longer duplicates
        # bus identities that belong to the shared catalog.
        self._buses: dict[int, AudioBus] = {
            definition.bus_id: AudioBus(
                bus_id=definition.bus_id,
                name=definition.name,
                gain_db=0.0,
                feed_to_regie=definition.default_feed_to_regie,
            )
            for definition in FIXED_BUSES
        }

        # Mixed frames waiting for the broadcast worker (RoutingResult).
        self._mix_queue: queue.Queue[object] = queue.Queue(maxsize=SERVER_MIX_QUEUE_MAX)
        self._stop = threading.Event()
        self._seq_out = 0
        self._routing_epoch = 1
        # Advanced by every session revocation. Broadcast compares it, under
        # this gate and right before sendto(), with the value read alongside
        # its destination list: a revoked address never gets one more packet.
        # The gate is not held during encoding or packet building.
        self._routing_io_lock = threading.Lock()

        self._rx_packets = 0
        self._rx_datagrams = 0
        self._rx_bytes = 0
        self._rx_socket_errors = 0
        self._tx_packets = 0
        self._rx_decode_errors = 0
        self._rx_accept_errors = 0
        self._mix_pop_errors = 0
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
        self._last_stats_log = time.monotonic()
        self._bad_packet_log = LogThrottle(10.0)
        self._control_error_log = LogThrottle(10.0)
        # Failed stream reopens: one line per stream and minute.
        self._stream_reopen_log = LogThrottle(60.0)

        self._outputs: dict[int, OutputState] = {}
        self._next_output_id: int = 0
        with self._lock:
            if outputs is not None:
                for entry in outputs:
                    try:
                        self._add_output_locked(
                            entry.get("device"),
                            entry.get("bus_id", REGIE_BUS_ID),
                            entry.get("gain_db", 0.0),
                        )
                    except (TypeError, ValueError) as exc:
                        logger.warning("ignoring invalid output {!r}: {}", entry, exc)
            elif output_device is not None:
                self._add_output_locked(int(output_device), PLATEAU_BUS_ID)

        self._running: bool = False
        self._worker_lock = threading.Lock()
        self._worker_threads: dict[str, threading.Thread] = {}
        self._worker_errors: dict[str, str] = {}
        self._ctrl_session_threads: set[threading.Thread] = set()

        self._return_input_device: int | None = int(return_input_device) if return_input_device is not None else None
        self._return_enabled: bool = bool(return_enabled)
        self._return_stream: sd.InputStream | None = None
        self._return_stream_gate: threading.Event | None = None
        self._return_stream_error: str = ""
        self._return_lock = threading.Lock()
        self._return_last_callback_monotonic = 0.0
        self._return_callback_statuses = 0
        self._return_frame_mismatches = 0
        self._return_frames = FrameRingBuffer(
            capacity=SERVER_RETURN_FRAMES_MAX,
            frame_samples=FRAME_SAMPLES,
            trim_above=PCM_RING_TRIM_ABOVE_FRAMES,
            trim_after=LATENCY_TRIM_AFTER_FRAMES,
        )
        self._return_input_scratch = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
        self._return_vu_dbfs: float = -60.0
        self._regie_vu_dbfs: float = -60.0
        # Written by the mix worker under self._lock, one entry per fixed bus.
        self._bus_vu_dbfs: dict[int, float] = {definition.bus_id: -60.0 for definition in FIXED_BUSES}

        self._ctrl_sock: socket.socket | None = None
        self._ctrl_lock = threading.Lock()
        self._config_push_event = threading.Event()
        # Clients whose microphone gain the operator changed since the last
        # push: only they get their gain back. Guarded by self._lock.
        self._input_gain_push_ids: set[int] = set()
        self._ctrl_sessions: dict[int, socket.socket] = {}
        self._ctrl_send_locks: dict[int, threading.Lock] = {}
        self._ctrl_rejected_sessions = 0
        self._ctrl_hello_timeouts = 0
        self._ctrl_invalid_identities = 0
        self._ctrl_identity_collisions = 0
        self._ctrl_codec_init_errors = 0

        default_path = Path.home() / "py-intercom" / "server_preset.json"
        self._preset_path = Path(preset_path) if preset_path else default_path
        self._preset_status_lock = threading.Lock()
        self._preset_write_blocked = False
        self._preset_load_error: PresetLoadError | None = None
        self._preset_loaded_source_version: int | None = None
        self._client_profiles: dict[str, ClientProfile] = {}
        # Preset entries whose card was absent at loading: never opened, but
        # saved back unchanged so a card switched off at boot is not forgotten.
        self._unresolved_outputs: tuple[PresetOutput, ...] = ()
        self._unresolved_return: SavedDevice | None = None
        self._identity_collisions: dict[int, tuple[str, ...]] = {}
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
        self._beacon: DiscoveryBeacon | None = None

    def get_buses_snapshot(self) -> dict[int, dict]:
        with self._lock:
            snapshot: dict[int, dict] = {}
            for definition in FIXED_BUSES:
                bus = self._buses[definition.bus_id]
                snapshot[definition.bus_id] = {
                    "bus_id": definition.bus_id,
                    "name": definition.name,
                    "gain_db": float(bus.gain_db),
                    "feed_to_regie": (False if definition.bus_id == REGIE_BUS_ID else bool(bus.feed_to_regie)),
                }
            return snapshot

    def get_outputs_snapshot(self) -> dict[int, dict]:
        with self._lock:
            outputs = [
                (int(output_id), st, int(st.device), int(st.bus_id), float(st.gain_db))
                for output_id, st in self._outputs.items()
            ]
        running = bool(self._running) and not self._stop.is_set()
        now = time.monotonic()
        out: dict[int, dict] = {}
        for output_id, st, device, bus_id, gain_db in outputs:
            with st.lock:
                stream = st.stream
                last_callback = float(st.last_callback_monotonic)
                stream_error = str(st.stream_error or "")
                underflows = int(st.underflows)
                callback_statuses = int(st.callback_statuses)
                vu_dbfs = float(st.vu_dbfs)
            ring_stats = st.ring.stats
            queued_ms = 1000.0 * float(ring_stats.depth * st.ring.frame_samples) / float(SAMPLE_RATE)
            # PortAudio is queried without holding a lock the audio callback
            # or the engine needs.
            stream_active = self._stream_is_active(stream)
            if running and not stream_error:
                if not stream_active:
                    stream_error = "la carte son ne joue plus"
                elif self._stream_stalled(last_callback, now=now):
                    stream_error = "la carte son ne répond plus"
            out[output_id] = {
                "output_id": output_id,
                "device": device,
                "bus_id": bus_id,
                "gain_db": gain_db,
                "samplerate": int(SAMPLE_RATE),
                "queued_ms": float(queued_ms),
                "queue_depth": int(ring_stats.depth),
                "queue_capacity": int(st.ring.capacity),
                "queue_pushed": int(ring_stats.pushed),
                "queue_popped": int(ring_stats.popped),
                "queue_dropped": int(ring_stats.dropped),
                "queue_trimmed": int(ring_stats.trimmed),
                "queue_underflows": int(ring_stats.underflows),
                "underflows": underflows,
                "callback_statuses": callback_statuses,
                "vu_dbfs": vu_dbfs,
                "stream_active": bool(stream_active),
                "stream_error": stream_error,
            }
        return out

    def get_clients_snapshot(self) -> dict[int, dict]:
        now = time.monotonic()
        with self._lock:
            out: dict[int, dict] = {}
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
                    "jb_trimmed": 0,
                    "jb_concealed": 0,
                    "jb_fec": 0,
                    "jb_resets": 0,
                    "control_connected": False,
                    "control_age_s": None,
                    "ptt_buses": {},
                    "listen_return_bus": False,
                    "listen_regie": True,
                    "duck_return": False,
                    "identity_collision": bool(collision_uuids),
                    "identity_collision_uuids": list(collision_uuids),
                }
            for client_id, st in self._clients.items():
                jb_stats = st.jb.stats
                collision_uuids = identity_collisions.get(int(client_id), ())
                out[client_id] = {
                    "client_id": client_id,
                    "name": st.name,
                    "client_uuid": st.client_uuid,
                    "input_gain_db": float(st.input_gain_db),
                    # A disconnected client is shown offline, not at its last IP.
                    "addr": st.addr if st.control_connected else None,
                    "age_s": max(0.0, now - st.last_packet_monotonic),
                    "vu_dbfs": st.vu_dbfs,
                    "last_timestamp_ms": st.last_timestamp_ms,
                    "last_sequence_number": st.last_sequence_number,
                    "jb_buf": int(st.jb.buffered_frames),
                    "jb_missing": int(jb_stats.missing),
                    "jb_late_dropped": int(jb_stats.late_dropped),
                    "jb_trimmed": int(jb_stats.trimmed),
                    "jb_concealed": int(jb_stats.concealed),
                    "jb_fec": int(jb_stats.fec),
                    "jb_resets": int(jb_stats.resets),
                    "control_connected": bool(st.control_connected),
                    "control_age_s": max(0.0, now - st.last_control_monotonic) if st.control_connected else None,
                    "ptt_buses": dict(st.ptt_buses),
                    "listen_return_bus": bool(st.listen_return_bus),
                    "listen_regie": bool(st.listen_regie),
                    "duck_return": bool(st.duck_return),
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
            control_connected = sum(1 for st in self._clients.values() if st.control_connected)
            underflows_total = sum(int(o.underflows) for o in self._outputs.values())
            bus_vu_dbfs = dict(self._bus_vu_dbfs)
            udp_rx_age_s = (
                max(0.0, time.monotonic() - float(self._last_valid_udp_monotonic))
                if float(self._last_valid_udp_monotonic) > 0.0
                else None
            )
        mix_q = int(self._mix_queue.qsize())
        with self._worker_lock:
            worker_errors = dict(self._worker_errors)
            worker_alive = {str(name): bool(thread.is_alive()) for name, thread in self._worker_threads.items()}
        with self._return_lock:
            return_stream = self._return_stream
            return_last_callback = float(self._return_last_callback_monotonic)
            return_stream_error = str(self._return_stream_error or "")
            return_ring_stats = self._return_frames.stats
        # PortAudio is queried without holding the lock the Return callback takes.
        return_stream_active = self._stream_is_active(return_stream)
        if self._return_enabled and running and not self._stop.is_set() and not return_stream_error:
            if not return_stream_active:
                return_stream_error = "l'entrée Programme ne capte plus"
            elif self._stream_stalled(return_last_callback, now=time.monotonic()):
                return_stream_error = "la carte de l'entrée Programme ne répond plus"
        return {
            "running": bool(running),
            "clients": int(clients),
            "control_connected_clients": int(control_connected),
            "outputs": int(outputs),
            "mix_q": int(mix_q),
            "underflows": int(underflows_total),
            "return_enabled": bool(self._return_enabled),
            "return_stream_active": bool(return_stream_active),
            "return_stream_error": return_stream_error,
            "return_vu_dbfs": float(self._return_vu_dbfs),
            "regie_vu_dbfs": float(self._regie_vu_dbfs),
            "bus_vu_dbfs": bus_vu_dbfs,
            "return_q": int(return_ring_stats.depth) if self._return_enabled else 0,
            "return_queue_capacity": int(self._return_frames.capacity),
            "return_queue_pushed": int(return_ring_stats.pushed),
            "return_queue_popped": int(return_ring_stats.popped),
            "return_queue_dropped": int(return_ring_stats.dropped),
            "return_queue_trimmed": int(return_ring_stats.trimmed),
            "return_queue_underflows": int(return_ring_stats.underflows),
            "return_callback_statuses": int(self._return_callback_statuses),
            "return_frame_mismatches": int(self._return_frame_mismatches),
            "rx_datagrams": int(self._rx_datagrams),
            "rx_bytes": int(self._rx_bytes),
            "rx_socket_errors": int(self._rx_socket_errors),
            "rx_packets": int(self._rx_packets),
            "tx_packets": int(self._tx_packets),
            "rx_decode_errors": int(self._rx_decode_errors),
            "rx_accept_errors": int(self._rx_accept_errors),
            "mix_pop_errors": int(self._mix_pop_errors),
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

        logger.info("client {} forgotten by the operator{}", cid, ", its session is kicked" if sock is not None else "")
        if sock is not None:
            with best_effort(f"kick send to {int(cid)}"):
                self._control_send_with_lock(sock, send_lock, {"type": "kick", "message": KICK_MESSAGE})
            with best_effort(f"kick socket close for {int(cid)}"):
                sock.close()

        self._autosave_preset()

    def set_bus_feed_to_regie(self, bus_id: int, enabled: bool) -> None:
        bid = int(bus_id)
        if bid == REGIE_BUS_ID or bid not in FIXED_BUS_IDS:
            return
        changed = False
        with self._lock:
            bus = self._buses[bid]
            if bool(bus.feed_to_regie) != bool(enabled):
                bus.feed_to_regie = bool(enabled)
                changed = True

        if not changed:
            return

        self._request_config_push()
        self._autosave_preset()

    def set_bus_gain(self, bus_id: int, gain_db: float) -> None:
        bid = int(bus_id)
        if bid not in FIXED_BUS_IDS:
            return
        next_gain = clamp_gain_db(gain_db)
        changed = False
        with self._lock:
            bus = self._buses[bid]
            if abs(float(bus.gain_db) - float(next_gain)) >= 0.01:
                bus.gain_db = float(next_gain)
                changed = True

        if not changed:
            return

        self._request_config_push()
        self._autosave_preset()

    def set_client_input_gain(self, client_id: int, gain_db: float) -> None:
        cid = int(client_id)
        with self._lock:
            st = self._clients.get(cid)
            if st is not None:
                changed = self._set_client_input_gain_locked(st, gain_db)
                if changed:
                    self._input_gain_push_ids.add(cid)
            else:
                # A known station that has not connected yet: its profile is
                # what its next welcome sends, so a pre-show setting sticks.
                changed = self._set_profile_input_gain_locked(cid, gain_db)

        if not changed:
            return

        if st is not None:
            self._request_config_push()
        self._autosave_preset()

    def _set_profile_input_gain_locked(self, client_id: int, gain_db: object) -> bool:
        next_gain = clamp_gain_db(gain_db)
        for client_uuid, profile in self._client_profiles.items():
            if int(client_id_from_uuid(client_uuid)) != client_id:
                continue
            if abs(float(profile.input_gain_db) - next_gain) < 0.01:
                return False
            profile.input_gain_db = next_gain
            return True
        return False

    def _set_client_input_gain_locked(self, st: ClientState, gain_db: object) -> bool:
        """Apply an input gain to a live client and its persistent profile.

        Shared by the operator fader and the client's own state messages.
        Callers hold ``self._lock``. Returns whether the gain changed.
        """
        next_gain = clamp_gain_db(gain_db)
        if abs(float(st.input_gain_db) - next_gain) < 0.01:
            return False
        st.input_gain_db = next_gain
        client_uuid = str(st.client_uuid or "")
        if client_uuid:
            profile = self._client_profiles.get(client_uuid)
            if profile is None:
                profile = ClientProfile(client_uuid=client_uuid, name=str(st.name or ""))
                self._client_profiles[client_uuid] = profile
            profile.input_gain_db = next_gain
            if st.name:
                profile.name = str(st.name)
        return True

    @staticmethod
    def _control_is_live(
        control_connected: bool,
        last_control_monotonic: float,
        *,
        now: float | None = None,
    ) -> bool:
        if not bool(control_connected):
            return False
        current = time.monotonic() if now is None else float(now)
        last = float(last_control_monotonic)
        # Callers read the clock before taking the engine lock: a hello or
        # ping recorded meanwhile is newer than ``now`` and is live, not stale.
        return bool(last > 0.0 and current - last <= float(CTRL_LIVENESS_TIMEOUT_S))

    @staticmethod
    def _new_session_token() -> int:
        token = 0
        while token == 0:
            token = int(secrets.randbits(64)) & 0xFFFFFFFFFFFFFFFF
        return token

    def _advance_routing_epoch_locked(self) -> int:
        """Invalidate every broadcast destination list taken before now.

        Callers hold self._lock. Broadcast never acquires self._lock while it
        owns this gate, so the state -> routing-I/O lock order cannot invert.
        Audio already mixed is kept: it is a sum over every talker, so
        dropping it would cut all the others to remove one revoked voice.
        """
        with self._routing_io_lock:
            self._routing_epoch = (int(self._routing_epoch) + 1) & 0x7FFFFFFFFFFFFFFF
            if self._routing_epoch == 0:
                self._routing_epoch = 1
            return int(self._routing_epoch)

    def _expire_client_session(self, st: ClientState) -> None:
        """Revoke a client's audio session. Callers hold ``self._lock``.

        Only a live token has audio in flight: the routing epoch (which stops
        any send to the revoked address) advances once per revoked session,
        never again for a session that is already expired.
        """
        revoked = int(st.session_token) != 0
        st.control_connected = False
        st.session_token = 0
        st.ptt_buses = {}
        st.vu_dbfs = -60.0
        st.jb.reset()
        if revoked:
            self._advance_routing_epoch_locked()

    def _worker_entry(self, name: str, target: Callable[[], None], *, critical: bool) -> None:
        error: str | None = None
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

    def _new_worker(
        self, name: str, target: Callable[[], None], *, critical: bool = True, audio: bool = False
    ) -> threading.Thread:
        """``audio`` workers run on the 10 ms deadline and get MMCSS priority."""
        thread = threading.Thread(
            target=run_as_audio_thread if audio else self._worker_entry,
            args=(self._worker_entry, str(name), target) if audio else (str(name), target),
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
            workers = [
                self._new_worker("udp-rx", self._rx_loop, audio=True),
                self._new_worker("udp-tx", self._broadcast_loop, audio=True),
                self._new_worker("mix", self._mix_loop, audio=True),
                self._new_worker("ctrl-accept", self._ctrl_accept_loop),
                self._new_worker("ctrl-push", self._config_push_loop, critical=False),
            ]
            for worker in workers:
                worker.start()

            if self._discovery_enabled:
                self._beacon = DiscoveryBeacon(
                    server_name=self._server_name,
                    audio_port=self.port,
                    bind_ip=self.bind_ip,
                )
                self._beacon.start()

            with self._lock:
                outputs = list(self._outputs.values())

            # A card that will not open (busy, unplugged) must not keep the
            # stations from talking to each other: it shows in error and the
            # supervisor started below keeps retrying it.
            for out in outputs:
                try:
                    with out.open_lock:
                        self._open_output_stream(out)
                except Exception as e:
                    logger.warning("output {} failed to start: {}", int(out.output_id), e)

            try:
                logger.info(
                    "start: return_enabled={} return_input_device={}",
                    bool(self._return_enabled),
                    self._return_input_device,
                )
                if self._return_enabled and self._return_input_device is not None:
                    self._open_return_input_stream(int(self._return_input_device))
                    logger.info("start: return input stream opened on device {}", self._return_input_device)
                else:
                    logger.info(
                        "start: return input stream NOT opened (enabled={} device={})",
                        bool(self._return_enabled),
                        self._return_input_device,
                    )
            except Exception as e:
                logger.error("return input failed to start: {}", e)

            # Outputs and Return that failed above, or die later, are reopened
            # from now on without operator action.
            self._start_stream_supervisor()
        except Exception:
            with best_effort("rollback after failed start"):
                self.stop()
            raise

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._beacon is not None:
            self._beacon.stop()
            self._beacon = None
        try:
            with best_effort("close control socket"):
                if self._ctrl_sock is not None:
                    self._ctrl_sock.close()
            self._ctrl_sock = None

            with best_effort("_sock.close()"):
                self._sock.close()

            with self._ctrl_lock:
                sessions = list(self._ctrl_sessions.items())
                self._ctrl_sessions.clear()
                self._ctrl_send_locks.clear()
            for _cid, s in sessions:
                with best_effort("s.close()"):
                    s.close()

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
