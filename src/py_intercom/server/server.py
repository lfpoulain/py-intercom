from __future__ import annotations

import json
import queue
import socket
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import apply_gain_db, rms_dbfs
from ..common.constants import AUDIO_UDP_PORT, CHANNELS, CONTROL_PORT_OFFSET, FRAME_SAMPLES, SAMPLE_RATE
from ..common.discovery import DiscoveryBeacon
from ..common.devices import list_devices
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
        default_factory=lambda: OpusPacketJitterBuffer(start_frames=3, max_frames=60),
        repr=False,
    )
    vu_dbfs: float = -60.0
    last_timestamp_ms: int = 0
    last_sequence_number: int = 0
    name: str = ""
    client_uuid: str = ""
    control_connected: bool = False
    last_control_monotonic: float = 0.0

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
    stream: Optional[sd.OutputStream] = None
    samplerate: int = int(SAMPLE_RATE)
    phase: float = 0.0
    buf: np.ndarray = field(default_factory=lambda: np.zeros((0,), dtype=np.float32))
    underflows: int = 0
    vu_dbfs: float = -60.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class IntercomServer:
    @staticmethod
    def _limit_peak(x: np.ndarray, limit: float = 0.99) -> np.ndarray:
        try:
            peak = float(np.max(np.abs(x)))
        except Exception:
            return x
        if peak > 1.0 and peak > 0.0:
            x = x * (float(limit) / peak)
        return x

    @staticmethod
    def _client_id_from_uuid(client_uuid: str) -> int:
        try:
            return int(zlib.crc32(str(client_uuid).encode("utf-8")) & 0xFFFFFFFF)
        except Exception:
            return 0

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
                self._outputs[oid] = OutputState(output_id=oid, device=dev, bus_id=bid)
        elif output_device is not None:
            oid = int(self._next_output_id)
            self._next_output_id += 1
            self._outputs[oid] = OutputState(output_id=oid, device=int(output_device), bus_id=self._default_output_bus_id())

        self._mix_thread: Optional[threading.Thread] = None
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

        def _resolve_device(saved_name: str, saved_hostapi: str, fallback_idx: Optional[int]) -> Optional[int]:
            if saved_name and devices:
                for d in devices:
                    if str(d.name) == saved_name and (not saved_hostapi or str(d.hostapi) == saved_hostapi):
                        return int(d.index)
            if fallback_idx is not None:
                try:
                    fallback_idx = int(fallback_idx)
                except Exception:
                    return None
                if not devices:
                    return int(fallback_idx)
                for d in devices:
                    if int(d.index) == int(fallback_idx):
                        return int(d.index)
            return None

        return_input_device = _resolve_device(
            return_input_device_name,
            return_input_device_hostapi,
            return_input_device,
        )

        feed_by_id = {1: True, 2: True}
        if isinstance(buses, dict):
            for bus_id_str, b in buses.items():
                try:
                    bid = int(bus_id_str)
                except Exception:
                    continue
                if bid not in (1, 2) or not isinstance(b, dict):
                    continue
                feed_by_id[int(bid)] = bool(b.get("feed_to_regie", False))

        with self._lock:
            self._buses = {
                0: AudioBus(bus_id=0, name="Regie", gain_db=0.0, feed_to_regie=False),
                1: AudioBus(bus_id=1, name="Plateau", gain_db=0.0, feed_to_regie=feed_by_id[1]),
                2: AudioBus(bus_id=2, name="VMix", gain_db=0.0, feed_to_regie=feed_by_id[2]),
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
                    dev = _resolve_device(dev_name, dev_hostapi, dev)
                    if dev is None:
                        continue

                    oid = int(self._next_output_id)
                    self._next_output_id += 1
                    self._outputs[oid] = OutputState(output_id=oid, device=int(dev), bus_id=bid)

            now = time.monotonic()
            if isinstance(clients, dict):
                for client_uuid, c in clients.items():
                    u = str(client_uuid or "")
                    if not u:
                        continue
                    cid = int(self._client_id_from_uuid(u))
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
                    }
                )

            return_dev = (
                device_map.get(int(self._return_input_device)) if self._return_input_device is not None else None
            )

            buses: Dict[str, dict] = {
                "0": {"name": "Regie", "feed_to_regie": False, "gain_db": 0.0},
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

    def set_return_enabled(self, enabled: bool) -> None:
        self._return_enabled = bool(enabled)
        try:
            if not bool(self._running):
                if not bool(self._return_enabled):
                    self._close_return_input_stream()
                return

            if not bool(self._return_enabled):
                self._close_return_input_stream()
                return

            if self._return_input_device is None:
                return

            self._close_return_input_stream()
            try:
                self._open_return_input_stream(int(self._return_input_device))
            except Exception as e:
                logger.warning("return input restart failed: {}", e)
        finally:
            self._autosave_preset()

    def set_return_input_device(self, device: Optional[int]) -> None:
        self._return_input_device = int(device) if device is not None else None
        try:
            if not bool(self._running):
                return

            self._close_return_input_stream()
            if not bool(self._return_enabled) or self._return_input_device is None:
                return

            try:
                self._open_return_input_stream(int(self._return_input_device))
            except Exception as e:
                logger.warning("return input device switch failed: {}", e)
        finally:
            self._autosave_preset()

    def get_buses_snapshot(self) -> Dict[int, dict]:
        with self._lock:
            return {
                bus_id: {
                    "bus_id": bus.bus_id,
                    "name": bus.name,
                    "gain_db": bus.gain_db,
                    "feed_to_regie": bool(bus.feed_to_regie),
                }
                for bus_id, bus in self._buses.items()
            }

    def get_outputs_snapshot(self) -> Dict[int, dict]:
        with self._lock:
            return {
                output_id: {
                    "output_id": out.output_id,
                    "device": out.device,
                    "bus_id": out.bus_id,
                    "samplerate": out.samplerate,
                    "queued_ms": float(out.buf.size) * 1000.0 / float(SAMPLE_RATE),
                    "underflows": int(out.underflows),
                    "vu_dbfs": float(out.vu_dbfs),
                }
                for output_id, out in self._outputs.items()
            }

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

    def start(self) -> None:
        logger.info("server listening on {}:{}", self.bind_ip, self.port)

        self._running = True

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

        try:
            if self._return_enabled and self._return_input_device is not None:
                self._open_return_input_stream(int(self._return_input_device))
        except Exception as e:
            logger.warning("return input failed to start: {}", e)

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

        y = self._limit_peak(mono)
        y = np.clip(y.astype(np.float32, copy=False), -1.0, 1.0)

        with self._return_lock:
            try:
                self._return_vu_dbfs = float(rms_dbfs(y))
            except Exception:
                self._return_vu_dbfs = -60.0

        try:
            while True:
                self._return_frames.put_nowait(y.astype(np.float32, copy=False))
                break
        except queue.Full:
            try:
                self._return_frames.get_nowait()
            except Exception:
                return

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
                "return_vu_dbfs": float(self._return_vu_dbfs),
                "buses": [
                    {
                        "bus_id": int(bus.bus_id),
                        "name": str(bus.name or f"Bus {int(bus.bus_id)}"),
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

                                st.control_connected = True
                                st.last_control_monotonic = time.monotonic()
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
                        st.name = name
                        st.client_uuid = client_uuid
                        try:
                            st.jb.reset()
                        except Exception:
                            pass
                        st.vu_dbfs = -60.0
                        if udp_port is not None:
                            st.addr = (str(addr[0]), int(udp_port))
                        st.control_connected = True
                        st.last_control_monotonic = now

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
            self._open_output_stream(out)
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
        try:
            with out.lock:
                out.samplerate = int(sr)
                out.phase = 0.0
                out.buf = np.zeros((0,), dtype=np.float32)

            out.stream = sd.OutputStream(
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
            out.stream.start()
            logger.debug(
                "audio output stream started: out_id={} bus_id={} sr={} ch={} blocksize={} latency=low",
                int(out.output_id),
                int(out.bus_id),
                sr,
                CHANNELS,
                blocksize,
            )
        except Exception as e:
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
                    clients_snapshot: list[tuple[int, ClientState, Dict[int, bool], bool, float]] = []
                    for client_id, st in list(self._clients.items()):
                        try:
                            clients_snapshot.append(
                                (
                                    int(client_id),
                                    st,
                                    dict(st.ptt_buses),
                                    bool(st.control_connected),
                                    float(st.last_control_monotonic),
                                )
                            )
                        except Exception:
                            clients_snapshot.append((int(client_id), st, {}, False, 0.0))
                    outputs = list(self._outputs.values())

                per_client: Dict[int, np.ndarray] = {}
                client_meta: Dict[int, tuple[Dict[int, bool], bool, float]] = {}
                vu_updates: Dict[int, float] = {}

                for client_id, st, ptt_buses, ctrl_ok, last_ctrl in clients_snapshot:
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

                    vu = rms_dbfs(frame)
                    vu_updates[int(client_id)] = float(vu)

                    # Per-client gain is fixed to unity.
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

                    raw_bus_mix = self._limit_peak(raw_bus_mix)
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
                regie_mix = self._limit_peak(regie_mix)

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

                try:
                    out.vu_dbfs = rms_dbfs(y)
                except Exception:
                    out.vu_dbfs = -60.0

            y = np.clip(y.astype(np.float32, copy=False), -1.0, 1.0)
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
            except OSError:
                self._rx_socket_errors += 1
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
                return_frame = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
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
                        mix_minus = np.zeros((FRAME_SAMPLES,), dtype=np.float32)

                    if bool(listen_return):
                        mix_minus = mix_minus + apply_gain_db(return_frame, float(return_gain_db))
                    mix_minus = self._limit_peak(mix_minus)
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
