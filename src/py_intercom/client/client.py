from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common import opus_codec
from ..common.buses import FIXED_BUS_IDS
from ..common.constants import (
    AUDIO_UDP_PORT,
    CONTROL_PORT_OFFSET,
    FRAME_SAMPLES,
    JB_START_FRAMES,
    LATENCY_TRIM_AFTER_FRAMES,
    MAX_GAIN_DB,
    PCM_RING_TRIM_ABOVE_FRAMES,
    SAMPLE_RATE,
)
from ..common.frame_ring import FrameRingBuffer
from ..common.jitter_buffer import new_realtime_jitter_buffer
from ..common.logging import best_effort
from .audio_io import AudioIOMixin
from .control import ControlMixin
from .transport import TransportMixin

_TX_RING_CAPACITY = 8
_PLAYOUT_RING_CAPACITY = max(8, int(JB_START_FRAMES) * 2)
_TX_METADATA_BUS_BITS = 16
_TX_METADATA_BUS_MASK = (1 << _TX_METADATA_BUS_BITS) - 1
_AUDIO_SESSION_EPOCH_MASK = (1 << (63 - _TX_METADATA_BUS_BITS)) - 1


@dataclass
class ClientConfig:
    server_ip: str
    server_port: int = AUDIO_UDP_PORT
    control_port: int | None = None

    name: str = ""
    client_uuid: str = ""
    input_device: int | None = None
    output_device: int | None = None
    input_gain_db: float = 0.0
    output_gain_db: float = 0.0
    return_gain_db: float = 0.0

    ptt_bus_keys: dict | None = None

    listen_return_bus: bool = False
    listen_regie: bool = True
    duck_return: bool = False


class IntercomClient(AudioIOMixin, ControlMixin, TransportMixin):
    """LAN intercom client engine, independent of Qt.

    Network sessions are isolated by generation: a reconnect starts a fresh
    set of workers and nothing from the previous session can leak into it.
    """

    def __init__(self, client_id: int, config: ClientConfig):
        self.client_id = client_id & 0xFFFFFFFF
        self.config = config

        if self.config.control_port is None:
            self.config.control_port = int(self.config.server_port) + int(CONTROL_PORT_OFFSET)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with best_effort("_sock.setsockopt()"):
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        self._sock.bind(("0.0.0.0", 0))
        self._server_addr = (self.config.server_ip, int(self.config.server_port))
        self._expected_server_udp_ips: frozenset[str] = frozenset()
        self._refresh_expected_server_udp_sources()
        self._sock.settimeout(0.5)

        self._enc = opus_codec.OpusEncoder()
        self._dec = opus_codec.OpusDecoder()

        self._jb = new_realtime_jitter_buffer()

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
            trim_above=PCM_RING_TRIM_ABOVE_FRAMES,
            trim_after=LATENCY_TRIM_AFTER_FRAMES,
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

        self._in_stream: sd.InputStream | None = None
        self._out_stream: sd.OutputStream | None = None

        self._rx_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None
        self._playout_thread: threading.Thread | None = None

        self._ctrl_sock: socket.socket | None = None
        self._ctrl_thread: threading.Thread | None = None
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
        self._duck_return: bool = bool(self.config.duck_return)

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
            jitter_trimmed = int(self._jb.stats.trimmed)

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
                "jitter_trimmed": jitter_trimmed,
                "tx_ring_depth": int(tx_ring_stats.depth),
                "tx_ring_pushed": int(tx_ring_stats.pushed),
                "tx_ring_popped": int(tx_ring_stats.popped),
                "tx_ring_dropped": int(tx_ring_stats.dropped),
                "tx_ring_underflows": int(tx_ring_stats.underflows),
                "playout_ring_depth": int(playout_ring_stats.depth),
                "playout_ring_pushed": int(playout_ring_stats.pushed),
                "playout_ring_popped": int(playout_ring_stats.popped),
                "playout_ring_dropped": int(playout_ring_stats.dropped),
                "playout_ring_trimmed": int(playout_ring_stats.trimmed),
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
                "duck_return": bool(self._duck_return),
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

    def set_duck_return(self, enabled: bool) -> None:
        with self._state_lock:
            self._duck_return = bool(enabled)
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
            with best_effort("_jb.reset()"):
                self._jb.reset()

    def _activate_session_token(self, session_token: int) -> None:
        token = int(session_token)
        if token <= 0 or token > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("session_token must be a non-zero uint64")
        with self._tx_lock:
            self._advance_audio_session_epoch_locked()
            self._session_token = token
            self._tx_ring.clear()
            self._playout_ring.clear()
            with best_effort("_jb.reset()"):
                self._jb.reset()

    def _network_is_current(self, stop_event: threading.Event, generation: int) -> bool:
        with self._network_state_lock:
            return (
                int(self._network_generation) == int(generation)
                and self._stop is stop_event
                and not stop_event.is_set()
            )

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
        with best_effort("sd.stop()"):
            sd.stop()

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
            with best_effort("rollback after failed start"):
                self.stop()
            raise

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
                with best_effort("ctrl_sock.shutdown()"):
                    ctrl_sock.shutdown(socket.SHUT_RDWR)
                with best_effort("ctrl_sock.close()"):
                    ctrl_sock.close()

            deadline = time.monotonic() + 3.0
            for thread in (ctrl_thread, rx_thread, tx_thread, playout_thread):
                if thread is None or thread is threading.current_thread() or not thread.is_alive():
                    continue
                with best_effort("thread.join()"):
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))

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
        with best_effort("disconnect_network()"):
            self.disconnect_network()

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
            with best_effort("_jb.reset()"):
                self._jb.reset()

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
            with best_effort("_sock.close()"):
                self._sock.close()

            # Reverse startup order.  In particular, keep Bluetooth
            # capture/HFP active until its matching playback endpoint has
            # been closed.
            out_stream, self._out_stream = self._out_stream, None
            in_stream, self._in_stream = self._in_stream, None
            self._close_audio_stream(out_stream)
            self._close_audio_stream(in_stream)
            self._fully_stopped = True

