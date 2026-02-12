from __future__ import annotations

import json
import socket
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np
from loguru import logger

from ..common.audio import int16_bytes_to_float32
from ..common.constants import CONTROL_PORT_OFFSET, FRAME_SAMPLES, SAMPLE_RATE
from ..common.jitter_buffer import OpusPacketJitterBuffer
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import pack_audio_packet, unpack_audio_packet


@dataclass
class BridgeConfig:
    server_ip: str
    server_port: int
    name: str
    mode: str = "ptt"


class IntercomBridge:
    @staticmethod
    def client_id_from_uuid(client_uuid: str) -> int:
        try:
            return int(zlib.crc32(str(client_uuid).encode("utf-8")) & 0xFFFFFFFF)
        except Exception:
            return 0

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
        self.client_id = int(self.client_id_from_uuid(self.client_uuid))
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
        self._muted = False
        self._ptt_general = False
        self._ptt_buses: dict[int, bool] = {}
        self._mute_buses: dict[int, bool] = {}

    def _can_transmit_audio(self) -> bool:
        with self._state_lock:
            if bool(self._muted):
                return False
            mode = str(self.config.mode or "always_on")
            ptt_general = bool(self._ptt_general)
            ptt_buses = dict(self._ptt_buses)
        if mode != "ptt":
            return True
        if ptt_general:
            return True
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

    def _send_udp_frame_f32(self, frame_f32: np.ndarray) -> None:
        if frame_f32.shape[0] != int(FRAME_SAMPLES):
            if frame_f32.shape[0] < int(FRAME_SAMPLES):
                frame_f32 = np.pad(frame_f32, (0, int(FRAME_SAMPLES) - int(frame_f32.shape[0])))
            else:
                frame_f32 = frame_f32[: int(FRAME_SAMPLES)]

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

        if not self._can_transmit_audio():
            return

        try:
            frame = int16_bytes_to_float32(pcm_i16_bytes)
        except Exception:
            return

        if frame.shape[0] != int(FRAME_SAMPLES):
            if frame.shape[0] < int(FRAME_SAMPLES):
                frame = np.pad(frame, (0, int(FRAME_SAMPLES) - int(frame.shape[0])))
            else:
                frame = frame[: int(FRAME_SAMPLES)]

        try:
            self._send_udp_frame_f32(frame)
        except Exception:
            return

    def set_state(self, *, muted: Optional[bool] = None, ptt_general: Optional[bool] = None) -> None:
        with self._state_lock:
            if muted is not None:
                self._muted = bool(muted)
            if ptt_general is not None:
                self._ptt_general = bool(ptt_general)
        self._control_send_state(muted=muted)

    def _control_send(self, sock: socket.socket, msg: dict[str, Any]) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._ctrl_send_lock:
            sock.sendall(data)

    def _control_send_state(self, *, muted: Optional[bool] = None) -> None:
        sock = self._ctrl_sock
        if sock is None or not self._ctrl_connected:
            return

        with self._state_lock:
            msg: dict[str, Any] = {
                "type": "state",
                "client_id": int(self.client_id),
                "ptt_general": bool(self._ptt_general),
                "ptt_buses": dict(self._ptt_buses),
                "mute_buses": dict(self._mute_buses),
            }
            if muted is not None:
                msg["muted"] = bool(muted)

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
                if "muted" in cfg:
                    with self._state_lock:
                        self._muted = bool(cfg.get("muted"))

        if self._on_control_msg is not None:
            try:
                self._on_control_msg(msg)
            except Exception:
                pass

    def _control_loop(self) -> None:
        backoff_s = 1.0
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect((self.config.server_ip, int(self.config.server_port) + int(CONTROL_PORT_OFFSET)))
                sock.settimeout(0.5)

                self._ctrl_sock = sock
                self._ctrl_connected = True

                hello = {
                    "type": "hello",
                    "version": 1,
                    "client_id": int(self.client_id),
                    "client_uuid": str(self.client_uuid),
                    "name": str(self.config.name or ""),
                    "mode": str(self.config.mode or "ptt"),
                }
                self._control_send(sock, hello)
                self._control_send_state(muted=bool(self._muted))

                buf = b""
                last_ping = 0.0

                while not self._stop.is_set() and not self._kick_pending:
                    now = time.monotonic()
                    if now - last_ping >= 2.0:
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
                continue

            try:
                frame = self._dec.decode(payload)
            except Exception:
                continue

            try:
                self._on_audio_frame(frame)
            except Exception:
                pass
