"""UDP audio plane: Opus encode/send, receive, jitter buffering and playout."""

from __future__ import annotations

import socket
import threading
import time

import numpy as np
from loguru import logger

from ..common import opus_codec
from ..common.buses import FIXED_BUS_MASK
from ..common.constants import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
)
from ..common.jitter_buffer import JitterFrameMetadata
from ..common.logging import best_effort
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import (
    AudioPacketDirection,
    AudioPacketValidationError,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)

_PLAYOUT_FRAME_PERIOD_S = float(FRAME_SAMPLES) / float(SAMPLE_RATE)


class TransportMixin:
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
                except (TimeoutError, BlockingIOError):
                    break
                except OSError as e:
                    err_code = getattr(e, "winerror", None) or getattr(e, "errno", None)
                    if err_code in (11, 35, 10035):
                        break
                    logger.debug("UDP drain stopped after {} datagrams: {}", drained, e)
                    break
        finally:
            with best_effort("_sock.settimeout()"):
                self._sock.settimeout(previous_timeout)
        if drained:
            logger.debug("discarded {} stale UDP datagrams before reconnect", drained)
        return int(drained)

    def _tx_loop(
        self,
        stop_event: threading.Event,
        generation: int,
        udp_ready: threading.Event,
    ) -> None:
        """Own the Opus encoder, sequence and UDP transmission path."""

        frame = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
        encoder: OpusEncoder | None = None
        encoder_epoch: int | None = None
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
            if bus_mask & ~int(FIXED_BUS_MASK):
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
                    encoder = opus_codec.OpusEncoder()
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
        decoder: OpusDecoder | None = None
        decoder_epoch: int | None = None
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
                    decoder = opus_codec.OpusDecoder()
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

    def _accept_server_udp_packet(self, pkt) -> bool:
        if int(getattr(pkt, "flags", 0)) != int(AudioPacketDirection.SERVER_TO_CLIENT):
            self._rx_invalid_directions += 1
            return False

        if int(getattr(pkt, "client_id", -1)) != int(self.client_id):
            self._rx_invalid_packets += 1
            return False

        stream_id = int(getattr(pkt, "stream_id", -1))
        bus_mask = int(getattr(pkt, "bus_mask", -1))
        if stream_id != 0 or bus_mask < 0 or bus_mask & ~int(FIXED_BUS_MASK):
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
            except TimeoutError:
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
        decoder: OpusDecoder | None = None,
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
