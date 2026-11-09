from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common import opus_codec
from ..common.audio import clamp_gain_db
from ..common.buses import FIXED_BUS_IDS
from ..common.constants import (
    AUDIO_UDP_PORT,
    CONTROL_PORT_OFFSET,
    FRAME_SAMPLES,
    JB_START_FRAMES,
    PLAYOUT_LEAD_FRAMES,
    SAMPLE_RATE,
)
from ..common.frame_ring import FrameRingBuffer
from ..common.jitter_buffer import new_realtime_jitter_buffer
from ..common.logging import best_effort
from ..common.qos import VoiceQos
from ..common.realtime import configure_udp_socket, run_as_audio_thread
from .audio_io import AudioIOMixin
from .control import ControlMixin
from .transport import TransportMixin

_TX_RING_CAPACITY = 8
_PLAYOUT_RING_CAPACITY = max(8, int(JB_START_FRAMES) * 2)
_TX_METADATA_BUS_BITS = 16
_TX_METADATA_BUS_MASK = (1 << _TX_METADATA_BUS_BITS) - 1
_AUDIO_SESSION_EPOCH_MASK = (1 << (63 - _TX_METADATA_BUS_BITS)) - 1


@dataclass(frozen=True, slots=True)
class ClientMeterLevels:
    """Meter levels in dBFS: capture, headphones, Return and the capture level per talked-to bus."""

    input_dbfs: float
    input_transmitting: bool
    output_dbfs: float
    return_dbfs: float
    bus_dbfs: dict[int, float]


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

    listen_return_bus: bool = False
    listen_regie: bool = True
    duck_return: bool = False
    # Deeper fixed network margin on both ends of this station's link.
    wifi_link: bool = False


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

        # Fail fast when libopus is unusable. The TX and playout workers own
        # their codecs and create a fresh pair for every audio session.
        opus_codec.OpusEncoder()
        opus_codec.OpusDecoder()
        self._opuslib_version = opus_codec.opuslib_version()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with best_effort("_sock.setsockopt()"):
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        configure_udp_socket(self._sock)
        self._sock.bind(("0.0.0.0", 0))
        self._resolve_server_address()
        self._sock.settimeout(0.5)
        # The audio sent to the server is marked as voice (see _start_network).
        self._voice_qos = VoiceQos(self._sock)

        self._jb = new_realtime_jitter_buffer(wifi=bool(self.config.wifi_link))

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
        # Filled on the card's own clock (see _playout_loop): it never holds
        # more than PLAYOUT_LEAD_FRAMES, so it needs no latency cap.
        self._playout_ring = FrameRingBuffer(
            capacity=_PLAYOUT_RING_CAPACITY,
            frame_samples=int(FRAME_SAMPLES),
        )
        # Set by the output callback each time the card takes a frame, at
        # _playout_tick_at (monotonic seconds).
        self._playout_tick = threading.Event()
        self._playout_tick_at = 0.0
        # Frames queued ahead of the card: PLAYOUT_LEAD_FRAMES, raised by the
        # output callback for a card that calls back in bursts (audio_io).
        self._playout_lead = int(PLAYOUT_LEAD_FRAMES)
        self._playout_burst_run = 0
        self._playout_burst_max_run = 0
        self._playout_bursts = 0
        self._capture_work = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
        self._output_work = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
        # Output declick (audio_io): the last sample played, and whether the
        # previous callback played silence. Written by the output callback only.
        self._out_last_sample = 0.0
        self._out_was_silent = True
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
        self._input_gain_db = clamp_gain_db(self.config.input_gain_db)
        self._output_gain_db = clamp_gain_db(self.config.output_gain_db)
        self._return_gain_db = clamp_gain_db(self.config.return_gain_db)
        self._in_vu_dbfs = -60.0
        self._in_transmitting = False
        self._out_vu_dbfs = -60.0
        self._return_vu_dbfs = -60.0
        self._bus_vu_dbfs: dict[int, float] = {int(bus_id): -60.0 for bus_id in FIXED_BUS_IDS}

        # Sound card counters live as long as the audio streams; the network
        # counters restart with each network session.
        self._input_callback_statuses = 0
        self._output_callback_statuses = 0
        self._input_callback_errors = 0
        self._output_callback_errors = 0
        self._input_callback_error_streak = 0
        self._output_callback_error_streak = 0
        self._input_frame_mismatches = 0
        self._output_frame_mismatches = 0
        self._reset_network_counters()
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
        self._ctrl_ping_sent_monotonic: float = 0.0
        self._ctrl_rtt_ms: float | None = None
        self._ctrl_received_welcome: bool = False
        # Why the server refused this station's hello; empty unless refused.
        self._ctrl_refusal: str = ""
        self._ctrl_received_config: bool = False
        self._session_token: int = 0

        self._ptt_buses: dict[int, bool] = {}
        self._listen_return_bus: bool = bool(self.config.listen_return_bus)
        self._listen_regie: bool = bool(self.config.listen_regie)
        self._duck_return: bool = bool(self.config.duck_return)

    def get_stats_snapshot(self) -> dict:
        # Thread and PortAudio queries can release the GIL: they are gathered
        # before taking the state lock.
        tx_ring_stats = self._tx_ring.stats
        playout_ring_stats = self._playout_ring.stats
        jitter_buffered_frames = self._jb.buffered_frames
        control_worker_alive = bool(self._ctrl_thread is not None and self._ctrl_thread.is_alive())
        rx_worker_alive = bool(self._rx_thread is not None and self._rx_thread.is_alive())
        tx_worker_alive = bool(self._tx_thread is not None and self._tx_thread.is_alive())
        playout_worker_alive = bool(self._playout_thread is not None and self._playout_thread.is_alive())
        network_workers_alive = bool(
            control_worker_alive and rx_worker_alive and tx_worker_alive and playout_worker_alive
        )
        in_stream_active = self._stream_is_active(self._in_stream)
        out_stream_active = self._stream_is_active(self._out_stream)
        audio_latency_ms = self._streams_latency_ms()
        jitter_stats = self._jb.stats

        with self._state_lock:
            now = time.monotonic()
            ctrl_age_s = max(0.0, now - float(self._ctrl_last_rx_monotonic)) if self._ctrl_connected else None
            ctrl_tx_age_s = max(0.0, now - float(self._ctrl_last_tx_monotonic)) if self._ctrl_connected else None
            udp_rx_age_s = (
                max(0.0, now - float(self._last_udp_rx_monotonic)) if float(self._last_udp_rx_monotonic) > 0.0 else None
            )

            return {
                "input_gain_db": self._input_gain_db,
                "output_gain_db": self._output_gain_db,
                "return_gain_db": self._return_gain_db,
                "jitter_buffered_frames": int(jitter_buffered_frames),
                "jitter_trimmed": int(jitter_stats.trimmed),
                "jitter_missing": int(jitter_stats.missing),
                "jitter_concealed": int(jitter_stats.concealed),
                "jitter_fec": int(jitter_stats.fec),
                "control_rtt_ms": self._ctrl_rtt_ms,
                "audio_latency_ms": audio_latency_ms,
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
                "playout_lead_frames": int(self._playout_lead),
                "playout_bursts": int(self._playout_bursts),
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
                "input_callback_statuses": int(self._input_callback_statuses),
                "output_callback_statuses": int(self._output_callback_statuses),
                "input_callback_errors": int(self._input_callback_errors),
                "output_callback_errors": int(self._output_callback_errors),
                "input_frame_mismatches": int(self._input_frame_mismatches),
                "output_frame_mismatches": int(self._output_frame_mismatches),
                "tx_worker_failures": int(self._tx_worker_failures),
                "playout_worker_failures": int(self._playout_worker_failures),
                "opus_encoder_resets": int(self._opus_encoder_resets),
                "opus_decoder_resets": int(self._opus_decoder_resets),
                "opus_encoder_reset_errors": int(self._opus_encoder_reset_errors),
                "opus_decoder_reset_errors": int(self._opus_decoder_reset_errors),
                # libopus worked when the client was built; this session's
                # workers have not failed to create a codec since.
                "opus_ok": self._opus_encoder_reset_errors == 0 and self._opus_decoder_reset_errors == 0,
                "opuslib_version": str(self._opuslib_version),
                "stopped": bool(self._stop.is_set()),
                "fully_stopped": bool(self._fully_stopped),
                "kicked": bool(self._kicked),
                "kick_message": str(self._kick_message),
                "control_connected": bool(self._ctrl_connected),
                "control_welcome_received": bool(self._ctrl_received_welcome),
                "control_refusal": str(self._ctrl_refusal),
                "control_ready": bool(
                    self._ctrl_received_config and int(self._session_token) != 0 and self._udp_ready.is_set()
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
                "listen_return_bus": bool(self._listen_return_bus),
                "listen_regie": bool(self._listen_regie),
                "duck_return": bool(self._duck_return),
                "name": str(self.config.name or ""),
            }

    def meter_levels(self) -> ClientMeterLevels:
        """Current meter levels, read without any lock (GUI meters refresh at 15 Hz).

        Each level is a float attribute, or an item of a dict that is never
        resized, written by the audio callbacks or the control thread: reading
        one is atomic under the GIL. The full snapshot would take the locks
        the callbacks use, for the sake of four numbers.
        """
        return ClientMeterLevels(
            input_dbfs=float(self._in_vu_dbfs),
            input_transmitting=bool(self._in_transmitting),
            output_dbfs=float(self._out_vu_dbfs),
            return_dbfs=float(self._return_vu_dbfs),
            bus_dbfs=dict(self._bus_vu_dbfs),
        )

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
            self._input_gain_db = clamp_gain_db(gain_db, default=self._input_gain_db)
        self._control_send_state()

    def set_output_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._output_gain_db = clamp_gain_db(gain_db, default=self._output_gain_db)

    def set_return_gain_db(self, gain_db: float) -> None:
        with self._state_lock:
            self._return_gain_db = clamp_gain_db(gain_db, default=self._return_gain_db)
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
        epoch = (int(self._audio_session_epoch) + 1) & int(_AUDIO_SESSION_EPOCH_MASK)
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

    def _activate_session_token(self, session_token: int, stop_event: threading.Event) -> bool:
        """Open the audio session of a welcome, unless its network generation is stopping.

        _stop_network sets ``stop_event`` before it resets the session under
        ``_tx_lock``. Checked under that lock, a welcome racing a disconnect
        either lands before that reset or is refused: it never re-arms the
        token after it.
        """
        token = int(session_token)
        if token <= 0 or token > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("session_token must be a non-zero uint64")
        with self._tx_lock:
            if stop_event.is_set():
                return False
            self._advance_audio_session_epoch_locked()
            self._session_token = token
            self._tx_ring.clear()
            self._playout_ring.clear()
            with best_effort("_jb.reset()"):
                self._jb.reset()
        return True

    def _reset_network_counters(self) -> None:
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
        self._tx_worker_failures = 0
        self._playout_worker_failures = 0
        self._opus_encoder_resets = 0
        self._opus_decoder_resets = 0
        self._opus_encoder_reset_errors = 0
        self._opus_decoder_reset_errors = 0
        self._last_udp_rx_monotonic = 0.0

    def _network_is_current_locked(self, stop_event: threading.Event, generation: int) -> bool:
        return int(self._network_generation) == int(generation) and self._stop is stop_event and not stop_event.is_set()

    def _network_is_current(self, stop_event: threading.Event, generation: int) -> bool:
        with self._network_state_lock:
            return self._network_is_current_locked(stop_event, generation)

    def _start_network_threads(self) -> None:
        """Start a fresh, isolated network generation."""
        with self._network_lifecycle_lock:
            if self._fully_stopped:
                raise RuntimeError("a fully stopped IntercomClient cannot be restarted; create a new instance")
            with self._network_state_lock:
                old_threads = (
                    self._rx_thread,
                    self._ctrl_thread,
                    self._tx_thread,
                    self._playout_thread,
                )
            if any(t is not None and t.is_alive() for t in old_threads):
                raise RuntimeError("previous network workers are still running")

            # The audio session is already empty: either this client is new or
            # _stop_network reset it. Only the socket backlog is left.
            self._resolve_server_address()
            # No worker runs: the "Liaison Wi-Fi" choice of this connection
            # takes effect without racing a reader.
            self._jb = new_realtime_jitter_buffer(wifi=bool(self.config.wifi_link))
            for marked in self._voice_qos.marked:
                if marked != self._server_addr:
                    self._voice_qos.remove(marked)
            self._voice_qos.add(self._server_addr)
            self._drain_udp_socket()
            self._reset_network_counters()

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
                self._ctrl_refusal = ""
                rx_thread = threading.Thread(
                    target=run_as_audio_thread,
                    args=(self._rx_loop, stop_event, generation, udp_ready),
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
                    target=run_as_audio_thread,
                    args=(self._tx_loop, stop_event, generation, udp_ready),
                    name=f"audio-tx-{generation}",
                    daemon=True,
                )
                playout_thread = threading.Thread(
                    target=run_as_audio_thread,
                    args=(self._playout_loop, stop_event, generation, udp_ready),
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
            raise RuntimeError("a fully stopped IntercomClient cannot be restarted; create a new instance")

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
            self._start_audio_streams()
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
                self._playout_tick.set()  # wake the playout worker at once

            self._reset_session_token()

            # Closing/shutting down TCP wakes recv or connect immediately. The
            # UDP socket intentionally remains open so reconnect reuses its
            # local port: a datagram sent to that port wakes recvfrom instead
            # (any leftover is drained by the next session).
            if ctrl_sock is not None:
                with best_effort("ctrl_sock.shutdown()"):
                    ctrl_sock.shutdown(socket.SHUT_RDWR)
                with best_effort("ctrl_sock.close()"):
                    ctrl_sock.close()
            if rx_thread is not None and rx_thread.is_alive():
                with best_effort("wake udp-rx"):
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as waker:
                        waker.sendto(b"\0", ("127.0.0.1", int(self._sock.getsockname()[1])))

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
        """Reconnect to the server, reusing the UDP socket and its local port.

        Running audio streams are kept. A stream that died (device unplugged,
        Bluetooth profile switch, ...) never comes back by itself, so both
        streams are reopened first.
        """
        with self._network_lifecycle_lock:
            if self._fully_stopped:
                raise RuntimeError("a fully stopped IntercomClient cannot be reconnected; create a new instance")
            if not self._stop_network():
                raise RuntimeError("cannot reconnect while previous network workers are still running")

            if self._in_stream is not None and not self.audio_streams_active():
                logger.warning("audio streams inactive: reopening them before reconnecting")
                self._close_audio_streams()
                self._start_audio_streams()

            self._kicked = False
            self._kick_message = ""
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
            self._voice_qos.close()
            with best_effort("_sock.close()"):
                self._sock.close()
            self._close_audio_streams()
            self._fully_stopped = True
