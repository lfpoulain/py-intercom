from __future__ import annotations

import json
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import apply_gain_db, limit_peak, rms_dbfs
from ..common.constants import AUDIO_UDP_PORT, CHANNELS, CONTROL_PORT_OFFSET, FRAME_SAMPLES, JB_MAX_FRAMES, JB_START_FRAMES, MAX_GAIN_DB, SAMPLE_RATE
from ..common.discovery import DiscoveryBeacon
from ..common.devices import list_devices, resolve_device
from ..common.identity import client_id_from_uuid
from ..common.jitter_buffer import OpusPacketJitterBuffer
from ..common.jsonio import atomic_write_json, read_json_file
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import unpack_audio_packet, pack_audio_packet


@dataclass
class ClientState:
    addr: Tuple[str, int]
    last_packet_monotonic: float
    decoder: OpusDecoder = field(default_factory=OpusDecoder, repr=False)
    encoder: OpusEncoder = field(default_factory=OpusEncoder, repr=False)
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
    phase: float = 0.0
    buf: np.ndarray = field(default_factory=lambda: np.zeros((0,), dtype=np.float32))
    underflows: int = 0
    vu_dbfs: float = -60.0
    last_callback_monotonic: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


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

        # Fixed buses (Regie, Plateau, VMix)
        self._buses: Dict[int, AudioBus] = {
            0: AudioBus(
                bus_id=0,
                name="Regie",
                gain_db=0.0,
                feed_to_regie=False,
            ),
            1: AudioBus(
                bus_id=1,
                name="Plateau",
                gain_db=0.0,
                feed_to_regie=False,
            ),
            2: AudioBus(
                bus_id=2,
                name="VMix",
                gain_db=0.0,
                feed_to_regie=False,
            ),
        }
        self._next_bus_id: int = 3

        # items are (raw_mix_48k, contributions_by_client_id)
        self._mix_queue: queue.Queue[object] = queue.Queue(maxsize=50)
        self._stop = threading.Event()
        self._seq_out = 0

        self._rx_packets = 0
        self._rx_datagrams = 0
        self._rx_bytes = 0
        self._rx_socket_errors = 0
        self._tx_packets = 0
        self._rx_decode_errors = 0
        self._tx_encode_errors = 0
        self._tx_socket_errors = 0
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
                if int(bid) not in (1, 2):
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
        self._retry_thread: Optional[threading.Thread] = None
        self._running: bool = False

        self._return_input_device: Optional[int] = int(return_input_device) if return_input_device is not None else None
        self._return_enabled: bool = bool(return_enabled)
        self._return_stream: Optional[sd.InputStream] = None
        self._return_lock = threading.Lock()
        self._return_in_samplerate: int = int(SAMPLE_RATE)
        self._return_in_phase: float = 0.0
        self._return_capture_buf: np.ndarray = np.zeros((0,), dtype=np.float32)
        self._return_frames: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=120)
        self._return_vu_dbfs: float = -60.0
        self._regie_vu_dbfs: float = -60.0

        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_thread: Optional[threading.Thread] = None
        self._ctrl_lock = threading.Lock()
        self._ctrl_sessions: Dict[int, socket.socket] = {}

        default_path = Path.home() / "py-intercom" / "server_preset.json"
        self._preset_path = Path(preset_path) if preset_path else default_path
        self._preset_lock = threading.Lock()
        self._preset_client_by_uuid: Dict[str, dict] = {}

        self._server_name = server_name
        self._discovery_enabled = discovery_enabled
        self._beacon: Optional[DiscoveryBeacon] = None

    def get_preset_paths_snapshot(self) -> dict:
        return {
            "server_preset": str(self._preset_path),
        }

    def load_preset(self) -> None:
        p = self._preset_path
        loaded = read_json_file(p)
        if not isinstance(loaded, dict):
            return
        data: dict = loaded

        outputs = data.get("outputs") if isinstance(data.get("outputs"), list) else []
        buses = data.get("buses") if isinstance(data.get("buses"), dict) else {}
        clients = data.get("clients") if isinstance(data.get("clients"), dict) else {}
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
        )

        feed_by_id = {1: False, 2: False}
        gain_by_id = {0: 0.0, 1: 0.0, 2: 0.0}
        if isinstance(buses, dict):
            for bus_id_str, b in buses.items():
                try:
                    bid = int(bus_id_str)
                except Exception:
                    continue
                if bid not in (1, 2) or not isinstance(b, dict):
                    if bid == 0 and isinstance(b, dict):
                        try:
                            gain_by_id[0] = float(b.get("gain_db", 0.0))
                        except Exception:
                            gain_by_id[0] = 0.0
                    continue
                feed_by_id[int(bid)] = bool(b.get("feed_to_regie", False))
                try:
                    gain_by_id[int(bid)] = float(b.get("gain_db", 0.0))
                except Exception:
                    gain_by_id[int(bid)] = 0.0

        logger.debug("load_preset: return_enabled={} return_input_device={}", bool(return_enabled), return_input_device)
        with self._lock:
            self._buses = {
                0: AudioBus(bus_id=0, name="Regie", gain_db=float(gain_by_id[0]), feed_to_regie=False),
                1: AudioBus(bus_id=1, name="Plateau", gain_db=float(gain_by_id[1]), feed_to_regie=feed_by_id[1]),
                2: AudioBus(bus_id=2, name="VMix", gain_db=float(gain_by_id[2]), feed_to_regie=feed_by_id[2]),
            }
            self._next_bus_id = 3
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
                    if int(bid) not in (0, 1, 2):
                        bid = int(fallback)

                    dev = None
                    try:
                        raw_dev = o.get("device")
                        dev = int(raw_dev) if raw_dev is not None else None
                    except Exception:
                        dev = None
                    dev_name = str(o.get("device_name") or "")
                    dev_hostapi = str(o.get("device_hostapi") or "")
                    dev = resolve_device(dev_name, dev_hostapi, dev, devices)
                    if dev is None:
                        continue

                    oid = int(self._next_output_id)
                    self._next_output_id += 1
                    try:
                        output_gain_db = float(o.get("gain_db", 0.0))
                    except Exception:
                        output_gain_db = 0.0
                    self._outputs[oid] = OutputState(output_id=oid, device=int(dev), bus_id=bid, gain_db=float(output_gain_db))

            now = time.monotonic()
            if isinstance(clients, dict):
                for client_uuid, c in clients.items():
                    u = str(client_uuid or "")
                    if not u:
                        continue
                    cid = int(client_id_from_uuid(u))
                    if cid == 0:
                        continue
                    st = self._clients.get(int(cid))
                    if st is None:
                        st = ClientState(addr=("", 0), last_packet_monotonic=now)
                        self._clients[int(cid)] = st
                    st.client_uuid = u
                    if isinstance(c, dict):
                        try:
                            st.name = str(c.get("name") or "")
                        except Exception:
                            st.name = ""
                        try:
                            st.input_gain_db = float(c.get("input_gain_db", 0.0))
                        except Exception:
                            st.input_gain_db = 0.0
                    st.control_connected = False

        with self._preset_lock:
            self._preset_client_by_uuid = dict(clients) if isinstance(clients, dict) else {}

    def save_preset(self) -> None:
        with self._lock:
            client_uuid_by_id = {int(cid): str(st.client_uuid or "") for cid, st in self._clients.items()}

            try:
                devices = list_devices(hostapi_substring=None, hard_refresh=False, validate=False)
            except Exception:
                devices = []
            device_map = {int(d.index): d for d in devices}

            outputs = []
            for o in self._outputs.values():
                dev_info = device_map.get(int(o.device))
                outputs.append(
                    {
                        "device": int(o.device),
                        "device_name": str(getattr(dev_info, "name", "")) if dev_info is not None else "",
                        "device_hostapi": str(getattr(dev_info, "hostapi", "")) if dev_info is not None else "",
                        "bus_id": int(o.bus_id),
                        "gain_db": float(o.gain_db),
                    }
                )

            return_dev = (
                device_map.get(int(self._return_input_device)) if self._return_input_device is not None else None
            )

            buses: Dict[str, dict] = {
                "0": {
                    "name": "Regie",
                    "feed_to_regie": False,
                    "gain_db": float(self._buses.get(0, AudioBus(0, "Regie")).gain_db),
                },
                "1": {
                    "name": "Plateau",
                    "feed_to_regie": bool(self._buses.get(1, AudioBus(1, "Plateau")).feed_to_regie),
                    "gain_db": float(self._buses.get(1, AudioBus(1, "Plateau")).gain_db),
                },
                "2": {
                    "name": "VMix",
                    "feed_to_regie": bool(self._buses.get(2, AudioBus(2, "VMix")).feed_to_regie),
                    "gain_db": float(self._buses.get(2, AudioBus(2, "VMix")).gain_db),
                },
            }

            clients: Dict[str, dict] = {}
            for _cid, st in self._clients.items():
                u = str(st.client_uuid or "")
                if not u:
                    continue
                clients[u] = {
                    "name": str(st.name or ""),
                    "input_gain_db": float(st.input_gain_db),
                }

        server_data = {
            "version": 1,
            "outputs": outputs,
            "buses": buses,
            "clients": clients,
            "return_enabled": bool(self._return_enabled),
            "return_input_device": int(self._return_input_device) if self._return_input_device is not None else None,
            "return_input_device_name": str(getattr(return_dev, "name", "")) if return_dev is not None else "",
            "return_input_device_hostapi": str(getattr(return_dev, "hostapi", "")) if return_dev is not None else "",
        }

        p = self._preset_path
        atomic_write_json(p, server_data)

        with self._preset_lock:
            self._preset_client_by_uuid = dict(clients)

    def _autosave_preset(self) -> None:
        try:
            self.save_preset()
        except Exception as e:
            logger.debug("autosave preset failed: {}", e)
            return

    def _default_output_bus_id(self) -> int:
        if 1 in self._buses:
            return 1
        if 2 in self._buses:
            return 2
        return 0

    def get_buses_snapshot(self) -> Dict[int, dict]:
        with self._lock:
            return {
                int(bus_id): {
                    "bus_id": int(bus.bus_id),
                    "name": str(bus.name),
                    "gain_db": float(bus.gain_db),
                    "feed_to_regie": bool(bus.feed_to_regie),
                }
                for bus_id, bus in self._buses.items()
            }

    def get_outputs_snapshot(self) -> Dict[int, dict]:
        with self._lock:
            out: Dict[int, dict] = {}
            for output_id, st in self._outputs.items():
                samplerate = int(st.samplerate) if int(st.samplerate) > 0 else int(SAMPLE_RATE)
                try:
                    queued_ms = (1000.0 * float(len(st.buf))) / float(samplerate)
                except Exception:
                    queued_ms = 0.0
                out[int(output_id)] = {
                    "output_id": int(st.output_id),
                    "device": int(st.device),
                    "bus_id": int(st.bus_id),
                    "gain_db": float(st.gain_db),
                    "samplerate": int(samplerate),
                    "queued_ms": float(queued_ms),
                    "underflows": int(st.underflows),
                    "vu_dbfs": float(st.vu_dbfs),
                }
            return out

    def get_clients_snapshot(self) -> Dict[int, dict]:
        now = time.monotonic()
        with self._lock:
            out: Dict[int, dict] = {}
            for client_id, st in self._clients.items():
                try:
                    jb_buf = int(st.jb.buffered_frames)
                    jb_stats = st.jb.stats
                except Exception:
                    jb_buf = 0
                    jb_stats = None
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
                }
            return out

    def get_stats_snapshot(self) -> dict:
        with self._lock:
            clients = len(self._clients)
            outputs = len(self._outputs)
            running = bool(self._running)
            try:
                underflows_total = int(sum(int(o.underflows) for o in self._outputs.values()))
            except Exception:
                underflows_total = 0
        try:
            mix_q = int(self._mix_queue.qsize())
        except Exception:
            mix_q = 0
        return {
            "running": bool(running),
            "clients": int(clients),
            "outputs": int(outputs),
            "mix_q": int(mix_q),
            "underflows": int(underflows_total),
            "return_enabled": bool(self._return_enabled),
            "return_vu_dbfs": float(self._return_vu_dbfs),
            "regie_vu_dbfs": float(self._regie_vu_dbfs),
            "return_q": int(self._return_frames.qsize()) if self._return_enabled else 0,
            "rx_datagrams": int(self._rx_datagrams),
            "rx_bytes": int(self._rx_bytes),
            "rx_socket_errors": int(self._rx_socket_errors),
            "rx_packets": int(self._rx_packets),
            "tx_packets": int(self._tx_packets),
            "rx_decode_errors": int(self._rx_decode_errors),
            "tx_encode_errors": int(self._tx_encode_errors),
            "tx_socket_errors": int(self._tx_socket_errors),
            "opus_ok": bool(self._opus_ok),
            "opus_err": str(self._opus_err),
            "opuslib_version": str(self._opuslib_version),
        }

    def forget_client(self, client_id: int) -> None:
        cid = int(client_id)

        sock = None
        with self._ctrl_lock:
            sock = self._ctrl_sessions.get(int(cid))

        if sock is not None:
            try:
                self._control_send(sock, {"type": "kick", "message": "Tu as été kick"})
            except Exception as e:
                logger.debug("kick send failed to {}: {}", int(cid), e)
            try:
                sock.close()
            except Exception as e:
                logger.debug("kick socket close failed for {}: {}", int(cid), e)
            with self._ctrl_lock:
                if self._ctrl_sessions.get(int(cid)) is sock:
                    self._ctrl_sessions.pop(int(cid), None)

        with self._lock:
            st = self._clients.pop(int(cid), None)

        u = str(st.client_uuid or "") if st is not None else ""
        if u:
            with self._preset_lock:
                self._preset_client_by_uuid.pop(str(u), None)

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
        stream = out.stream
        if stream is None or not self._stream_is_active(stream):
            return True
        if now is None:
            now = time.monotonic()
        with out.lock:
            last_callback = float(out.last_callback_monotonic)
        return last_callback <= 0.0 or (float(now) - float(last_callback)) > 1.0

    def _restart_output_stream(self, out: OutputState) -> None:
        with out.lock:
            old_stream = out.stream
            out.stream = None
        try:
            if old_stream is not None:
                old_stream.stop()
                old_stream.close()
        except Exception:
            pass
        self._open_output_stream(out)

    def _ensure_output_retry_thread(self) -> None:
        t = self._retry_thread
        if t is not None and t.is_alive():
            return
        self._retry_thread = threading.Thread(
            target=self._retry_output_streams,
            name="output-retry",
            daemon=True,
        )
        self._retry_thread.start()

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
            return
        self._close_return_input_stream()
        try:
            self._open_return_input_stream(int(self._return_input_device))
            logger.debug("reopen_return_input: stream reopened on device {}", self._return_input_device)
        except Exception as e:
            logger.warning("reopen_return_input failed: {}", e)

    def set_bus_feed_to_regie(self, bus_id: int, enabled: bool) -> None:
        bid = int(bus_id)
        if bid not in (1, 2):
            return
        with self._lock:
            bus = self._buses.get(int(bid))
            if bus is None:
                return
            bus.feed_to_regie = bool(enabled)

        self._control_push_all_configs()
        self._autosave_preset()

    def set_bus_gain(self, bus_id: int, gain_db: float) -> None:
        bid = int(bus_id)
        if bid not in (0, 1, 2):
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
                changed = True

        if not changed:
            return

        self._control_push_config(int(cid))
        self._autosave_preset()

    def start(self) -> None:
        logger.info("server listening on {}:{}", self.bind_ip, self.port)

        self._running = True

        try:
            rx = threading.Thread(target=self._rx_loop, name="udp-rx", daemon=True)
            tx = threading.Thread(target=self._broadcast_loop, name="udp-tx", daemon=True)
            self._mix_thread = threading.Thread(target=self._mix_loop, name="mix", daemon=True)
            self._ctrl_thread = threading.Thread(target=self._ctrl_accept_loop, name="ctrl-accept", daemon=True)

            rx.start()
            tx.start()
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

            with self._ctrl_lock:
                sessions = list(self._ctrl_sessions.items())
                self._ctrl_sessions.clear()
            for _cid, s in sessions:
                try:
                    s.close()
                except Exception:
                    pass

            with self._lock:
                outs = list(self._outputs.values())

            for out in outs:
                try:
                    if out.stream is not None:
                        out.stream.stop()
                        out.stream.close()
                except Exception:
                    pass

            self._close_return_input_stream()
        finally:
            try:
                self._sock.close()
            except Exception:
                pass

    def _retry_output_streams(self) -> None:
        retries = 3
        delay_s = 1.0
        try:
            for attempt in range(retries):
                if self._stop.is_set():
                    return
                time.sleep(delay_s)
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
            self._retry_thread = None

    def _close_return_input_stream(self) -> None:
        try:
            if self._return_stream is not None:
                self._return_stream.stop()
                self._return_stream.close()
        except Exception:
            pass
        self._return_stream = None

        with self._return_lock:
            self._return_vu_dbfs = -60.0
            self._return_in_phase = 0.0
            self._return_capture_buf = np.zeros((0,), dtype=np.float32)
            try:
                while True:
                    self._return_frames.get_nowait()
            except Exception:
                pass

    def _open_return_input_stream(self, device: int) -> None:
        in_sr = int(SAMPLE_RATE)
        blocksize_frame = int(FRAME_SAMPLES)

        with self._return_lock:
            self._return_in_samplerate = int(in_sr)
            self._return_in_phase = 0.0
            self._return_capture_buf = np.zeros((0,), dtype=np.float32)
            self._return_vu_dbfs = -60.0
            try:
                while True:
                    self._return_frames.get_nowait()
            except Exception:
                pass

        st = sd.InputStream(
            samplerate=float(in_sr),
            channels=1,
            dtype="float32",
            blocksize=blocksize_frame,
            device=int(device),
            latency="low",
            callback=self._return_in_callback,
        )
        st.start()
        self._return_stream = st

    def _return_in_callback(self, indata, frames, time_info, status) -> None:
        if not getattr(self, "_return_cb_logged", False):
            self._return_cb_logged = True
            logger.info("return_in_callback: first call frames={} return_enabled={}", int(frames), bool(self._return_enabled))
        if status:
            logger.debug("return in status: {}", status)

        if self._stop.is_set() or not bool(self._return_enabled):
            return

        if int(frames) != int(FRAME_SAMPLES):
            logger.debug("return in frame mismatch: got={} expected={}", int(frames), int(FRAME_SAMPLES))
            return

        if indata.ndim == 1:
            mono = indata.astype(np.float32, copy=False)
        else:
            if indata.shape[1] == 1:
                mono = indata[:, 0].astype(np.float32, copy=False)
            else:
                mono = np.mean(indata.astype(np.float32, copy=False), axis=1)

        y = limit_peak(mono)
        y = np.clip(y.astype(np.float32, copy=False), -1.0, 1.0)

        with self._return_lock:
            try:
                self._return_vu_dbfs = float(rms_dbfs(y))
            except Exception:
                self._return_vu_dbfs = -60.0

        try:
            self._return_frames.put_nowait(y.astype(np.float32, copy=False))
        except queue.Full:
            try:
                self._return_frames.get_nowait()
            except Exception:
                pass
            try:
                self._return_frames.put_nowait(y.astype(np.float32, copy=False))
            except Exception:
                pass

    def _control_send(self, sock: socket.socket, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        sock.sendall(data)

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
                        "bus_id": int(bus.bus_id),
                        "name": str(bus.name or f"Bus {int(bus.bus_id)}"),
                        "gain_db": float(bus.gain_db),
                        "feed_to_regie": bool(bus.feed_to_regie),
                    }
                    for _bid, bus in sorted(self._buses.items(), key=lambda kv: int(kv[0]))
                ],
            }

        with self._ctrl_lock:
            sock = self._ctrl_sessions.get(int(client_id))
        if sock is None:
            return
        try:
            self._control_send(sock, {"type": "update", "config": cfg})
        except Exception as e:
            logger.debug("control push config failed for {}: {}", int(client_id), e)
            try:
                sock.close()
            except Exception:
                pass
            with self._ctrl_lock:
                if self._ctrl_sessions.get(int(client_id)) is sock:
                    self._ctrl_sessions.pop(int(client_id), None)
            with self._lock:
                st2 = self._clients.get(int(client_id))
                if st2 is not None:
                    st2.control_connected = False

    def _ctrl_accept_loop(self) -> None:
        port = int(self.port) + int(CONTROL_PORT_OFFSET)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind_ip, port))
        s.listen(5)
        s.settimeout(0.5)
        self._ctrl_sock = s
        logger.info("control listening on {}:{}", self.bind_ip, port)

        while not self._stop.is_set():
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._ctrl_session_loop, args=(conn, addr), daemon=True)
            t.start()

    def _ctrl_session_loop(self, conn: socket.socket, addr) -> None:
        conn.settimeout(0.5)
        buf = b""
        client_id: Optional[int] = None

        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                except socket.timeout:
                    continue
                except Exception:
                    break

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue

                    mtype = str(msg.get("type") or "").lower()
                    if mtype == "ping":
                        if client_id is not None:
                            now = time.monotonic()
                            with self._lock:
                                st = self._clients.get(int(client_id))
                                if st is not None:
                                    st.control_connected = True
                                    st.last_control_monotonic = now
                        try:
                            with self._lock:
                                return_vu_dbfs = float(self._return_vu_dbfs)
                            self._control_send(
                                conn,
                                {
                                    "type": "pong",
                                    "t": msg.get("t"),
                                    "return_vu_dbfs": float(return_vu_dbfs),
                                },
                            )
                        except Exception as e:
                            logger.debug("pong send failed: {}", e)
                        continue

                    if mtype == "state":
                        cid = client_id
                        if cid is None:
                            try:
                                cid = int(msg.get("client_id")) & 0xFFFFFFFF
                            except Exception:
                                cid = None
                        if cid is None:
                            continue
                        ptt_buses = msg.get("ptt_buses")
                        listen_return_bus = msg.get("listen_return_bus")
                        listen_regie = msg.get("listen_regie")
                        return_gain_db = msg.get("return_gain_db")
                        input_gain_db = msg.get("input_gain_db")
                        applies_input_gain_locally = msg.get("applies_input_gain_locally")
                        changed = False
                        logger.debug("state from cid={}: listen_return_bus={} listen_regie={}", cid, listen_return_bus, listen_regie)
                        with self._lock:
                            st = self._clients.get(int(cid))
                            if st is not None:
                                if isinstance(ptt_buses, dict):
                                    try:
                                        st.ptt_buses = {int(k): bool(v) for k, v in ptt_buses.items()}
                                    except Exception:
                                        st.ptt_buses = {}
                                if listen_return_bus is not None:
                                    st.listen_return_bus = bool(listen_return_bus)
                                if listen_regie is not None:
                                    st.listen_regie = bool(listen_regie)
                                if return_gain_db is not None:
                                    try:
                                        st.return_gain_db = float(return_gain_db)
                                    except Exception:
                                        st.return_gain_db = 0.0
                                if input_gain_db is not None:
                                    try:
                                        next_input_gain = max(-60.0, min(float(MAX_GAIN_DB), float(input_gain_db)))
                                    except Exception:
                                        next_input_gain = float(st.input_gain_db)
                                    if abs(float(st.input_gain_db) - float(next_input_gain)) >= 0.01:
                                        st.input_gain_db = float(next_input_gain)
                                        changed = True
                                if applies_input_gain_locally is not None:
                                    next_applies = bool(applies_input_gain_locally)
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

                    try:
                        cid = int(msg.get("client_id")) & 0xFFFFFFFF
                    except Exception:
                        continue

                    client_id = int(cid)
                    name = str(msg.get("name") or "")
                    client_uuid = str(msg.get("client_uuid") or "")
                    applies_input_gain_locally = bool(msg.get("applies_input_gain_locally", False))
                    udp_port = None
                    try:
                        raw_udp_port = int(msg.get("udp_port"))
                        if 1 <= raw_udp_port <= 65535:
                            udp_port = int(raw_udp_port)
                    except Exception:
                        udp_port = None

                    with self._ctrl_lock:
                        prev = self._ctrl_sessions.get(int(client_id))
                        self._ctrl_sessions[int(client_id)] = conn
                    if prev is not None and prev is not conn:
                        try:
                            prev.close()
                        except Exception:
                            pass

                    now = time.monotonic()
                    with self._lock:
                        st = self._clients.get(int(client_id))
                        if st is None:
                            st = ClientState(addr=(str(addr[0]), 0), last_packet_monotonic=now)
                            self._clients[int(client_id)] = st
                        # Full reset of audio backend for this client
                        try:
                            st.decoder = OpusDecoder()
                        except Exception:
                            pass
                        st.jb = OpusPacketJitterBuffer(start_frames=JB_START_FRAMES, max_frames=JB_MAX_FRAMES)
                        st.last_timestamp_ms = 0
                        st.last_sequence_number = 0
                        st.name = name
                        st.client_uuid = client_uuid
                        st.applies_input_gain_locally = bool(applies_input_gain_locally)
                        st.ptt_buses = {}
                        st.vu_dbfs = -60.0
                        if udp_port is not None:
                            st.addr = (str(addr[0]), int(udp_port))
                        st.control_connected = True
                        st.last_control_monotonic = now
                        st.last_packet_monotonic = now

                    if client_uuid:
                        with self._preset_lock:
                            cur = self._preset_client_by_uuid.get(str(client_uuid))
                            if isinstance(cur, dict):
                                cur["name"] = str(name or "")
                            else:
                                self._preset_client_by_uuid[str(client_uuid)] = {"name": str(name or "")}

                        self._autosave_preset()

                    self._control_send(conn, {"type": "welcome", "server_time_ms": int(time.time() * 1000)})
                    self._control_push_config(int(client_id))

        finally:
            try:
                conn.close()
            except Exception:
                pass

            if client_id is not None:
                with self._ctrl_lock:
                    if self._ctrl_sessions.get(int(client_id)) is conn:
                        self._ctrl_sessions.pop(int(client_id), None)

                with self._lock:
                    st = self._clients.get(int(client_id))
                    if st is not None:
                        st.control_connected = False

    def create_output(self, device: int, bus_id: int) -> int:
        with self._lock:
            if int(bus_id) not in (0, 1, 2):
                raise ValueError("output bus must be Regie, Plateau, or VMix")

            out = OutputState(output_id=0, device=int(device), bus_id=int(bus_id))
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
        try:
            if out.stream is not None:
                out.stream.stop()
                out.stream.close()
        except Exception:
            pass
        self._autosave_preset()

    def set_output_bus(self, output_id: int, bus_id: int) -> None:
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            if int(bus_id) not in (0, 1, 2):
                return
            out.bus_id = int(bus_id)

        self._autosave_preset()

    def set_output_device(self, output_id: int, device: int) -> None:
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            out.device = int(device)

        # Re-open the stream to apply device changes.
        if self._running and not self._stop.is_set():
            with out.lock:
                old_stream = out.stream
                out.stream = None
            try:
                if old_stream is not None:
                    old_stream.stop()
                    old_stream.close()
            except Exception:
                pass
            self._open_output_stream(out)

        self._autosave_preset()

    def _open_output_stream(self, out: OutputState) -> None:
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
        stream: Optional[sd.OutputStream] = None
        try:
            with out.lock:
                out.samplerate = int(sr)
                out.phase = 0.0
                out.buf = np.zeros((0,), dtype=np.float32)
                out.last_callback_monotonic = 0.0

            stream = sd.OutputStream(
                samplerate=float(sr),
                channels=CHANNELS,
                dtype="float32",
                blocksize=blocksize,
                device=int(out.device),
                latency="low",
                callback=lambda outdata, frames, time_info, status, oid=int(out.output_id): self._output_callback(
                    oid, outdata, frames, time_info, status
                ),
            )
            stream.start()
            with out.lock:
                out.stream = stream
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
            with out.lock:
                out.stream = None
            raise RuntimeError(f"failed to open output stream: {e}") from e

    def _mix_loop(self) -> None:
        tick_s = float(FRAME_SAMPLES) / float(SAMPLE_RATE)
        next_t = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(0.005, next_t - now))
                continue

            produced = 0
            while now >= next_t and produced < 5 and not self._stop.is_set():
                next_t += tick_s
                produced += 1

                with self._lock:
                    buses = {
                        bus_id: (float(bus.gain_db), bool(bus.feed_to_regie))
                        for bus_id, bus in self._buses.items()
                    }
                    clients_snapshot: list[tuple[int, ClientState, Dict[int, bool], bool, float, float, bool]] = []
                    for client_id, st in list(self._clients.items()):
                        try:
                            clients_snapshot.append(
                                (
                                    int(client_id),
                                    st,
                                    dict(st.ptt_buses),
                                    bool(st.control_connected),
                                    float(st.last_control_monotonic),
                                    float(st.input_gain_db),
                                    bool(st.applies_input_gain_locally),
                                )
                            )
                        except Exception:
                            clients_snapshot.append((int(client_id), st, {}, False, 0.0, 0.0, False))
                    outputs = list(self._outputs.values())

                per_client: Dict[int, np.ndarray] = {}
                client_meta: Dict[int, tuple[Dict[int, bool], bool, float]] = {}
                vu_updates: Dict[int, float] = {}

                for client_id, st, ptt_buses, ctrl_ok, last_ctrl, input_gain_db, applies_input_gain_locally in clients_snapshot:
                    try:
                        payload = st.jb.pop()
                    except Exception:
                        payload = None
                    if payload is None:
                        if bool(ctrl_ok) and not any(bool(v) for v in ptt_buses.values()):
                            vu_updates[int(client_id)] = -60.0
                        continue
                    try:
                        frame = st.decoder.decode(payload)
                    except Exception:
                        self._rx_decode_errors += 1
                        continue

                    if not bool(applies_input_gain_locally):
                        try:
                            frame = apply_gain_db(frame.astype(np.float32, copy=False), float(input_gain_db))
                        except Exception:
                            frame = frame.astype(np.float32, copy=False)

                    vu = rms_dbfs(frame)
                    vu_updates[int(client_id)] = float(vu)

                    per_client[int(client_id)] = frame
                    client_meta[int(client_id)] = (
                        dict(ptt_buses),
                        bool(ctrl_ok),
                        float(last_ctrl),
                    )

                if vu_updates:
                    with self._lock:
                        for cid, vu in vu_updates.items():
                            st2 = self._clients.get(int(cid))
                            if st2 is not None:
                                st2.vu_dbfs = float(vu)

                bus_mixes: Dict[int, np.ndarray] = {}

                def _client_active_for_bus(cid: int, bus_id: int) -> bool:
                    meta = client_meta.get(int(cid))
                    if meta is None:
                        return True
                    ptt_buses, ctrl_ok, _last_ctrl = meta
                    # If control is disconnected, rely on client-side gating and do not block.
                    if not bool(ctrl_ok):
                        return True
                    if not ptt_buses:
                        return True
                    return bool(ptt_buses.get(int(bus_id), False))

                bus_selected_ids: Dict[int, list[int]] = {}
                bus_source_contrib: Dict[int, Dict[int, np.ndarray]] = {}

                for bus_id, (bus_gain_db, _feed_to_regie) in buses.items():
                    raw_bus_mix = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
                    selected_ids = [cid for cid in per_client.keys() if _client_active_for_bus(int(cid), int(bus_id))]
                    bus_selected_ids[int(bus_id)] = list(selected_ids)

                    for cid in selected_ids:
                        try:
                            raw_bus_mix += apply_gain_db(per_client[cid], float(bus_gain_db))
                        except Exception:
                            raw_bus_mix += per_client[cid]

                    raw_bus_mix = limit_peak(raw_bus_mix)
                    bus_mixes[int(bus_id)] = raw_bus_mix

                    contrib: Dict[int, np.ndarray] = {}
                    for cid in selected_ids:
                        try:
                            contrib[int(cid)] = apply_gain_db(per_client[cid], float(bus_gain_db))
                        except Exception:
                            contrib[int(cid)] = per_client[cid]
                    bus_source_contrib[int(bus_id)] = contrib

                regie_contrib: Dict[int, np.ndarray] = {}
                for cid in bus_selected_ids.get(0, []):
                    c = bus_source_contrib.get(0, {}).get(int(cid))
                    if isinstance(c, np.ndarray):
                        regie_contrib[int(cid)] = c

                for bid, (_g, feed_to_regie) in buses.items():
                    if int(bid) == 0 or not bool(feed_to_regie):
                        continue
                    contrib = bus_source_contrib.get(int(bid), {})
                    for cid in bus_selected_ids.get(int(bid), []):
                        if int(cid) in regie_contrib:
                            continue
                        c = contrib.get(int(cid))
                        if isinstance(c, np.ndarray):
                            regie_contrib[int(cid)] = c

                regie_mix = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
                for c in regie_contrib.values():
                    regie_mix += c
                regie_mix = limit_peak(regie_mix)

                try:
                    regie_vu_dbfs = float(rms_dbfs(regie_mix))
                except Exception:
                    regie_vu_dbfs = -60.0
                with self._lock:
                    self._regie_vu_dbfs = float(regie_vu_dbfs)

                try:
                    self._mix_queue.put_nowait((regie_mix.copy(), regie_contrib))
                except queue.Full:
                    pass

                for out in outputs:
                    mix = bus_mixes.get(int(out.bus_id))
                    if mix is None:
                        mix = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
                    with out.lock:
                        if out.buf.size == 0:
                            out.buf = mix.astype(np.float32, copy=False)
                        else:
                            out.buf = np.concatenate([out.buf, mix.astype(np.float32, copy=False)])

                        max_samples = int(SAMPLE_RATE * 3)
                        if out.buf.size > max_samples:
                            out.buf = out.buf[-max_samples:]

    def _output_callback(self, output_id: int, outdata, frames, time_info, status) -> None:
        if status:
            logger.debug("audio status: {}", status)

        try:
            out = self._outputs.get(int(output_id))
        except Exception:
            out = None

        if out is None:
            outdata[:] = 0.0
            return

        now = time.monotonic()
        with out.lock:
            out.last_callback_monotonic = float(now)

        out_sr = int(out.samplerate)

        if out_sr != int(SAMPLE_RATE):
            with out.lock:
                out.underflows += 1
                out.vu_dbfs = -60.0
            outdata[:] = 0.0
            return

        if out_sr == int(SAMPLE_RATE):
            with out.lock:
                n = int(frames)
                have = int(out.buf.size)
                output_gain_db = float(out.gain_db)
                take = min(n, have)
                if take > 0:
                    y = out.buf[:take]
                    out.buf = out.buf[take:]
                else:
                    y = np.zeros((0,), dtype=np.float32)

                if take < n:
                    out.underflows += 1
                    if y.size == 0:
                        y = np.zeros((n,), dtype=np.float32)
                    else:
                        y = np.concatenate([y.astype(np.float32, copy=False), np.zeros((n - take,), dtype=np.float32)])

            y = np.clip(y.astype(np.float32, copy=False), -1.0, 1.0)
            try:
                y = apply_gain_db(y, float(output_gain_db))
            except Exception:
                pass
            y = np.clip(y.astype(np.float32, copy=False), -1.0, 1.0)
            with out.lock:
                try:
                    out.vu_dbfs = rms_dbfs(y)
                except Exception:
                    out.vu_dbfs = -60.0
            if outdata.ndim == 1:
                outdata[:] = y
            else:
                outdata[:] = y.reshape(int(frames), 1)
            return

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

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
                pkt = unpack_audio_packet(data)
                seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
                payload = bytes(getattr(pkt, "payload", b""))
            except Exception as e:
                self._rx_decode_errors += 1
                logger.warning("bad packet from {}: {}", addr, e)
                continue

            self._rx_packets += 1

            now = time.monotonic()
            with self._lock:
                st = self._clients.get(pkt.client_id)
                if st is None:
                    st = ClientState(addr=addr, last_packet_monotonic=now)
                    self._clients[pkt.client_id] = st
                    logger.info("new client {} from {}", pkt.client_id, addr)
                else:
                    try:
                        if now - float(st.last_packet_monotonic) > 2.0:
                            st.jb.reset()
                    except Exception:
                        pass
                    st.addr = addr
                    st.last_packet_monotonic = now
                st.last_timestamp_ms = pkt.timestamp_ms
                st.last_sequence_number = pkt.sequence_number
                try:
                    st.jb.push(seq, payload)
                except Exception:
                    pass

            now2 = time.monotonic()
            if now2 - self._last_stats_log >= 5.0:
                self._last_stats_log = now2
                with self._lock:
                    clients = len(self._clients)
                    mix_q = self._mix_queue.qsize()
                logger.debug(
                    "stats: clients={} rx_pkts={} tx_pkts={} rx_dec_err={} tx_enc_err={} tx_sock_err={} mix_q={}",
                    clients,
                    self._rx_packets,
                    self._tx_packets,
                    self._rx_decode_errors,
                    self._tx_encode_errors,
                    self._tx_socket_errors,
                    mix_q,
                )

    def _broadcast_loop(self) -> None:
        _zero_frame = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
        while not self._stop.is_set():
            try:
                item = self._mix_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if isinstance(item, tuple) and len(item) == 2:
                raw_mix, contrib = item
            else:
                raw_mix, contrib = item, {}

            if not isinstance(raw_mix, np.ndarray):
                continue

            ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
            self._seq_out = (self._seq_out + 1) & 0xFFFFFFFF

            with self._lock:
                dests = [
                    (
                        client_id,
                        st.addr,
                        st.encoder,
                        bool(st.listen_return_bus),
                        bool(st.listen_regie),
                        float(st.return_gain_db),
                    )
                    for client_id, st in self._clients.items()
                ]

            return_frame = None
            if bool(self._return_enabled):
                try:
                    return_frame = self._return_frames.get_nowait()
                except Exception:
                    return_frame = None

            if return_frame is None or not isinstance(return_frame, np.ndarray) or int(getattr(return_frame, "shape", [0])[0]) != int(FRAME_SAMPLES):
                return_frame = _zero_frame
            else:
                return_frame = return_frame.astype(np.float32, copy=False)

            for client_id, addr, enc, listen_return, listen_regie, return_gain_db in dests:
                try:
                    if int(addr[1]) <= 0:
                        continue
                except Exception:
                    continue
                try:
                    c = contrib.get(int(client_id))
                    if bool(listen_regie):
                        if c is None:
                            mix_minus = raw_mix
                        else:
                            mix_minus = raw_mix - c
                    else:
                        mix_minus = _zero_frame

                    if bool(listen_return):
                        mix_minus = mix_minus + apply_gain_db(return_frame, float(return_gain_db))
                    mix_minus = limit_peak(mix_minus)
                    mix_minus = np.clip(mix_minus, -1.0, 1.0)

                    payload = enc.encode(mix_minus)
                except Exception as e:
                    self._tx_encode_errors += 1
                    logger.debug("broadcast encode failed for {}: {}", addr, e)
                    continue
                try:
                    pkt = pack_audio_packet(client_id=0, timestamp_ms=ts_ms, sequence_number=self._seq_out, payload=payload)
                    self._sock.sendto(pkt, addr)
                    self._tx_packets += 1
                except Exception as e:
                    self._tx_socket_errors += 1
                    logger.debug("broadcast send failed to {}: {}", addr, e)
