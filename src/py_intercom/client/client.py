from __future__ import annotations
from collections import deque

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Deque, Optional

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import apply_gain_db, rms_dbfs
from ..common.constants import AUDIO_UDP_PORT, CONTROL_PORT_OFFSET, FRAME_SAMPLES, SAMPLE_RATE
from ..common.jitter_buffer import OpusPacketJitterBuffer
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import pack_audio_packet, unpack_audio_packet


@dataclass
class ClientConfig:
    server_ip: str
    server_port: int = AUDIO_UDP_PORT
    control_port: Optional[int] = None

    name: str = ""
    mode: str = "always_on"
    client_uuid: str = ""
    input_device: Optional[int] = None
    output_device: Optional[int] = None
    input_gain_db: float = 0.0
    output_gain_db: float = 0.0
    muted: bool = False
    sidetone_enabled: bool = False
    sidetone_gain_db: float = -12.0

    ptt_general_key: Optional[str] = None
    ptt_bus_keys: Optional[dict] = None
    mute_buses: Optional[dict] = None
    tx_mode_buses: Optional[dict] = None
    output_mute_buses: Optional[dict] = None

    listen_return_bus: bool = False


class IntercomClient:
    @staticmethod
    def _limit_peak(x: np.ndarray, limit: float = 0.99) -> np.ndarray:
        try:
            peak = float(np.max(np.abs(x)))
        except Exception:
            return x
        if peak > 1.0 and peak > 0.0:
            x = x * (float(limit) / peak)
        return x

    def __init__(self, client_id: int, config: ClientConfig):
        self.client_id = client_id & 0xFFFFFFFF
        self.config = config

        if self.config.control_port is None:
            self.config.control_port = int(self.config.server_port) + int(CONTROL_PORT_OFFSET)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        except Exception:
            pass
        self._sock.bind(("0.0.0.0", 0))
        self._server_addr = (self.config.server_ip, int(self.config.server_port))
        self._sock.settimeout(0.5)

        self._enc = OpusEncoder()
        self._dec = OpusDecoder()

        self._jb = OpusPacketJitterBuffer(start_frames=3, max_frames=60)

        self._opus_ok: bool = True
        self._opus_err: str = ""
        self._opuslib_version: str = ""
        try:
            import opuslib  # type: ignore

            self._opuslib_version = str(getattr(opuslib, "__version__", ""))
            test_payload = self._enc.encode(np.zeros((FRAME_SAMPLES,), dtype=np.float32))
            test_frame = self._dec.decode(test_payload)
            self._opus_ok = bool(getattr(test_frame, "shape", None) is not None and int(test_frame.shape[0]) == int(FRAME_SAMPLES))
        except Exception as e:
            self._opus_ok = False
            self._opus_err = str(e)

        self._seq = 0
        self._stop = threading.Event()

        self._kicked: bool = False
        self._kick_message: str = ""

        self._state_lock = threading.Lock()
        self._input_gain_db = float(self.config.input_gain_db)
        self._output_gain_db = float(self.config.output_gain_db)
        self._muted = bool(self.config.muted)
        self._sidetone_enabled = bool(self.config.sidetone_enabled)
        self._sidetone_gain_db = float(self.config.sidetone_gain_db)
        self._in_vu_dbfs = -60.0
        self._out_vu_dbfs = -60.0
        self._return_vu_dbfs = -60.0

        self._in_channels = 1
        self._out_channels = 1

        self._in_samplerate = SAMPLE_RATE
        self._in_phase = 0.0
        self._capture_buf = np.zeros((0,), dtype=np.float32)

        self._out_samplerate = SAMPLE_RATE
        self._out_phase = 0.0
        self._playback_buf = np.zeros((0,), dtype=np.float32)

        self._sidetone_frames: Deque[np.ndarray] = deque(maxlen=200)

        self._tx_packets = 0
        self._tx_udp_sent = 0
        self._rx_packets = 0
        self._opus_encode_errors = 0
        self._opus_decode_errors = 0
        self._tx_socket_errors = 0
        self._rx_socket_errors = 0
        self._playout_underflows = 0
        self._last_stats_log = time.monotonic()

        self._in_stream: Optional[sd.InputStream] = None
        self._out_stream: Optional[sd.OutputStream] = None

        self._rx_thread: Optional[threading.Thread] = None

        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_thread: Optional[threading.Thread] = None
        self._ctrl_send_lock = threading.Lock()
        self._ctrl_connected: bool = False
        self._ctrl_last_rx_monotonic: float = 0.0
        self._ctrl_last_tx_monotonic: float = 0.0
        self._ctrl_routes: Optional[dict] = None

        self._ptt_general: bool = False
        self._ptt_buses: dict[int, bool] = {}
        self._mute_buses: dict[int, bool] = {}
        self._tx_mode_buses: dict[int, str] = {}
        self._tx_modes_configured: bool = False
        self._output_mute_buses: dict[int, bool] = {}
        self._listen_return_bus: bool = bool(self.config.listen_return_bus)

        has_explicit_tx_modes = isinstance(self.config.tx_mode_buses, dict)
        self._tx_modes_configured = bool(has_explicit_tx_modes)
        try:
            if isinstance(self.config.tx_mode_buses, dict):
                for k, v in self.config.tx_mode_buses.items():
                    mode = str(v or "").strip().lower()
                    if mode not in ("ptt", "always_on"):
                        continue
                    self._tx_mode_buses[int(k)] = mode
        except Exception:
            pass

        if not has_explicit_tx_modes:
            base_mode = str(self.config.mode or "always_on").strip().lower()
            if base_mode not in ("ptt", "always_on"):
                base_mode = "always_on"
            for bid in (0, 1, 2):
                self._tx_mode_buses[int(bid)] = str(base_mode)

        try:
            output_mute_buses = self.config.output_mute_buses
            if not isinstance(output_mute_buses, dict):
                output_mute_buses = self.config.mute_buses
            if isinstance(output_mute_buses, dict):
                for k, v in output_mute_buses.items():
                    self._output_mute_buses[int(k)] = bool(v)
        except Exception:
            self._output_mute_buses = {}
        self._mute_buses = dict(self._output_mute_buses)

    def get_stats_snapshot(self) -> dict:
        with self._state_lock:
            try:
                capture_samples = int(self._capture_buf.size)
            except Exception:
                capture_samples = 0
            try:
                playback_samples = int(self._jb.buffered_frames) * int(FRAME_SAMPLES)
            except Exception:
                playback_samples = 0
            try:
                sidetone_samples = int(len(self._sidetone_frames)) * int(FRAME_SAMPLES)
            except Exception:
                sidetone_samples = 0

            ctrl_age_s = (
                max(0.0, time.monotonic() - float(self._ctrl_last_rx_monotonic)) if self._ctrl_connected else None
            )
            ctrl_tx_age_s = (
                max(0.0, time.monotonic() - float(self._ctrl_last_tx_monotonic)) if self._ctrl_connected else None
            )

            return {
                "muted": self._muted,
                "input_gain_db": self._input_gain_db,
                "output_gain_db": self._output_gain_db,
                "sidetone_enabled": self._sidetone_enabled,
                "sidetone_gain_db": self._sidetone_gain_db,
                "in_vu_dbfs": self._in_vu_dbfs,
                "out_vu_dbfs": self._out_vu_dbfs,
                "return_vu_dbfs": self._return_vu_dbfs,
                "in_channels": self._in_channels,
                "out_channels": self._out_channels,
                "in_samplerate": self._in_samplerate,
                "out_samplerate": self._out_samplerate,
                "capture_samples": int(capture_samples),
                "playback_samples": int(playback_samples),
                "sidetone_samples": int(sidetone_samples),
                "tx_packets": int(self._tx_packets),
                "tx_udp_sent": int(self._tx_udp_sent),
                "rx_packets": int(self._rx_packets),
                "opus_encode_errors": int(self._opus_encode_errors),
                "opus_decode_errors": int(self._opus_decode_errors),
                "tx_socket_errors": int(self._tx_socket_errors),
                "rx_socket_errors": int(self._rx_socket_errors),
                "playout_underflows": int(self._playout_underflows),
                "opus_ok": bool(self._opus_ok),
                "opus_err": str(self._opus_err),
                "opuslib_version": str(self._opuslib_version),
                "stopped": bool(self._stop.is_set()),
                "kicked": bool(self._kicked),
                "kick_message": str(self._kick_message),
                "control_connected": bool(self._ctrl_connected),
                "control_age_s": ctrl_age_s,
                "control_tx_age_s": ctrl_tx_age_s,
                "ptt_general": bool(self._ptt_general),
                "ptt_buses": dict(self._ptt_buses),
                "mute_buses": dict(self._mute_buses),
                "tx_mode_buses": dict(self._tx_mode_buses),
                "output_mute_buses": dict(self._output_mute_buses),
                "listen_return_bus": bool(self._listen_return_bus),
                "routes": dict(self._ctrl_routes or {}),
                "name": str(self.config.name or ""),
                "mode": str(self.config.mode or ""),
            }

    def set_ptt_general(self, active: bool) -> None:
        with self._state_lock:
            self._ptt_general = bool(active)
        self._control_send_state()

    def set_ptt_bus(self, bus_id: int, active: bool) -> None:
        with self._state_lock:
            self._ptt_buses[int(bus_id)] = bool(active)
        self._control_send_state()

    def set_mute_bus(self, bus_id: int, muted: bool) -> None:
        self.set_output_mute_bus(int(bus_id), bool(muted))

    def set_output_mute_bus(self, bus_id: int, muted: bool) -> None:
        with self._state_lock:
            self._output_mute_buses[int(bus_id)] = bool(muted)
            self._mute_buses[int(bus_id)] = bool(muted)
        self._control_send_state()

    def set_tx_mode_bus(self, bus_id: int, mode: str) -> None:
        mode_norm = str(mode or "").strip().lower()
        if mode_norm in ("", "off", "none", "disabled"):
            with self._state_lock:
                self._tx_modes_configured = True
                self._tx_mode_buses.pop(int(bus_id), None)
            self._control_send_state()
            return
        if mode_norm not in ("ptt", "always_on"):
            return
        with self._state_lock:
            self._tx_modes_configured = True
            self._tx_mode_buses[int(bus_id)] = mode_norm
        self._control_send_state()

    def set_listen_return_bus(self, enabled: bool) -> None:
        with self._state_lock:
            self._listen_return_bus = bool(enabled)
        self._control_send_state()

    def set_muted(self, muted: bool, from_control: bool = False) -> None:
        with self._state_lock:
            self._muted = bool(muted)

        if not from_control:
            self._control_send_state(muted=bool(muted))

    def set_input_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._input_gain_db = float(gain_db)

    def set_output_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._output_gain_db = float(gain_db)

    def set_sidetone_enabled(self, enabled: bool) -> None:
        with self._state_lock:
            self._sidetone_enabled = bool(enabled)

    def set_sidetone_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._sidetone_gain_db = float(gain_db)

    def start(self) -> None:
        try:
            self._stop.clear()
        except Exception:
            pass

        try:
            sd.stop()
        except Exception:
            pass

        logger.info(
            "client starting: server={}:{} in_dev={} out_dev={} sr={} frame={}",
            self.config.server_ip,
            self.config.server_port,
            str(self.config.input_device),
            str(self.config.output_device),
            SAMPLE_RATE,
            FRAME_SAMPLES,
        )

        self._rx_thread = threading.Thread(target=self._rx_loop, name="udp-rx", daemon=True)
        self._rx_thread.start()

        self._ctrl_thread = threading.Thread(target=self._control_loop, name="ctrl", daemon=True)
        self._ctrl_thread.start()

        self._out_stream = self._open_output_stream()
        self._out_stream.start()

        self._in_stream = self._open_input_stream()
        self._in_stream.start()

    def _stop_network(self) -> None:
        """Tear down control TCP and network threads. UDP socket is kept alive."""
        self._stop.set()

        try:
            if self._ctrl_sock is not None:
                self._ctrl_sock.close()
        except Exception:
            pass
        self._ctrl_sock = None

        # Do NOT close self._sock (UDP) here — it stays alive so the
        # same ephemeral port is reused on reconnect (Windows firewall).

        try:
            if self._ctrl_thread is not None and self._ctrl_thread.is_alive():
                self._ctrl_thread.join(timeout=2.0)
        except Exception:
            pass
        self._ctrl_thread = None

        try:
            if self._rx_thread is not None and self._rx_thread.is_alive():
                self._rx_thread.join(timeout=2.0)
        except Exception:
            pass
        self._rx_thread = None

        self._ctrl_connected = False

    def _deferred_disconnect(self) -> None:
        """Disconnect from a separate thread (used by kick handler to avoid self-join deadlock)."""
        try:
            self.disconnect_network()
        except Exception:
            pass

    def disconnect_network(self) -> None:
        """Disconnect from server (network only). Audio streams stay alive."""
        self._stop_network()

    def reconnect_network(self) -> None:
        """Reconnect to server (network only). Audio streams and UDP socket stay alive."""
        self._stop_network()

        # Reuse existing UDP socket (same ephemeral port — avoids Windows firewall issues)
        self._server_addr = (self.config.server_ip, int(self.config.server_port))

        # Reset audio buffers and phase to avoid stale data
        with self._state_lock:
            self._capture_buf = np.zeros((0,), dtype=np.float32)
            self._in_phase = 0.0
            self._playback_buf = np.zeros((0,), dtype=np.float32)
            self._out_phase = 0.0
            try:
                self._sidetone_frames.clear()
            except Exception:
                pass

        try:
            self._jb.reset()
        except Exception:
            pass

        # Reset network state and diagnostic counters
        self._stop.clear()
        self._kicked = False
        self._kick_message = ""
        self._ctrl_connected = False
        self._ctrl_routes = None
        self._tx_packets = 0
        self._tx_udp_sent = 0
        self._rx_packets = 0
        self._tx_socket_errors = 0
        self._rx_socket_errors = 0

        # Restart network threads
        self._rx_thread = threading.Thread(target=self._rx_loop, name="udp-rx", daemon=True)
        self._rx_thread.start()

        self._ctrl_thread = threading.Thread(target=self._control_loop, name="ctrl", daemon=True)
        self._ctrl_thread.start()

        logger.info("network reconnected to {}:{}", self.config.server_ip, self.config.server_port)

    def stop(self) -> None:
        """Full stop: network + audio streams + UDP socket."""
        self._stop_network()

        # Close UDP socket only on full stop
        try:
            self._sock.close()
        except Exception:
            pass

        try:
            if self._in_stream is not None:
                try:
                    self._in_stream.abort()
                except Exception:
                    pass
                self._in_stream.stop()
                self._in_stream.close()
        except Exception:
            pass
        self._in_stream = None
        try:
            if self._out_stream is not None:
                try:
                    self._out_stream.abort()
                except Exception:
                    pass
                self._out_stream.stop()
                self._out_stream.close()
        except Exception:
            pass
        self._out_stream = None

    def _control_send(self, sock: socket.socket, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._ctrl_send_lock:
            sock.sendall(data)

    def _control_send_state(self, muted: Optional[bool] = None) -> None:
        sock = self._ctrl_sock
        if sock is None or not self._ctrl_connected:
            return
        with self._state_lock:
            ptt_general = bool(self._ptt_general)
            ptt_buses = dict(self._ptt_buses)
            mute_buses = dict(self._mute_buses)
            tx_mode_buses = dict(self._tx_mode_buses)
            output_mute_buses = dict(self._output_mute_buses)
            listen_return_bus = bool(self._listen_return_bus)
        msg: dict = {
            "type": "state",
            "client_id": int(self.client_id),
            "ptt_general": bool(ptt_general),
            "ptt_buses": dict(ptt_buses),
            "mute_buses": dict(mute_buses),
            "tx_mode_buses": dict(tx_mode_buses),
            "output_mute_buses": dict(output_mute_buses),
            "listen_return_bus": bool(listen_return_bus),
        }
        if muted is not None:
            msg["muted"] = bool(muted)
        try:
            self._control_send(sock, msg)
        except Exception as e:
            logger.debug("control send state failed: {}", e)
            return

    def _can_transmit_audio(self) -> bool:
        with self._state_lock:
            muted = bool(self._muted)
            mode = str(self.config.mode or "always_on")
            ptt_general = bool(self._ptt_general)
            ptt_buses = dict(self._ptt_buses)
            tx_mode_buses = dict(self._tx_mode_buses)
            tx_modes_configured = bool(self._tx_modes_configured)

        if tx_modes_configured:
            has_routing = False
            for bus_id, tx_mode in tx_mode_buses.items():
                tx_mode_norm = str(tx_mode or "").strip().lower()
                if tx_mode_norm not in ("ptt", "always_on"):
                    continue
                has_routing = True
                if tx_mode_norm == "always_on":
                    if not muted:
                        return True
                    continue
                if ptt_general or bool(ptt_buses.get(int(bus_id), False)):
                    return True
            if has_routing:
                return False

        if mode != "ptt":
            if muted:
                return False
            return True

        if ptt_general:
            return True

        return any(bool(v) for v in ptt_buses.values())

    def _control_handle_msg(self, msg: dict) -> None:
        mtype = str(msg.get("type") or "").lower()
        if mtype == "kick":
            try:
                with self._state_lock:
                    self._kicked = True
                    self._kick_message = str(msg.get("message") or "Tu as été kick")
            except Exception:
                pass
            # Defer disconnect to a separate thread to avoid deadlock:
            # we are ON the control thread, so calling disconnect_network()
            # here would try to join the control thread from itself.
            threading.Thread(target=self._deferred_disconnect, daemon=True).start()
            return
        if mtype in ("welcome", "update"):
            cfg = msg.get("config") if isinstance(msg.get("config"), dict) else None
            if cfg is None and mtype == "update":
                cfg = msg

            if isinstance(cfg, dict):
                if "muted" in cfg:
                    try:
                        self.set_muted(bool(cfg.get("muted")), from_control=True)
                    except Exception:
                        pass
                if "routes" in cfg and isinstance(cfg.get("routes"), dict):
                    try:
                        self._ctrl_routes = dict(cfg.get("routes") or {})
                    except Exception:
                        pass
                if "return_vu_dbfs" in cfg:
                    try:
                        with self._state_lock:
                            self._return_vu_dbfs = float(cfg.get("return_vu_dbfs"))
                    except Exception:
                        pass
        elif mtype == "pong":
            if "return_vu_dbfs" in msg:
                try:
                    with self._state_lock:
                        self._return_vu_dbfs = float(msg.get("return_vu_dbfs"))
                except Exception:
                    pass
            return

    def _control_loop(self) -> None:
        backoff_s = 1.0
        liveness_timeout_s = 6.0

        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect((self.config.server_ip, int(self.config.control_port or 0)))
                sock.settimeout(0.5)
                self._ctrl_sock = sock
                self._ctrl_connected = True
                self._ctrl_last_rx_monotonic = time.monotonic()
                self._ctrl_last_tx_monotonic = time.monotonic()
                backoff_s = 1.0

                hello = {
                    "type": "hello",
                    "version": 1,
                    "client_id": int(self.client_id),
                    "client_uuid": str(self.config.client_uuid or ""),
                    "name": str(self.config.name or ""),
                    "mode": str(self.config.mode or "always_on"),
                    "udp_port": int(self._sock.getsockname()[1]),
                }
                self._control_send(sock, hello)

                self._control_send_state(muted=bool(self._muted))

                buf = b""
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now - float(self._ctrl_last_rx_monotonic) >= float(liveness_timeout_s):
                        logger.debug("control liveness timeout (no rx for {:.1f}s)", float(liveness_timeout_s))
                        break
                    if now - float(self._ctrl_last_tx_monotonic) >= 2.0:
                        try:
                            self._control_send(sock, {"type": "ping", "t": int(time.time() * 1000)})
                            self._ctrl_last_tx_monotonic = now
                        except Exception:
                            break

                    try:
                        chunk = sock.recv(4096)
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
                        if isinstance(msg, dict):
                            self._ctrl_last_rx_monotonic = time.monotonic()
                            self._control_handle_msg(msg)

            except Exception as e:
                logger.debug("control loop error: {}", e)
            finally:
                self._ctrl_connected = False
                try:
                    if self._ctrl_sock is not None:
                        self._ctrl_sock.close()
                except Exception:
                    pass
                self._ctrl_sock = None

            if self._stop.is_set():
                break
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2.0, 10.0)

    def _open_input_stream(self) -> sd.InputStream:
        dev_info = None
        try:
            if self.config.input_device is not None:
                dev_info = sd.query_devices(self.config.input_device)
        except Exception:
            dev_info = None

        if dev_info is not None:
            logger.debug(
                "input device: name={} hostapi={} max_in={} default_sr={}",
                dev_info.get("name"),
                dev_info.get("hostapi"),
                dev_info.get("max_input_channels"),
                dev_info.get("default_samplerate"),
            )

        max_in = 2
        if dev_info is not None:
            try:
                max_in = int(dev_info.get("max_input_channels", 2))
            except Exception:
                max_in = 2
        candidate_channels = []
        for ch in (1, 2, max_in):
            if isinstance(ch, int) and ch > 0 and ch not in candidate_channels:
                candidate_channels.append(ch)

        samplerates = [SAMPLE_RATE]

        errors = []
        for sr in samplerates:
            for ch in candidate_channels:
                blocksize_frame = int(round(float(FRAME_SAMPLES) * float(sr) / float(SAMPLE_RATE)))
                blocksize_frame = max(0, blocksize_frame)
                for blocksize in (blocksize_frame, 0):
                    for latency in ("low", None):
                        try:
                            logger.debug(
                                "opening InputStream: dev={} ch={} blocksize={} latency={} sr={}",
                                str(self.config.input_device),
                                ch,
                                str(blocksize),
                                latency,
                                sr,
                            )
                            st = sd.InputStream(
                                samplerate=sr,
                                channels=ch,
                                dtype="float32",
                                blocksize=blocksize,
                                device=self.config.input_device,
                                latency=latency,
                                callback=self._in_callback,
                            )
                            with self._state_lock:
                                self._in_channels = ch
                                self._in_samplerate = int(sr)
                                self._in_phase = 0.0
                                self._capture_buf = np.zeros((0,), dtype=np.float32)
                            return st
                        except Exception as e:
                            errors.append(f"sr={sr} ch={ch} blocksize={blocksize} latency={latency}: {e}")
                            logger.warning("InputStream open failed: {}", errors[-1])

        raise RuntimeError("failed to open input stream. attempts:\n" + "\n".join(errors))

    def _open_output_stream(self) -> sd.OutputStream:
        dev_info = None
        try:
            if self.config.output_device is not None:
                dev_info = sd.query_devices(self.config.output_device)
        except Exception:
            dev_info = None

        if dev_info is not None:
            logger.debug(
                "output device: name={} hostapi={} max_out={} default_sr={}",
                dev_info.get("name"),
                dev_info.get("hostapi"),
                dev_info.get("max_output_channels"),
                dev_info.get("default_samplerate"),
            )

        max_out = 2
        if dev_info is not None:
            try:
                max_out = int(dev_info.get("max_output_channels", 2))
            except Exception:
                max_out = 2
        candidate_channels = []
        for ch in (1, 2, max_out):
            if isinstance(ch, int) and ch > 0 and ch not in candidate_channels:
                candidate_channels.append(ch)

        samplerates = [SAMPLE_RATE]

        errors = []
        for sr in samplerates:
            for ch in candidate_channels:
                for latency in ("low", None):
                    try:
                        logger.debug(
                            "opening OutputStream: dev={} ch={} blocksize={} latency={} sr={}",
                            str(self.config.output_device),
                            ch,
                            str(FRAME_SAMPLES),
                            latency,
                            sr,
                        )
                        st = sd.OutputStream(
                            samplerate=sr,
                            channels=ch,
                            dtype="float32",
                            blocksize=int(FRAME_SAMPLES),
                            device=self.config.output_device,
                            latency=latency,
                            callback=self._out_callback,
                        )
                        with self._state_lock:
                            self._out_channels = ch
                            self._out_samplerate = sr
                            self._playback_buf = np.zeros((0,), dtype=np.float32)
                            try:
                                self._sidetone_frames.clear()
                            except Exception:
                                pass
                        return st
                    except Exception as e:
                        errors.append(f"sr={sr} ch={ch} blocksize={int(FRAME_SAMPLES)} latency={latency}: {e}")
                        logger.warning("OutputStream open failed: {}", errors[-1])

        raise RuntimeError("failed to open output stream. attempts:\n" + "\n".join(errors))

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _in_callback(self, indata, frames, time_info, status) -> None:
        if status:
            logger.debug("audio in status: {}", status)

        # Network disconnected — keep callback alive but skip processing
        if self._stop.is_set():
            return

        if not self._can_transmit_audio():
            return

        with self._state_lock:
            gain_db = self._input_gain_db
            in_sr = int(self._in_samplerate)

        if indata.ndim == 1:
            mono = indata.astype(np.float32, copy=False)
        else:
            if indata.shape[1] == 1:
                mono = indata[:, 0].astype(np.float32, copy=False)
            else:
                mono = np.mean(indata.astype(np.float32, copy=False), axis=1)

        mono = apply_gain_db(mono, gain_db)

        with self._state_lock:
            if self._capture_buf.size == 0:
                self._capture_buf = mono
            else:
                self._capture_buf = np.concatenate((self._capture_buf, mono))

            max_samples = int(in_sr * 3)
            if self._capture_buf.size > max_samples:
                self._capture_buf = self._capture_buf[-max_samples:]

        ratio = float(in_sr) / float(SAMPLE_RATE)

        while True:
            with self._state_lock:
                phase = float(self._in_phase)
                src = self._capture_buf

            need = int(np.ceil((FRAME_SAMPLES - 1) * ratio + phase + 2))
            if src.size < need:
                return

            positions = phase + np.arange(FRAME_SAMPLES, dtype=np.float32) * ratio
            idx0 = np.floor(positions).astype(np.int64)
            frac = (positions - idx0.astype(np.float32)).astype(np.float32)
            idx1 = idx0 + 1

            if int(idx1.max(initial=0)) >= int(src.size):
                return

            y = src[idx0] * (1.0 - frac) + src[idx1] * frac
            y = self._limit_peak(y)
            y = np.clip(y.astype(np.float32, copy=False), -1.0, 1.0)

            new_phase = float(positions[-1] + ratio)
            drop = int(new_phase)
            new_phase = new_phase - drop

            with self._state_lock:
                self._in_phase = new_phase
                if drop > 0 and self._capture_buf.size >= drop:
                    self._capture_buf = self._capture_buf[drop:]
                self._in_vu_dbfs = rms_dbfs(y)

                if self._sidetone_enabled:
                    try:
                        self._sidetone_frames.append(y.astype(np.float32, copy=True))
                    except Exception:
                        pass

            try:
                payload = self._enc.encode(y.astype(np.float32, copy=False))
            except Exception:
                self._opus_encode_errors += 1
                return

            self._tx_packets += 1
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
            pkt = pack_audio_packet(self.client_id, ts_ms, self._seq, payload)

            try:
                self._sock.sendto(pkt, self._server_addr)
            except Exception as e:
                self._tx_socket_errors += 1
                logger.debug("udp send failed: {}", e)
                return

            self._tx_udp_sent += 1

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                self._rx_socket_errors += 1
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue

            try:
                pkt = unpack_audio_packet(data)
                seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
                payload = bytes(getattr(pkt, "payload", b""))
            except Exception:
                self._opus_decode_errors += 1
                continue

            self._rx_packets += 1

            try:
                self._jb.push(seq, payload)
            except Exception:
                continue

            now = time.monotonic()
            if now - self._last_stats_log >= 5.0:
                self._last_stats_log = now
                try:
                    playback_samples = int(self._jb.buffered_frames) * int(FRAME_SAMPLES)
                except Exception:
                    playback_samples = 0
                try:
                    jb_stats = self._jb.stats
                    jb_buf = int(self._jb.buffered_frames)
                except Exception:
                    jb_stats = None
                    jb_buf = 0
                logger.debug(
                    "stats: tx_pkts={} rx_pkts={} enc_err={} dec_err={} tx_sock_err={} rx_sock_err={} playback_samples={} jb_buf={} jb_missing={} jb_late_dropped={} jb_concealed={} playout_underflows={}",
                    self._tx_packets,
                    self._rx_packets,
                    self._opus_encode_errors,
                    self._opus_decode_errors,
                    self._tx_socket_errors,
                    self._rx_socket_errors,
                    playback_samples,
                    jb_buf,
                    0 if jb_stats is None else int(jb_stats.missing),
                    0 if jb_stats is None else int(jb_stats.late_dropped),
                    0 if jb_stats is None else int(jb_stats.concealed),
                    int(self._playout_underflows),
                )

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            logger.debug("audio out status: {}", status)

        if self._stop.is_set():
            outdata[:] = 0
            return

        with self._state_lock:
            out_sr = int(self._out_samplerate)
            out_gain_db = float(self._output_gain_db)
            sidetone_enabled = bool(self._sidetone_enabled)
            sidetone_gain_db = float(self._sidetone_gain_db)

        if out_sr != int(SAMPLE_RATE):
            outdata[:] = 0
            return

        y_net = np.zeros((int(frames),), dtype=np.float32)
        off = 0
        while off < int(frames):
            need = int(min(int(FRAME_SAMPLES), int(frames) - off))
            try:
                p = self._jb.pop()
            except Exception:
                p = None

            if p is None:
                self._playout_underflows += 1
                chunk = np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)
            else:
                try:
                    chunk = self._dec.decode(p)
                except Exception:
                    self._opus_decode_errors += 1
                    chunk = np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)

            y_net[off : off + need] = chunk[:need]
            off += need

        y_side = np.zeros((int(frames),), dtype=np.float32)
        if sidetone_enabled:
            try:
                f_side = self._sidetone_frames.popleft() if len(self._sidetone_frames) > 0 else None
            except Exception:
                f_side = None
            if f_side is not None and int(getattr(f_side, "shape", [0])[0]) >= int(frames):
                y_side = apply_gain_db(f_side[: int(frames)].astype(np.float32, copy=False), sidetone_gain_db)

        y = y_net + y_side
        y = apply_gain_db(y.astype(np.float32, copy=False), out_gain_db)
        y = np.clip(y, -1.0, 1.0)

        with self._state_lock:
            self._out_vu_dbfs = rms_dbfs(y)

        y = y.astype(np.float32, copy=False)
        if outdata.ndim == 1:
            outdata[:] = y
        elif outdata.shape[1] == 1:
            outdata[:] = y.reshape(int(frames), 1)
        else:
            outdata[:] = np.repeat(y.reshape(int(frames), 1), repeats=outdata.shape[1], axis=1)
