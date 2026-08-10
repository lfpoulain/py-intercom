from __future__ import annotations
import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio_errors import (
    friendly_audio_open_error,
    shared_stream_extra_settings,
)
from ..common.buses import FIXED_BUS_IDS
from ..common.constants import AUDIO_UDP_PORT, CONTROL_PORT_OFFSET, CTRL_LIVENESS_TIMEOUT_S, CTRL_PING_INTERVAL_S, FRAME_SAMPLES, JB_MAX_FRAMES, JB_START_FRAMES, MAX_GAIN_DB, SAMPLE_RATE
from ..common.control_protocol import (
    CONTROL_PROTOCOL_VERSION,
    ControlProtocolError,
    JsonLineBuffer,
    encode_control_message,
    parse_session_token,
    require_control_protocol_version,
)
from ..common.frame_ring import FrameRingBuffer
from ..common.jitter_buffer import JitterFrameMetadata, OpusPacketJitterBuffer
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import (
    AudioPacketDirection,
    AudioPacketValidationError,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)


_FIXED_BUS_MASK = sum(1 << int(bus_id) for bus_id in FIXED_BUS_IDS if 0 <= int(bus_id) < 16)
_TX_RING_CAPACITY = 8
_PLAYOUT_RING_CAPACITY = max(8, int(JB_START_FRAMES) * 2)
_TX_METADATA_BUS_BITS = 16
_TX_METADATA_BUS_MASK = (1 << _TX_METADATA_BUS_BITS) - 1
_AUDIO_SESSION_EPOCH_MASK = (1 << (63 - _TX_METADATA_BUS_BITS)) - 1
_PLAYOUT_FRAME_PERIOD_S = float(FRAME_SAMPLES) / float(SAMPLE_RATE)


@dataclass
class ClientConfig:
    server_ip: str
    server_port: int = AUDIO_UDP_PORT
    control_port: Optional[int] = None

    name: str = ""
    client_uuid: str = ""
    input_device: Optional[int] = None
    output_device: Optional[int] = None
    input_gain_db: float = 0.0
    output_gain_db: float = 0.0
    return_gain_db: float = 0.0

    ptt_bus_keys: Optional[dict] = None

    listen_return_bus: bool = False
    listen_regie: bool = True


