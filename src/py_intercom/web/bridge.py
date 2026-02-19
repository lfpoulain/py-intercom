from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np
from loguru import logger

from ..common.audio import int16_bytes_to_float32
from ..common.identity import client_id_from_uuid
from ..common.constants import CONTROL_PORT_OFFSET, FRAME_SAMPLES, SAMPLE_RATE
from ..common.jitter_buffer import OpusPacketJitterBuffer
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import pack_audio_packet, unpack_audio_packet


@dataclass
class BridgeConfig:
    server_ip: str
    server_port: int
    name: str
    listen_return_bus: bool = False
    listen_regie: bool = True


class IntercomBridge:
    def __init__(
        self,
        *,
        client_uuid: str,
        config: BridgeConfig,
        on_audio_frame: Callable[[np.ndarray], None],
        on_control_msg: Optional[Callable[[dict[str, Any]], None]] = None,
        on_kick: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.client_uuid = str(client_uuid)
        self.client_id = int(client_id_from_uuid(self.client_uuid))
        self.config = config

        self._enc = OpusEncoder()
        self._dec = OpusDecoder()
        self._jb = OpusPacketJitterBuffer(start_frames=3, max_frames=60)

        self._on_audio_frame = on_audio_frame
        self._on_control_msg = on_control_msg
        self._on_kick = on_kick

        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        except Exception:
            pass
        self._udp_sock.bind(("0.0.0.0", 0))
        self._udp_sock.settimeout(0.5)
        self._server_addr = (str(self.config.server_ip), int(self.config.server_port))

        self._seq = 0
        self._stop = threading.Event()
        self._kick_pending = False

        self._rx_thread: Optional[threading.Thread] = None
        self._playout_thread: Optional[threading.Thread] = None

        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_thread: Optional[threading.Thread] = None
        self._ctrl_send_lock = threading.Lock()
        self._ctrl_connected = False

        self._state_lock = threading.Lock()
        self._ptt_buses: dict[int, bool] = {}
        self._known_bus_ids: set[int] = {0, 1, 2}
        self._listen_return_bus = bool(self.config.listen_return_bus)
        self._listen_regie = bool(self.config.listen_regie)

    def _can_transmit_audio(self) -> bool:
        with self._state_lock:
            ptt_buses = dict(self._ptt_buses)
        return any(bool(v) for v in ptt_buses.values())

    def start(self) -> None:
        if self._rx_thread is not None and self._rx_thread.is_alive():
            return

        self._stop.clear()
        self._kick_pending = False

        self._rx_thread = threading.Thread(target=self._rx_loop, name=f"web-bridge-udp-rx-{self.client_id}", daemon=True)
        self._rx_thread.start()

        self._playout_thread = threading.Thread(target=self._playout_loop, name=f"web-bridge-playout-{self.client_id}", daemon=True)
        self._playout_thread.start()

        self._ctrl_thread = threading.Thread(target=self._control_loop, name=f"web-bridge-ctrl-{self.client_id}", daemon=True)
        self._ctrl_thread.start()

        self._send_silence_probe()

    def stop(self) -> None:
        self._stop.set()

        try:
            if self._ctrl_sock is not None:
                self._ctrl_sock.close()
        except Exception:
            pass
        self._ctrl_sock = None

        try:
            if self._udp_sock is not None:
                self._udp_sock.close()
        except Exception:
            pass

        for t in (self._ctrl_thread, self._rx_thread, self._playout_thread):
            if t is not None and t.is_alive():
                try:
                    t.join(timeout=2.0)
                except Exception:
                    pass
        self._ctrl_thread = None
        self._rx_thread = None
        self._playout_thread = None

    def _send_udp_frame_f32(self, frame_f32: np.ndarray) -> None:
        if frame_f32.shape[0] != int(FRAME_SAMPLES):
            return

        payload = self._enc.encode(frame_f32.astype(np.float32, copy=False))

        self._seq = (self._seq + 1) & 0xFFFFFFFF
        ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
        pkt = pack_audio_packet(self.client_id, ts_ms, self._seq, payload)
        self._udp_sock.sendto(pkt, self._server_addr)

    def _send_silence_probe(self) -> None:
        try:
            silence = np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)
            self._send_udp_frame_f32(silence)
        except Exception:
            pass

    def handle_audio_in_int16(self, pcm_i16_bytes: bytes) -> None:
        if self._stop.is_set():
            return

        try:
            frame = int16_bytes_to_float32(pcm_i16_bytes)
        except Exception:
            return

        if frame.shape[0] != int(FRAME_SAMPLES):
            return

        try:
            self._send_udp_frame_f32(frame)
        except Exception:
            return

    def set_ptt_bus(self, bus_id: int, active: bool) -> None:
        try:
            bid = int(bus_id)
        except Exception:
            return
        with self._state_lock:
            self._ptt_buses[bid] = bool(active)
        self._control_send_state()

    def set_listen_return_bus(self, enabled: bool) -> None:
        with self._state_lock:
            self._listen_return_bus = bool(enabled)
        self._control_send_state()

    def set_listen_regie(self, enabled: bool) -> None:
        with self._state_lock:
            self._listen_regie = bool(enabled)
        self._control_send_state()

    def _control_send(self, sock: socket.socket, msg: dict[str, Any]) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._ctrl_send_lock:
            sock.sendall(data)

    def _control_send_state(self) -> None:
        sock = self._ctrl_sock
        if sock is None or not self._ctrl_connected:
            return

        with self._state_lock:
            msg: dict[str, Any] = {
                "type": "state",
                "client_id": int(self.client_id),
                "ptt_buses": dict(self._ptt_buses),
                "listen_return_bus": bool(self._listen_return_bus),
                "listen_regie": bool(self._listen_regie),
            }

        try:
            self._control_send(sock, msg)
        except Exception:
            return

    def _control_handle_msg(self, msg: dict[str, Any]) -> None:
        mtype = str(msg.get("type") or "").lower()
        if mtype == "kick":
            self._kick_pending = True
            if self._on_kick is not None:
                try:
                    self._on_kick(str(msg.get("message") or ""))
                except Exception:
                    pass
            return

        if mtype in ("welcome", "update"):
            cfg = msg.get("config") if isinstance(msg.get("config"), dict) else None
            if cfg is None and mtype == "update":
                cfg = msg
            if isinstance(cfg, dict):
                if "buses" in cfg:
                    buses = cfg.get("buses")
                    parsed_bus_ids: set[int] = set()
                    if isinstance(buses, list):
                        for b in buses:
                            if not isinstance(b, dict):
                                continue
                            try:
                                parsed_bus_ids.add(int(b.get("bus_id")))
                            except Exception:
                                continue
                    elif isinstance(buses, dict):
                        for k, b in buses.items():
                            if isinstance(b, dict) and "bus_id" in b:
                                try:
                                    parsed_bus_ids.add(int(b.get("bus_id")))
                                    continue
                                except Exception:
                                    pass
                            try:
                                parsed_bus_ids.add(int(k))
                            except Exception:
                                continue
                    if len(parsed_bus_ids) == 0:
                        parsed_bus_ids = {0}
                    with self._state_lock:
                        self._known_bus_ids = set(parsed_bus_ids)
                        for bid in parsed_bus_ids:
                            self._ptt_buses.setdefault(int(bid), False)

        if self._on_control_msg is not None:
            try:
                self._on_control_msg(msg)
            except Exception:
                pass

    def _control_loop(self) -> None:
        backoff_s = 1.0
        liveness_timeout_s = 6.0
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect((self.config.server_ip, int(self.config.server_port) + int(CONTROL_PORT_OFFSET)))
                sock.settimeout(0.5)

                self._ctrl_sock = sock
                self._ctrl_connected = True
                backoff_s = 1.0

                hello = {
                    "type": "hello",
                    "version": 1,
                    "client_id": int(self.client_id),
                    "client_uuid": str(self.client_uuid),
                    "name": str(self.config.name or ""),
                    "udp_port": int(self._udp_sock.getsockname()[1]),
                }
                self._control_send(sock, hello)
                self._control_send_state()

                buf = b""
                last_ping = 0.0
                ping_interval_s = 0.05
                last_rx = time.monotonic()

                while not self._stop.is_set() and not self._kick_pending:
                    now = time.monotonic()
                    if now - float(last_rx) >= float(liveness_timeout_s):
                        logger.debug("web bridge control liveness timeout (no rx for {:.1f}s)", float(liveness_timeout_s))
                        break
                    if now - last_ping >= float(ping_interval_s):
                        try:
                            self._control_send(sock, {"type": "ping", "t": int(time.time() * 1000)})
                            last_ping = now
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
                            self._control_handle_msg(msg)
                            last_rx = time.monotonic()

            except Exception as e:
                logger.debug("web bridge control loop error: {}", e)
            finally:
                self._ctrl_connected = False
                try:
                    if self._ctrl_sock is not None:
                        self._ctrl_sock.close()
                except Exception:
                    pass
                self._ctrl_sock = None

            if self._stop.is_set() or self._kick_pending:
                break

            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2.0, 10.0)

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._udp_sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                pkt = unpack_audio_packet(data)
                seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
                payload = bytes(getattr(pkt, "payload", b""))
            except Exception:
                continue

            try:
                self._jb.push(seq, payload)
            except Exception:
                continue

    def _playout_loop(self) -> None:
        tick_s = float(FRAME_SAMPLES) / float(SAMPLE_RATE)
        next_t = time.monotonic()
        _silence = np.zeros(int(FRAME_SAMPLES), dtype=np.float32)
        _consecutive_silence = 0
        _SILENCE_GATE = 8  # stop sending after 8 consecutive silent frames (~80ms)
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(0.002, next_t - now))
                continue

            next_t += tick_s
            if next_t < now - 0.1:
                next_t = now

            try:
                payload = self._jb.pop()
            except Exception:
                payload = None

            if payload is None:
                _consecutive_silence += 1
                if _consecutive_silence <= _SILENCE_GATE:
                    # Send silence to keep the JS queue fed and avoid underrun clicks
                    try:
                        self._on_audio_frame(_silence)
                    except Exception:
                        pass
                continue

            _consecutive_silence = 0
            try:
                frame = self._dec.decode(payload)
            except Exception:
                continue

            try:
                self._on_audio_frame(frame)
            except Exception:
                pass