class IntercomClient:
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
        self._expected_server_udp_ips: frozenset[str] = frozenset()
        self._refresh_expected_server_udp_sources()
        self._sock.settimeout(0.5)

        self._enc = OpusEncoder()
        self._dec = OpusDecoder()

        self._jb = OpusPacketJitterBuffer(start_frames=JB_START_FRAMES, max_frames=JB_MAX_FRAMES)

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
        # Only the TX worker may touch the stateful encoder, sequence number or
        # UDP send path. This lock publishes and invalidates audio sessions; it
        # is never held while encoding.
        self._tx_lock = threading.Lock()
        self._audio_session_epoch = 1

        self._tx_ring = FrameRingBuffer(
            capacity=_TX_RING_CAPACITY,
            frame_samples=int(FRAME_SAMPLES),
        )
        self._playout_ring = FrameRingBuffer(
            capacity=_PLAYOUT_RING_CAPACITY,
            frame_samples=int(FRAME_SAMPLES),
        )
        self._capture_work = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
        self._capture_abs_work = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
        self._output_work = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
        self._silence_frame = np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)

        self._network_lifecycle_lock = threading.RLock()
        self._network_state_lock = threading.Lock()
        self._network_generation = 0
        self._fully_stopped = False
        self._stop = threading.Event()
        # A newly constructed client has no live network generation yet.
        self._stop.set()
        self._udp_ready = threading.Event()

        self._kicked: bool = False
        self._kick_message: str = ""

        self._state_lock = threading.Lock()
        self._input_gain_db = max(
            -60.0,
            min(float(MAX_GAIN_DB), float(self.config.input_gain_db)),
        )
        self._output_gain_db = max(
            -60.0,
            min(float(MAX_GAIN_DB), float(self.config.output_gain_db)),
        )
        self._return_gain_db = float(self.config.return_gain_db)
        self._in_vu_dbfs = -60.0
        self._out_vu_dbfs = -60.0
        self._return_vu_dbfs = -60.0
        self._bus_vu_dbfs: dict[int, float] = {
            int(bus_id): -60.0 for bus_id in FIXED_BUS_IDS
        }

        self._in_channels = 1
        self._out_channels = 1

        self._in_samplerate = SAMPLE_RATE
        self._out_samplerate = SAMPLE_RATE

        self._tx_packets = 0
        self._tx_udp_sent = 0
        self._rx_packets = 0
        self._opus_encode_errors = 0
        self._opus_decode_errors = 0
        self._tx_socket_errors = 0
        self._rx_socket_errors = 0
        self._rx_source_dropped = 0
        self._rx_not_ready_dropped = 0
        self._rx_invalid_packets = 0
        self._rx_invalid_tokens = 0
        self._rx_invalid_directions = 0
        self._rx_invalid_route_masks = 0
        self._last_udp_rx_monotonic = 0.0
        self._playout_underflows = 0
        self._input_callback_statuses = 0
        self._output_callback_statuses = 0
        self._input_frame_mismatches = 0
        self._output_frame_mismatches = 0
        self._tx_worker_failures = 0
        self._playout_worker_failures = 0
        self._opus_encoder_resets = 0
        self._opus_decoder_resets = 0
        self._opus_encoder_reset_errors = 0
        self._opus_decoder_reset_errors = 0
        self._last_stats_log = time.monotonic()

        self._in_stream: Optional[sd.InputStream] = None
        self._out_stream: Optional[sd.OutputStream] = None

        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None
        self._playout_thread: Optional[threading.Thread] = None

        self._ctrl_sock: Optional[socket.socket] = None
        self._ctrl_thread: Optional[threading.Thread] = None
        self._ctrl_send_lock = threading.Lock()
        self._ctrl_connected: bool = False
        self._ctrl_last_rx_monotonic: float = 0.0
        self._ctrl_last_tx_monotonic: float = 0.0
        self._ctrl_buses: dict[int, dict] = {}
        self._ctrl_received_welcome: bool = False
        self._ctrl_received_config: bool = False
        self._session_token: int = 0

        self._ptt_buses: dict[int, bool] = {}
        self._listen_return_bus: bool = bool(self.config.listen_return_bus)
        self._listen_regie: bool = bool(self.config.listen_regie)

    def get_stats_snapshot(self) -> dict:
        with self._state_lock:
            tx_ring_stats = self._tx_ring.stats
            playout_ring_stats = self._playout_ring.stats
            capture_samples = int(tx_ring_stats.depth) * int(FRAME_SAMPLES)
            playback_samples = int(playout_ring_stats.depth) * int(FRAME_SAMPLES)
            try:
                jitter_buffered_frames = int(self._jb.buffered_frames)
            except Exception:
                jitter_buffered_frames = 0

            ctrl_age_s = (
                max(0.0, time.monotonic() - float(self._ctrl_last_rx_monotonic)) if self._ctrl_connected else None
            )
            ctrl_tx_age_s = (
                max(0.0, time.monotonic() - float(self._ctrl_last_tx_monotonic)) if self._ctrl_connected else None
            )
            udp_rx_age_s = (
                max(0.0, time.monotonic() - float(self._last_udp_rx_monotonic))
                if float(self._last_udp_rx_monotonic) > 0.0
                else None
            )
            control_worker_alive = bool(
                self._ctrl_thread is not None and self._ctrl_thread.is_alive()
            )
            rx_worker_alive = bool(
                self._rx_thread is not None and self._rx_thread.is_alive()
            )
            tx_worker_alive = bool(
                self._tx_thread is not None and self._tx_thread.is_alive()
            )
            playout_worker_alive = bool(
                self._playout_thread is not None and self._playout_thread.is_alive()
            )
            network_workers_alive = bool(
                control_worker_alive
                and rx_worker_alive
                and tx_worker_alive
                and playout_worker_alive
            )
            try:
                in_stream_active = bool(self._in_stream is not None and self._in_stream.active)
            except Exception:
                in_stream_active = False
            try:
                out_stream_active = bool(self._out_stream is not None and self._out_stream.active)
            except Exception:
                out_stream_active = False

            return {
                "input_gain_db": self._input_gain_db,
                "output_gain_db": self._output_gain_db,
                "return_gain_db": self._return_gain_db,
                "in_vu_dbfs": self._in_vu_dbfs,
                "out_vu_dbfs": self._out_vu_dbfs,
                "return_vu_dbfs": self._return_vu_dbfs,
                "in_channels": self._in_channels,
                "out_channels": self._out_channels,
                "in_samplerate": self._in_samplerate,
                "out_samplerate": self._out_samplerate,
                "capture_samples": int(capture_samples),
                "playback_samples": int(playback_samples),
                "jitter_buffered_frames": int(jitter_buffered_frames),
                "tx_ring_depth": int(tx_ring_stats.depth),
                "tx_ring_pushed": int(tx_ring_stats.pushed),
                "tx_ring_popped": int(tx_ring_stats.popped),
                "tx_ring_dropped": int(tx_ring_stats.dropped),
                "tx_ring_underflows": int(tx_ring_stats.underflows),
                "playout_ring_depth": int(playout_ring_stats.depth),
                "playout_ring_pushed": int(playout_ring_stats.pushed),
                "playout_ring_popped": int(playout_ring_stats.popped),
                "playout_ring_dropped": int(playout_ring_stats.dropped),
                "playout_ring_underflows": int(playout_ring_stats.underflows),
                "tx_packets": int(self._tx_packets),
                "tx_udp_sent": int(self._tx_udp_sent),
                "rx_packets": int(self._rx_packets),
                "opus_encode_errors": int(self._opus_encode_errors),
                "opus_decode_errors": int(self._opus_decode_errors),
                "tx_socket_errors": int(self._tx_socket_errors),
                "rx_socket_errors": int(self._rx_socket_errors),
                "rx_source_dropped": int(self._rx_source_dropped),
                "rx_not_ready_dropped": int(self._rx_not_ready_dropped),
                "rx_invalid_packets": int(self._rx_invalid_packets),
                "rx_invalid_tokens": int(self._rx_invalid_tokens),
                "rx_invalid_directions": int(self._rx_invalid_directions),
                "rx_invalid_route_masks": int(self._rx_invalid_route_masks),
                "udp_rx_age_s": udp_rx_age_s,
                "playout_underflows": int(self._playout_underflows),
                "input_callback_statuses": int(self._input_callback_statuses),
                "output_callback_statuses": int(self._output_callback_statuses),
                "input_frame_mismatches": int(self._input_frame_mismatches),
                "output_frame_mismatches": int(self._output_frame_mismatches),
                "tx_worker_failures": int(self._tx_worker_failures),
                "playout_worker_failures": int(self._playout_worker_failures),
                "opus_encoder_resets": int(self._opus_encoder_resets),
                "opus_decoder_resets": int(self._opus_decoder_resets),
                "opus_encoder_reset_errors": int(self._opus_encoder_reset_errors),
                "opus_decoder_reset_errors": int(self._opus_decoder_reset_errors),
                "opus_ok": bool(self._opus_ok),
                "opus_err": str(self._opus_err),
                "opuslib_version": str(self._opuslib_version),
                "stopped": bool(self._stop.is_set()),
                "fully_stopped": bool(self._fully_stopped),
                "kicked": bool(self._kicked),
                "kick_message": str(self._kick_message),
                "control_connected": bool(self._ctrl_connected),
                "control_welcome_received": bool(self._ctrl_received_welcome),
                "control_ready": bool(
                    self._ctrl_received_config
                    and int(self._session_token) != 0
                    and self._udp_ready.is_set()
                ),
                "control_age_s": ctrl_age_s,
                "control_tx_age_s": ctrl_tx_age_s,
                "network_workers_alive": network_workers_alive,
                "control_worker_alive": control_worker_alive,
                "rx_worker_alive": rx_worker_alive,
                "tx_worker_alive": tx_worker_alive,
                "playout_worker_alive": playout_worker_alive,
                "in_stream_active": in_stream_active,
                "out_stream_active": out_stream_active,
                "ptt_buses": dict(self._ptt_buses),
                "bus_vu_dbfs": dict(self._bus_vu_dbfs),
                "listen_return_bus": bool(self._listen_return_bus),
                "listen_regie": bool(self._listen_regie),
                "buses": {
                    str(int(bid)): {
                        "bus_id": int(bid),
                        "name": str(b.get("name") or f"Bus {int(bid)}"),
                        "feed_to_regie": bool(b.get("feed_to_regie", False)),
                    }
                    for bid, b in self._ctrl_buses.items()
                    if isinstance(b, dict)
                },
                "name": str(self.config.name or ""),
            }

    def set_ptt_bus(self, bus_id: int, active: bool) -> None:
        with self._state_lock:
            self._ptt_buses[int(bus_id)] = bool(active)
        self._control_send_state()

    def set_listen_return_bus(self, enabled: bool) -> None:
        with self._state_lock:
            self._listen_return_bus = bool(enabled)
        self._control_send_state()

    def set_listen_regie(self, enabled: bool) -> None:
        with self._state_lock:
            self._listen_regie = bool(enabled)
        self._control_send_state()

    def set_input_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._input_gain_db = max(-60.0, min(float(MAX_GAIN_DB), float(gain_db)))
        self._control_send_state()

    def set_output_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._output_gain_db = max(
                -60.0,
                min(float(MAX_GAIN_DB), float(gain_db)),
            )

    def set_return_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._return_gain_db = float(gain_db)
        self._control_send_state()

    @staticmethod
    def _ptt_bus_mask(ptt_buses: dict[int, bool]) -> int:
        mask = 0
        for bus_id in FIXED_BUS_IDS:
            bid = int(bus_id)
            if 0 <= bid < 16 and bool(ptt_buses.get(bid, False)):
                mask |= 1 << bid
        return int(mask)

    @staticmethod
    def _pack_tx_metadata(session_epoch: int, bus_mask: int) -> int:
        epoch = int(session_epoch) & int(_AUDIO_SESSION_EPOCH_MASK)
        routing_mask = int(bus_mask) & int(_TX_METADATA_BUS_MASK)
        return int((epoch << _TX_METADATA_BUS_BITS) | routing_mask)

    @staticmethod
    def _unpack_tx_metadata(metadata: int) -> tuple[int, int]:
        value = int(metadata)
        return (
            int((value >> _TX_METADATA_BUS_BITS) & _AUDIO_SESSION_EPOCH_MASK),
            int(value & _TX_METADATA_BUS_MASK),
        )

    @staticmethod
    def _rms_dbfs_realtime(frame: np.ndarray, floor_db: float = -60.0) -> float:
        if int(frame.size) == 0:
            return float(floor_db)
        mean_square = float(np.dot(frame, frame)) / float(frame.size)
        if not math.isfinite(mean_square) or mean_square <= 0.0:
            return float(floor_db)
        db = 10.0 * math.log10(mean_square)
        return max(float(floor_db), min(0.0, float(db)))

    def _advance_audio_session_epoch_locked(self) -> int:
        epoch = (int(self._audio_session_epoch) + 1) & int(
            _AUDIO_SESSION_EPOCH_MASK
        )
        self._audio_session_epoch = 1 if epoch == 0 else int(epoch)
        return int(self._audio_session_epoch)

    def _reset_session_token(self) -> None:
        with self._tx_lock:
            self._session_token = 0
            self._advance_audio_session_epoch_locked()
            self._tx_ring.clear()
            self._playout_ring.clear()
            try:
                self._jb.reset()
            except Exception:
                pass

    def _activate_session_token(self, session_token: int) -> None:
        token = int(session_token)
        if token <= 0 or token > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("session_token must be a non-zero uint64")
        with self._tx_lock:
            self._advance_audio_session_epoch_locked()
            self._session_token = token
            self._tx_ring.clear()
            self._playout_ring.clear()
            try:
                self._jb.reset()
            except Exception:
                pass

    def _count_invalid_audio_packet(self, reason: str) -> None:
        if reason == "invalid_token":
            self._rx_invalid_tokens += 1
        elif reason == "invalid_direction":
            self._rx_invalid_directions += 1
        elif reason == "invalid_route":
            self._rx_invalid_route_masks += 1
        else:
            self._rx_invalid_packets += 1

    def _refresh_expected_server_udp_sources(self) -> None:
        """Resolve the configured server once for strict UDP source checks."""
        host = str(self.config.server_ip or "").strip()
        port = int(self.config.server_port)
        resolved: set[str] = set()
        if host:
            resolved.add(host)
        try:
            for info in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM):
                sockaddr = info[4]
                if isinstance(sockaddr, tuple) and sockaddr:
                    resolved.add(str(sockaddr[0]))
        except Exception as e:
            logger.debug("could not resolve UDP server {}:{}: {}", host, port, e)
        self._expected_server_udp_ips = frozenset(resolved)

    def _is_expected_udp_source(self, addr: tuple) -> bool:
        try:
            source_ip = str(addr[0])
            source_port = int(addr[1])
        except Exception:
            return False
        return source_port == int(self.config.server_port) and source_ip in self._expected_server_udp_ips

    def _network_is_current(self, stop_event: threading.Event, generation: int) -> bool:
        with self._network_state_lock:
            return (
                int(self._network_generation) == int(generation)
                and self._stop is stop_event
                and not stop_event.is_set()
            )

    def _drain_udp_socket(self) -> int:
        """Discard datagrams left in the kernel queue before a new session."""
        drained = 0
        try:
            previous_timeout = self._sock.gettimeout()
        except Exception:
            previous_timeout = 0.5
        try:
            self._sock.setblocking(False)
            while True:
                try:
                    self._sock.recvfrom(65535)
                    drained += 1
                except (BlockingIOError, socket.timeout):
                    break
                except OSError as e:
                    err_code = getattr(e, "winerror", None) or getattr(e, "errno", None)
                    if err_code in (11, 35, 10035):
                        break
                    logger.debug("UDP drain stopped after {} datagrams: {}", drained, e)
                    break
        finally:
            try:
                self._sock.settimeout(previous_timeout)
            except Exception:
                pass
        if drained:
            logger.debug("discarded {} stale UDP datagrams before reconnect", drained)
        return int(drained)

    def _start_network_threads(self) -> None:
        """Start a fresh, isolated network generation."""
        with self._network_lifecycle_lock:
            if self._fully_stopped:
                raise RuntimeError(
                    "a fully stopped IntercomClient cannot be restarted; create a new instance"
                )
            with self._network_state_lock:
                old_threads = (
                    self._rx_thread,
                    self._ctrl_thread,
                    self._tx_thread,
                    self._playout_thread,
                )
            if any(t is not None and t.is_alive() for t in old_threads):
                raise RuntimeError("previous network workers are still running")

            self._refresh_expected_server_udp_sources()
            self._drain_udp_socket()
            self._reset_session_token()
            self._last_udp_rx_monotonic = 0.0

            stop_event = threading.Event()
            udp_ready = threading.Event()
            with self._network_state_lock:
                self._network_generation += 1
                generation = int(self._network_generation)
                self._stop = stop_event
                self._udp_ready = udp_ready
                self._ctrl_connected = False
                self._ctrl_received_welcome = False
                self._ctrl_received_config = False
                rx_thread = threading.Thread(
                    target=self._rx_loop,
                    args=(stop_event, generation, udp_ready),
                    name=f"udp-rx-{generation}",
                    daemon=True,
                )
                ctrl_thread = threading.Thread(
                    target=self._control_loop,
                    args=(stop_event, generation, udp_ready),
                    name=f"ctrl-{generation}",
                    daemon=True,
                )
                tx_thread = threading.Thread(
                    target=self._tx_loop,
                    args=(stop_event, generation, udp_ready),
                    name=f"audio-tx-{generation}",
                    daemon=True,
                )
                playout_thread = threading.Thread(
                    target=self._playout_loop,
                    args=(stop_event, generation, udp_ready),
                    name=f"audio-playout-{generation}",
                    daemon=True,
                )
                self._rx_thread = rx_thread
                self._ctrl_thread = ctrl_thread
                self._tx_thread = tx_thread
                self._playout_thread = playout_thread

            try:
                for thread in (tx_thread, playout_thread, rx_thread, ctrl_thread):
                    thread.start()
            except Exception:
                stop_event.set()
                udp_ready.clear()
                self._reset_session_token()
                for thread in (ctrl_thread, rx_thread, tx_thread, playout_thread):
                    if thread.is_alive() and thread is not threading.current_thread():
                        thread.join(timeout=2.0)
                with self._network_state_lock:
                    if self._rx_thread is rx_thread:
                        self._rx_thread = None
                    if self._ctrl_thread is ctrl_thread:
                        self._ctrl_thread = None
                    if self._tx_thread is tx_thread:
                        self._tx_thread = None
                    if self._playout_thread is playout_thread:
                        self._playout_thread = None
                raise

    def start(self) -> None:
        if self._fully_stopped:
            raise RuntimeError(
                "a fully stopped IntercomClient cannot be restarted; create a new instance"
            )
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

        self._start_network_threads()

        try:
            # Opening the microphone first is significant for Bluetooth
            # headsets: Windows establishes the bidirectional HFP profile when
            # capture starts.  Playback is opened only after that transition,
            # so it cannot be an already-running A2DP stream invalidated by the
            # profile switch.
            self._in_stream = self._open_input_stream()
            self._start_audio_stream(
                self._in_stream,
                kind="input",
                device=self.config.input_device,
            )

            self._out_stream = self._open_output_stream()
            self._start_audio_stream(
                self._out_stream,
                kind="output",
                device=self.config.output_device,
            )
        except Exception:
            # Full rollback: no half-open capture/playback stream and no
            # network generation may survive a failed audio transaction.
            try:
                self.stop()
            except Exception:
                pass
            raise

    @staticmethod
    def _query_audio_device(device: Optional[int]) -> Optional[dict]:
        if device is None:
            return None
        try:
            info = sd.query_devices(int(device))
        except Exception:
            return None
        return info if isinstance(info, dict) else None

    def _start_audio_stream(self, stream, *, kind: str, device: Optional[int]) -> None:
        try:
            stream.start()
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind=kind,
                dev_info=self._query_audio_device(device),
                device=device,
                sr=int(SAMPLE_RATE),
                ch=1,
            )
            raise RuntimeError(msg) from e

    @staticmethod
    def _close_audio_stream(stream) -> None:
        if stream is None:
            return
        # PortAudio backends differ in which operations remain valid after a
        # failed start.  Each step is isolated so a stop/abort failure can
        # never prevent the final close.
        for operation in ("abort", "stop", "close"):
            method = getattr(stream, operation, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception as e:
                logger.debug("audio stream {} during cleanup failed: {}", operation, e)

    def _stop_network(self) -> bool:
        """Tear down control TCP and network threads. UDP socket is kept alive."""
        with self._network_lifecycle_lock:
            with self._network_state_lock:
                generation = int(self._network_generation)
                stop_event = self._stop
                udp_ready = self._udp_ready
                ctrl_sock = self._ctrl_sock
                ctrl_thread = self._ctrl_thread
                rx_thread = self._rx_thread
                tx_thread = self._tx_thread
                playout_thread = self._playout_thread
                stop_event.set()
                udp_ready.clear()

            self._reset_session_token()

            # Closing/shutting down TCP wakes recv immediately.  The UDP socket
            # intentionally remains open so reconnect reuses its local port.
            if ctrl_sock is not None:
                try:
                    ctrl_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    ctrl_sock.close()
                except Exception:
                    pass

            deadline = time.monotonic() + 3.0
            for thread in (ctrl_thread, rx_thread, tx_thread, playout_thread):
                if thread is None or thread is threading.current_thread() or not thread.is_alive():
                    continue
                try:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
                except Exception:
                    pass

            alive = [
                thread.name
                for thread in (ctrl_thread, rx_thread, tx_thread, playout_thread)
                if thread is not None and thread.is_alive()
            ]
            with self._network_state_lock:
                if int(self._network_generation) == generation:
                    self._ctrl_connected = False
                    self._ctrl_received_welcome = False
                    self._ctrl_received_config = False
                    if self._ctrl_sock is ctrl_sock:
                        self._ctrl_sock = None
                    if self._ctrl_thread is ctrl_thread and (ctrl_thread is None or not ctrl_thread.is_alive()):
                        self._ctrl_thread = None
                    if self._rx_thread is rx_thread and (rx_thread is None or not rx_thread.is_alive()):
                        self._rx_thread = None
                    if self._tx_thread is tx_thread and (tx_thread is None or not tx_thread.is_alive()):
                        self._tx_thread = None
                    if self._playout_thread is playout_thread and (
                        playout_thread is None or not playout_thread.is_alive()
                    ):
                        self._playout_thread = None

            if alive:
                logger.error("network workers did not stop within deadline: {}", ", ".join(alive))
                return False
            return True

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
        with self._network_lifecycle_lock:
            if self._fully_stopped:
                raise RuntimeError(
                    "a fully stopped IntercomClient cannot be reconnected; create a new instance"
                )
            if not self._stop_network():
                raise RuntimeError("cannot reconnect while previous network workers are still running")

            # Reuse the UDP socket and its ephemeral port, but never its queued
            # packets or the state of the previous playout session.
            self._server_addr = (self.config.server_ip, int(self.config.server_port))
            self._tx_ring.clear()
            self._playout_ring.clear()
            try:
                self._jb.reset()
            except Exception:
                pass

            self._kicked = False
            self._kick_message = ""
            self._ctrl_buses = {}
            self._tx_packets = 0
            self._tx_udp_sent = 0
            self._rx_packets = 0
            self._tx_socket_errors = 0
            self._rx_socket_errors = 0
            self._rx_source_dropped = 0
            self._rx_not_ready_dropped = 0
            self._rx_invalid_packets = 0
            self._rx_invalid_tokens = 0
            self._rx_invalid_directions = 0
            self._rx_invalid_route_masks = 0
            self._last_udp_rx_monotonic = 0.0

            self._start_network_threads()

        logger.info("network reconnected to {}:{}", self.config.server_ip, self.config.server_port)

    def stop(self) -> None:
        """Full stop: network + audio streams + UDP socket."""
        if self._fully_stopped:
            return
        try:
            self._stop_network()
        finally:
            # Audio cleanup must not depend on a network worker shutting down
            # cleanly.  This is also the rollback path for a failed stream
            # start, so it must leave no PortAudio handle behind.
            try:
                self._sock.close()
            except Exception:
                pass

            # Reverse startup order.  In particular, keep Bluetooth
            # capture/HFP active until its matching playback endpoint has
            # been closed.
            out_stream, self._out_stream = self._out_stream, None
            in_stream, self._in_stream = self._in_stream, None
            self._close_audio_stream(out_stream)
            self._close_audio_stream(in_stream)
            self._fully_stopped = True

    def _control_send(self, sock: socket.socket, msg: dict) -> None:
        data = encode_control_message(msg)
        with self._ctrl_send_lock:
            sock.sendall(data)

    def _control_send_state(self) -> None:
        sock = self._ctrl_sock
        if sock is None or not self._ctrl_connected:
            return
        with self._state_lock:
            input_gain_db = float(self._input_gain_db)
            ptt_buses = dict(self._ptt_buses)
            listen_return_bus = bool(self._listen_return_bus)
            listen_regie = bool(self._listen_regie)
            return_gain_db = float(self._return_gain_db)
        msg: dict[str, object] = {
            "type": "state",
            "client_id": int(self.client_id),
            "input_gain_db": input_gain_db,
            "applies_input_gain_locally": True,
            "ptt_buses": ptt_buses,
            "listen_return_bus": listen_return_bus,
            "listen_regie": listen_regie,
            "return_gain_db": return_gain_db,
        }
        try:
            self._control_send(sock, msg)
        except Exception as e:
            logger.debug("control send state failed: {}", e)
            return

    def _send_silence_probe(self) -> None:
        if self._stop.is_set() or not self._udp_ready.is_set():
            return
        with self._tx_lock:
            if self._stop.is_set() or not self._udp_ready.is_set():
                return
            session_token = int(self._session_token)
            session_epoch = int(self._audio_session_epoch)
            if session_token == 0:
                return
            metadata = self._pack_tx_metadata(session_epoch, 0)
            self._tx_ring.push(self._silence_frame, metadata=metadata)

    def _tx_loop(
        self,
        stop_event: threading.Event,
        generation: int,
        udp_ready: threading.Event,
    ) -> None:
        """Own the Opus encoder, sequence and UDP transmission path."""

        frame = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
        encoder: Optional[OpusEncoder] = None
        encoder_epoch: Optional[int] = None
        while self._network_is_current(stop_event, generation):
            metadata = self._tx_ring.wait_pop_into(
                frame,
                stop_event=stop_event,
                timeout=None,
            )
            if metadata is None:
                if stop_event.is_set():
                    return
                continue

            session_epoch, bus_mask = self._unpack_tx_metadata(metadata)
            if bus_mask & ~int(_FIXED_BUS_MASK):
                self._tx_worker_failures += 1
                continue

            with self._tx_lock:
                session_token = int(self._session_token)
                current_epoch = int(self._audio_session_epoch)
            if (
                session_token == 0
                or session_epoch != current_epoch
                or not udp_ready.is_set()
                or not self._network_is_current(stop_event, generation)
            ):
                continue

            if encoder is None or encoder_epoch != session_epoch:
                try:
                    encoder = OpusEncoder()
                except Exception as e:
                    encoder = None
                    encoder_epoch = None
                    self._opus_encoder_reset_errors += 1
                    logger.error(
                        "Opus TX reset failed for audio epoch {}: {}",
                        session_epoch,
                        e,
                    )
                    continue
                self._enc = encoder
                encoder_epoch = int(session_epoch)
                self._opus_encoder_resets += 1

            try:
                payload = encoder.encode(frame)
            except Exception as e:
                self._opus_encode_errors += 1
                logger.debug("Opus TX encode failed: {}", e)
                continue

            # Revalidate immediately before publication. The socket operation
            # stays outside this lock so a slow network can never stall the
            # PortAudio callback while it snapshots the active session.
            with self._tx_lock:
                if (
                    int(self._session_token) != session_token
                    or int(self._audio_session_epoch) != session_epoch
                    or stop_event.is_set()
                    or not udp_ready.is_set()
                ):
                    continue
                self._seq = (int(self._seq) + 1) & 0xFFFFFFFF
                sequence_number = int(self._seq)

            try:
                packet = pack_audio_packet_v2(
                    session_token=session_token,
                    client_id=self.client_id,
                    timestamp_ms=int(time.time() * 1000) & 0xFFFFFFFF,
                    sequence_number=sequence_number,
                    stream_id=0,
                    bus_mask=bus_mask,
                    direction=AudioPacketDirection.CLIENT_TO_SERVER,
                    payload=payload,
                )
            except Exception as e:
                self._tx_worker_failures += 1
                logger.debug("audio packet build failed: {}", e)
                continue
            if bus_mask != 0:
                self._tx_packets += 1
            try:
                self._sock.sendto(packet, self._server_addr)
            except Exception as e:
                self._tx_socket_errors += 1
                logger.debug("udp send failed: {}", e)
                continue
            self._tx_udp_sent += 1

    def _playout_loop(
        self,
        stop_event: threading.Event,
        generation: int,
        udp_ready: threading.Event,
    ) -> None:
        """Own jitter playout and Opus decoding, publishing fixed PCM frames."""

        next_deadline = time.monotonic()
        decoder: Optional[OpusDecoder] = None
        decoder_epoch: Optional[int] = None
        while self._network_is_current(stop_event, generation):
            if not udp_ready.is_set():
                next_deadline = time.monotonic()
                if stop_event.wait(_PLAYOUT_FRAME_PERIOD_S):
                    return
                continue

            now = time.monotonic()
            if now < next_deadline:
                if stop_event.wait(next_deadline - now):
                    return
            elif now - next_deadline > _PLAYOUT_FRAME_PERIOD_S * 4.0:
                next_deadline = now
            next_deadline += _PLAYOUT_FRAME_PERIOD_S

            if (
                not self._network_is_current(stop_event, generation)
                or not udp_ready.is_set()
            ):
                continue

            try:
                jitter_frame = self._jb.pop_frame()
            except Exception as e:
                self._playout_worker_failures += 1
                logger.debug("jitter playout failed: {}", e)
                continue
            if jitter_frame is None:
                continue

            with self._tx_lock:
                session_token = int(self._session_token)
                session_epoch = int(self._audio_session_epoch)
            if session_token == 0:
                continue
            if (
                not bool(jitter_frame.concealed)
                and int(jitter_frame.metadata.session_token) != session_token
            ):
                continue

            if decoder is None or decoder_epoch != session_epoch:
                try:
                    decoder = OpusDecoder()
                except Exception as e:
                    decoder = None
                    decoder_epoch = None
                    self._opus_decoder_reset_errors += 1
                    logger.error(
                        "Opus playout reset failed for audio epoch {}: {}",
                        session_epoch,
                        e,
                    )
                    continue
                self._dec = decoder
                decoder_epoch = int(session_epoch)
                self._opus_decoder_resets += 1

            decoded = self._decode_playout_frame(
                jitter_frame.payload,
                decoder=decoder,
            )

            # Prevent a decode from the previous session from being published
            # after reconnect cleared the PCM ring.
            with self._tx_lock:
                if (
                    int(self._session_token) != session_token
                    or int(self._audio_session_epoch) != session_epoch
                    or stop_event.is_set()
                    or not udp_ready.is_set()
                ):
                    continue
                self._playout_ring.push(
                    decoded,
                    metadata=int(jitter_frame.sequence_number),
                )

    def _control_handle_msg(
        self,
        msg: dict,
        *,
        generation: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
        udp_ready: Optional[threading.Event] = None,
    ) -> None:
        if generation is not None:
            event = self._stop if stop_event is None else stop_event
            if not self._network_is_current(event, int(generation)):
                return
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

        # A TCP connection is not an authenticated/usable control session
        # until the server has completed the hello/welcome handshake.  In
        # particular, an unsolicited update must never open the UDP audio gate.
        if not self._ctrl_received_welcome:
            if mtype != "welcome":
                raise ControlProtocolError(
                    f"expected welcome as first control message, received {mtype or '<missing type>'}"
                )
            require_control_protocol_version(msg.get("version"))
            session_token = parse_session_token(msg.get("session_token"))
            self._activate_session_token(int(session_token))
            self._ctrl_received_welcome = True
        elif mtype == "welcome":
            raise ControlProtocolError("received duplicate welcome")

        if mtype in ("welcome", "update"):
            raw_cfg = msg.get("config")
            if raw_cfg is not None and not isinstance(raw_cfg, dict):
                raise ControlProtocolError(f"{mtype} config must be an object")
            if mtype == "update" and not isinstance(raw_cfg, dict):
                raise ControlProtocolError("update is missing its config object")
            cfg = raw_cfg if isinstance(raw_cfg, dict) else None

            if isinstance(cfg, dict):
                if not self._ctrl_received_config and "buses" not in cfg:
                    raise ControlProtocolError("initial config is missing buses")
                if "input_gain_db" in cfg:
                    try:
                        with self._state_lock:
                            self._input_gain_db = max(-60.0, min(float(MAX_GAIN_DB), float(cfg.get("input_gain_db"))))
                    except Exception:
                        pass
                if "buses" in cfg:
                    buses = cfg.get("buses")
                    if not isinstance(buses, (list, dict)):
                        raise ControlProtocolError("config buses must be a list or object")
                    try:
                        parsed: dict[int, dict] = {}
                        if isinstance(buses, list):
                            for b in buses:
                                if not isinstance(b, dict):
                                    continue
                                try:
                                    bid = int(b.get("bus_id"))
                                except Exception:
                                    continue
                                parsed[int(bid)] = {
                                    "bus_id": int(bid),
                                    "name": str(b.get("name") or f"Bus {int(bid)}"),
                                    "feed_to_regie": bool(b.get("feed_to_regie", False)),
                                }
                        elif isinstance(buses, dict):
                            for k, b in buses.items():
                                if not isinstance(b, dict):
                                    continue
                                try:
                                    bid = int(k)
                                except Exception:
                                    continue
                                parsed[int(bid)] = {
                                    "bus_id": int(bid),
                                    "name": str(b.get("name") or f"Bus {int(bid)}"),
                                    "feed_to_regie": bool(b.get("feed_to_regie", False)),
                                }
                        self._ctrl_buses = parsed
                    except Exception:
                        pass
                if "return_vu_dbfs" in cfg:
                    try:
                        with self._state_lock:
                            self._return_vu_dbfs = float(cfg.get("return_vu_dbfs"))
                    except Exception:
                        pass
                if not bool(self._ctrl_received_config):
                    # No audio from the previous control session is allowed to
                    # survive into this one.  RX is gated until this reset has
                    # completed and a real server config was received.
                    try:
                        self._jb.reset()
                    except Exception:
                        pass
                    self._ctrl_received_config = True
                    ready_event = self._udp_ready if udp_ready is None else udp_ready
                    ready_event.set()
                    self._control_send_state()
                    self._send_silence_probe()
        elif mtype == "pong":
            if "return_vu_dbfs" in msg:
                try:
                    with self._state_lock:
                        self._return_vu_dbfs = float(msg.get("return_vu_dbfs"))
                except Exception:
                    pass
            return

    def _control_loop(
        self,
        stop_event: threading.Event,
        generation: int,
        udp_ready: threading.Event,
    ) -> None:
        backoff_s = 1.0
        liveness_timeout_s = CTRL_LIVENESS_TIMEOUT_S
        ping_interval_s = CTRL_PING_INTERVAL_S

        while self._network_is_current(stop_event, generation):
            sock: Optional[socket.socket] = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect((self.config.server_ip, int(self.config.control_port or 0)))
                if not self._network_is_current(stop_event, generation):
                    continue
                sock.settimeout(0.02)
                self._reset_session_token()
                with self._network_state_lock:
                    if (
                        int(self._network_generation) != int(generation)
                        or self._stop is not stop_event
                        or stop_event.is_set()
                    ):
                        continue
                    self._ctrl_sock = sock
                    self._ctrl_connected = True
                    self._ctrl_received_welcome = False
                    self._ctrl_received_config = False
                    udp_ready.clear()
                    self._ctrl_last_rx_monotonic = time.monotonic()
                    self._ctrl_last_tx_monotonic = time.monotonic()
                backoff_s = 1.0

                hello = {
                    "type": "hello",
                    "version": int(CONTROL_PROTOCOL_VERSION),
                    "client_id": int(self.client_id),
                    "client_uuid": str(self.config.client_uuid or ""),
                    "name": str(self.config.name or ""),
                    "applies_input_gain_locally": True,
                    "udp_port": int(self._sock.getsockname()[1]),
                }
                self._control_send(sock, hello)

                parser = JsonLineBuffer()
                while self._network_is_current(stop_event, generation):
                    now = time.monotonic()
                    if now - float(self._ctrl_last_rx_monotonic) >= float(liveness_timeout_s):
                        logger.debug("control liveness timeout (no rx for {:.1f}s)", float(liveness_timeout_s))
                        break
                    if now - float(self._ctrl_last_tx_monotonic) >= float(ping_interval_s):
                        try:
                            self._control_send(sock, {"type": "ping", "t": int(time.time() * 1000)})
                            self._ctrl_last_tx_monotonic = now
                        except Exception:
                            break

                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    for msg in parser.feed(chunk):
                        self._ctrl_last_rx_monotonic = time.monotonic()
                        self._control_handle_msg(
                            msg,
                            generation=generation,
                            stop_event=stop_event,
                            udp_ready=udp_ready,
                        )

            except ControlProtocolError as e:
                if not stop_event.is_set():
                    logger.warning("control protocol error, reconnecting: {}", e)
            except Exception as e:
                if not stop_event.is_set():
                    logger.debug("control loop error: {}", e)
            finally:
                udp_ready.clear()
                self._reset_session_token()
                if self._network_is_current(stop_event, generation):
                    try:
                        self._jb.reset()
                    except Exception:
                        pass
                try:
                    if sock is not None:
                        sock.close()
                except Exception:
                    pass
                with self._network_state_lock:
                    if int(self._network_generation) == int(generation):
                        if self._ctrl_sock is sock:
                            self._ctrl_sock = None
                        self._ctrl_connected = False
                        self._ctrl_received_welcome = False
                        self._ctrl_received_config = False

            if not self._network_is_current(stop_event, generation):
                break
            if stop_event.wait(backoff_s):
                break
            backoff_s = min(backoff_s * 2.0, 10.0)

    def _open_input_stream(self) -> sd.InputStream:
        dev_info = self._query_audio_device(self.config.input_device)

        if dev_info is not None:
            logger.debug(
                "input device: name={} hostapi={} max_in={} default_sr={}",
                dev_info.get("name"),
                dev_info.get("hostapi"),
                dev_info.get("max_input_channels"),
                dev_info.get("default_samplerate"),
            )

        sr = int(SAMPLE_RATE)
        ch = 1
        blocksize = int(FRAME_SAMPLES)
        extra_settings = shared_stream_extra_settings(dev_info)

        try:
            sd.check_input_settings(
                device=self.config.input_device,
                channels=ch,
                dtype="float32",
                samplerate=sr,
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=self.config.input_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        try:
            logger.debug(
                "opening InputStream: dev={} ch={} blocksize={} latency=low sr={}",
                str(self.config.input_device),
                ch,
                blocksize,
                sr,
            )
            st = sd.InputStream(
                samplerate=sr,
                channels=ch,
                dtype="float32",
                blocksize=blocksize,
                device=self.config.input_device,
                latency="low",
                extra_settings=extra_settings,
                callback=self._in_callback,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=self.config.input_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        with self._state_lock:
            self._in_channels = ch
            self._in_samplerate = int(sr)
        return st

    def _open_output_stream(self) -> sd.OutputStream:
        dev_info = self._query_audio_device(self.config.output_device)

        if dev_info is not None:
            logger.debug(
                "output device: name={} hostapi={} max_out={} default_sr={}",
                dev_info.get("name"),
                dev_info.get("hostapi"),
                dev_info.get("max_output_channels"),
                dev_info.get("default_samplerate"),
            )

        sr = int(SAMPLE_RATE)
        ch = 1
        blocksize = int(FRAME_SAMPLES)
        extra_settings = shared_stream_extra_settings(dev_info)

        try:
            sd.check_output_settings(
                device=self.config.output_device,
                channels=ch,
                dtype="float32",
                samplerate=sr,
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev_info,
                device=self.config.output_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        try:
            logger.debug(
                "opening OutputStream: dev={} ch={} blocksize={} latency=low sr={}",
                str(self.config.output_device),
                ch,
                blocksize,
                sr,
            )
            st = sd.OutputStream(
                samplerate=sr,
                channels=ch,
                dtype="float32",
                blocksize=blocksize,
                device=self.config.output_device,
                latency="low",
                extra_settings=extra_settings,
                callback=self._out_callback,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev_info,
                device=self.config.output_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        with self._state_lock:
            self._out_channels = ch
            self._out_samplerate = sr
        return st

    def _in_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self._input_callback_statuses += 1

        if self._stop.is_set() or not self._udp_ready.is_set():
            with self._state_lock:
                self._in_vu_dbfs = -60.0
            return

        with self._state_lock:
            gain_db = float(self._input_gain_db)
            bus_mask = self._ptt_bus_mask(self._ptt_buses)

        if bus_mask == 0:
            with self._state_lock:
                self._in_vu_dbfs = -60.0
                for bus_id in FIXED_BUS_IDS:
                    bid = int(bus_id)
                    self._bus_vu_dbfs[bid] = max(
                        -60.0,
                        float(self._bus_vu_dbfs.get(bid, -60.0)) - 3.0,
                    )
            return

        if int(frames) != int(FRAME_SAMPLES):
            self._input_frame_mismatches += 1
            return

        if indata.ndim == 1 and int(indata.shape[0]) == int(FRAME_SAMPLES):
            source = indata
        elif (
            indata.ndim == 2
            and int(indata.shape[0]) == int(FRAME_SAMPLES)
            and int(indata.shape[1]) == 1
        ):
            source = indata[:, 0]
        else:
            self._input_frame_mismatches += 1
            return

        with self._tx_lock:
            session_token = int(self._session_token)
            session_epoch = int(self._audio_session_epoch)
        if session_token == 0:
            return

        np.copyto(self._capture_work, source, casting="same_kind")
        gain_linear = math.pow(10.0, gain_db / 20.0)
        np.multiply(self._capture_work, gain_linear, out=self._capture_work)
        np.absolute(self._capture_work, out=self._capture_abs_work)
        peak = float(np.max(self._capture_abs_work))
        if not math.isfinite(peak):
            self._capture_work.fill(0.0)
        elif peak > 1.0:
            np.multiply(self._capture_work, 0.99 / peak, out=self._capture_work)
        np.clip(self._capture_work, -1.0, 1.0, out=self._capture_work)
        vu = self._rms_dbfs_realtime(self._capture_work)

        with self._state_lock:
            self._in_vu_dbfs = vu
            for bus_id in FIXED_BUS_IDS:
                bid = int(bus_id)
                if bus_mask & (1 << bid):
                    self._bus_vu_dbfs[bid] = vu
                else:
                    self._bus_vu_dbfs[bid] = max(
                        -60.0,
                        float(self._bus_vu_dbfs.get(bid, -60.0)) - 3.0,
                    )

        metadata = self._pack_tx_metadata(session_epoch, bus_mask)
        with self._tx_lock:
            if (
                int(self._session_token) != session_token
                or int(self._audio_session_epoch) != session_epoch
                or self._stop.is_set()
                or not self._udp_ready.is_set()
            ):
                return
            self._tx_ring.push(self._capture_work, metadata=metadata)

    def _accept_server_udp_packet(self, pkt) -> bool:
        if int(getattr(pkt, "flags", 0)) != int(AudioPacketDirection.SERVER_TO_CLIENT):
            self._rx_invalid_directions += 1
            return False

        if int(getattr(pkt, "client_id", -1)) != int(self.client_id):
            self._rx_invalid_packets += 1
            return False

        stream_id = int(getattr(pkt, "stream_id", -1))
        bus_mask = int(getattr(pkt, "bus_mask", -1))
        if stream_id != 0 or bus_mask < 0 or bus_mask & ~int(_FIXED_BUS_MASK):
            self._rx_invalid_route_masks += 1
            return False

        seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
        payload = bytes(getattr(pkt, "payload", b""))
        with self._tx_lock:
            session_token = int(self._session_token)
            if (
                session_token == 0
                or int(getattr(pkt, "session_token", 0)) != session_token
            ):
                self._rx_invalid_tokens += 1
                return False
            metadata = JitterFrameMetadata(
                session_token=session_token,
                client_id=int(pkt.client_id),
                timestamp_ms=int(pkt.timestamp_ms),
                stream_id=stream_id,
                bus_mask=bus_mask,
                flags=int(pkt.flags),
            )
            try:
                self._jb.push(seq, payload, metadata)
            except Exception:
                self._rx_invalid_packets += 1
                return False

        self._rx_packets += 1
        self._last_udp_rx_monotonic = time.monotonic()
        return True

    def _rx_loop(
        self,
        stop_event: threading.Event,
        generation: int,
        udp_ready: threading.Event,
    ) -> None:
        while self._network_is_current(stop_event, generation):
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                self._rx_socket_errors += 1
                err_code = getattr(e, 'winerror', None) or getattr(e, 'errno', None)
                if err_code == 10054:
                    continue
                if stop_event.is_set() or not self._network_is_current(stop_event, generation):
                    return
                if stop_event.wait(0.1):
                    return
                continue

            if not self._network_is_current(stop_event, generation):
                return
            if not self._is_expected_udp_source(addr):
                self._rx_source_dropped += 1
                continue
            if not udp_ready.is_set():
                self._rx_not_ready_dropped += 1
                continue

            try:
                pkt = unpack_audio_packet_v2(data)
            except AudioPacketValidationError as exc:
                self._count_invalid_audio_packet(exc.reason)
                continue
            except Exception:
                self._rx_invalid_packets += 1
                continue

            if not self._accept_server_udp_packet(pkt):
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
                    "stats: tx_pkts={} rx_pkts={} enc_err={} dec_err={} tx_sock_err={} rx_sock_err={} rx_source_drop={} rx_not_ready_drop={} rx_invalid={} rx_token={} rx_direction={} rx_route={} playback_samples={} jb_buf={} jb_missing={} jb_late_dropped={} jb_concealed={} playout_underflows={}",
                    self._tx_packets,
                    self._rx_packets,
                    self._opus_encode_errors,
                    self._opus_decode_errors,
                    self._tx_socket_errors,
                    self._rx_socket_errors,
                    self._rx_source_dropped,
                    self._rx_not_ready_dropped,
                    self._rx_invalid_packets,
                    self._rx_invalid_tokens,
                    self._rx_invalid_directions,
                    self._rx_invalid_route_masks,
                    playback_samples,
                    jb_buf,
                    0 if jb_stats is None else int(jb_stats.missing),
                    0 if jb_stats is None else int(jb_stats.late_dropped),
                    0 if jb_stats is None else int(jb_stats.concealed),
                    int(self._playout_underflows),
                )

    def _decode_playout_frame(
        self,
        payload: bytes,
        *,
        decoder: Optional[OpusDecoder] = None,
    ) -> np.ndarray:
        """Decode exactly one protocol frame, failing to silence on mismatch."""
        try:
            active_decoder = self._dec if decoder is None else decoder
            decoded = np.asarray(active_decoder.decode(payload), dtype=np.float32)
            if decoded.ndim != 1 or int(decoded.shape[0]) != int(FRAME_SAMPLES):
                raise ValueError(
                    f"decoded Opus frame has {int(decoded.size)} samples; expected {int(FRAME_SAMPLES)}"
                )
            if not np.all(np.isfinite(decoded)):
                raise ValueError("decoded Opus frame contains non-finite samples")
            return decoded
        except Exception as e:
            self._opus_decode_errors += 1
            logger.debug("Opus playout decode rejected: {}", e)
            return np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self._output_callback_statuses += 1

        if self._stop.is_set() or not self._udp_ready.is_set():
            outdata.fill(0.0)
            return

        with self._state_lock:
            out_sr = int(self._out_samplerate)
            out_gain_db = float(self._output_gain_db)

        if out_sr != int(SAMPLE_RATE) or int(frames) != int(FRAME_SAMPLES):
            self._output_frame_mismatches += 1
            outdata.fill(0.0)
            return

        metadata = self._playout_ring.pop_into(self._output_work)
        if metadata is None:
            self._playout_underflows += 1
            outdata.fill(0.0)
            with self._state_lock:
                self._out_vu_dbfs = -60.0
            return

        gain_linear = math.pow(10.0, out_gain_db / 20.0)
        np.multiply(self._output_work, gain_linear, out=self._output_work)
        np.clip(self._output_work, -1.0, 1.0, out=self._output_work)
        vu = self._rms_dbfs_realtime(self._output_work)

        with self._state_lock:
            self._out_vu_dbfs = vu

        if outdata.ndim == 1:
            np.copyto(outdata, self._output_work, casting="same_kind")
        elif outdata.ndim == 2 and int(outdata.shape[0]) == int(FRAME_SAMPLES):
            for channel in range(int(outdata.shape[1])):
                np.copyto(
                    outdata[:, channel],
                    self._output_work,
                    casting="same_kind",
                )
        else:
            self._output_frame_mismatches += 1
            outdata.fill(0.0)
