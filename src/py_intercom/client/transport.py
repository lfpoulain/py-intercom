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
    PLAYOUT_DECODE_AHEAD_MS,
    SAMPLE_RATE,
)
from ..common.jitter_buffer import JitterFrame, JitterFrameMetadata, decode_jitter_frame, skip_silent_slot
from ..common.logging import best_effort
from ..common.opus_codec import OpusDecoder, OpusEncoder
from ..common.packets import (
    WSAECONNRESET,
    WSAEMSGSIZE,
    WSAENETRESET,
    AudioPacketDirection,
    AudioPacketValidationError,
    count_rejected_datagram,
    is_valid_route,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)

# How long the playout worker waits for the card before re-checking its state.
_PLAYOUT_IDLE_WAIT_S = 0.1
# When to read the jitter buffer, counted from the card's previous callback.
_PLAYOUT_DECODE_AT_S = (float(FRAME_SAMPLES) / float(SAMPLE_RATE)) - PLAYOUT_DECODE_AHEAD_MS / 1000.0


class TransportMixin:
    def _resolve_server_address(self) -> None:
        """Resolve the configured server once per network session.

        Audio and control then use the first IPv4 address, so the realtime TX
        path never resolves a host name. Every resolved address is accepted
        as a UDP source.
        """
        host = str(self.config.server_ip or "").strip()
        port = int(self.config.server_port)
        resolved: list[str] = []
        if host:
            try:
                for info in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM):
                    address = str(info[4][0])
                    if address not in resolved:
                        resolved.append(address)
            except (OSError, UnicodeError) as e:
                logger.warning("could not resolve server {}:{}: {}", host, port, e)
        self._server_addr = (resolved[0] if resolved else host, port)
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
        """Own jitter playout and Opus decoding, on the headphones' clock.

        Each output callback sets ``_playout_tick`` when the card takes a
        frame; this worker then pops the jitter buffer PLAYOUT_DECODE_AHEAD_MS
        before the card's next read and queues the next frame,
        ``_playout_lead`` frames ahead (PLAYOUT_LEAD_FRAMES unless the card
        calls back in bursts, see AudioIOMixin._note_playout_burst). The jitter buffer is therefore read
        exactly as fast as the card plays: no second clock, no drift between
        them, and no PCM ring margin to trim. A slot the card had to play as
        silence (late worker) is not made up: its frame stays in the jitter
        buffer, which sheds that extra depth in the next pause between words
        (skip_silent_slot), or through its latency cap.
        """

        decoder: OpusDecoder | None = None
        decoder_epoch: int | None = None
        while self._network_is_current(stop_event, generation):
            if not udp_ready.is_set():
                if stop_event.wait(_PLAYOUT_IDLE_WAIT_S):
                    return
                continue
            if not self._playout_tick.wait(_PLAYOUT_IDLE_WAIT_S):
                continue  # output stream stopped or not started: check again
            self._playout_tick.clear()
            delay = self._playout_tick_at + _PLAYOUT_DECODE_AT_S - time.monotonic()
            if delay > 0.0:
                time.sleep(delay)  # high-resolution timer on Windows (Python 3.11+)

            while self._playout_ring.depth < self._playout_lead:
                if not self._network_is_current(stop_event, generation) or not udp_ready.is_set():
                    break
                try:
                    jitter_frame = self._jb.pop_frame()
                except Exception as e:
                    self._playout_worker_failures += 1
                    logger.debug("jitter playout failed: {}", e)
                    break
                if jitter_frame is None:
                    break  # pre-buffering or silence: the card plays silence

                with self._tx_lock:
                    session_token = int(self._session_token)
                    session_epoch = int(self._audio_session_epoch)
                if session_token == 0:
                    break
                if not bool(jitter_frame.concealed) and int(jitter_frame.metadata.session_token) != session_token:
                    break

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
                        break
                    decoder_epoch = int(session_epoch)
                    self._opus_decoder_resets += 1

                decoded = self._decode_playout_frame(jitter_frame, decoder)
                # Extra depth left by a stall goes away in a pause between
                # words, not as a cut in the middle of one.
                played_frame, decoded = skip_silent_slot(
                    self._jb,
                    jitter_frame,
                    decoded,
                    lambda following, _decoder=decoder: self._decode_playout_frame(following, _decoder),
                )
                if played_frame is not jitter_frame:
                    jitter_frame = played_frame
                    if int(jitter_frame.metadata.session_token) != session_token:
                        break

                # Prevent a decode from the previous session from being published
                # after reconnect cleared the PCM ring.
                with self._tx_lock:
                    if (
                        int(self._session_token) != session_token
                        or int(self._audio_session_epoch) != session_epoch
                        or stop_event.is_set()
                        or not udp_ready.is_set()
                    ):
                        break
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

        if not is_valid_route(int(getattr(pkt, "stream_id", -1)), int(getattr(pkt, "bus_mask", -1))):
            self._rx_invalid_route_masks += 1
            return False

        seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
        payload = bytes(getattr(pkt, "payload", b""))
        with self._tx_lock:
            session_token = int(self._session_token)
            if session_token == 0 or int(getattr(pkt, "session_token", 0)) != session_token:
                self._rx_invalid_tokens += 1
                return False
            # Playout only checks the session; the downlink route is not used.
            metadata = JitterFrameMetadata(session_token=session_token)
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
                err_code = getattr(e, "winerror", None) or getattr(e, "errno", None)
                if err_code == WSAEMSGSIZE:
                    self._rx_invalid_packets += 1
                    continue
                self._rx_socket_errors += 1
                # Per-datagram reports: never pause reception.
                if err_code in (WSAENETRESET, WSAECONNRESET):
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
                count_rejected_datagram(self, exc.reason)
                continue
            except Exception:
                self._rx_invalid_packets += 1
                continue

            if not self._accept_server_udp_packet(pkt):
                continue

            now = time.monotonic()
            if now - self._last_stats_log >= 5.0:
                self._last_stats_log = now
                self._log_stats()

    def _log_stats(self) -> None:
        jb_stats = self._jb.stats
        logger.debug(
            "stats: tx_pkts={} rx_pkts={} enc_err={} dec_err={} tx_sock_err={} rx_sock_err={} "
            "rx_source_drop={} rx_not_ready_drop={} rx_invalid={} rx_token={} rx_direction={} rx_route={} "
            "jb_buf={} jb_missing={} jb_late_dropped={} jb_concealed={} jb_trimmed={} jb_resets={} "
            "playout_underflows={} in_callback_errors={} out_callback_errors={}",
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
            self._jb.buffered_frames,
            jb_stats.missing,
            jb_stats.late_dropped,
            jb_stats.concealed,
            jb_stats.trimmed,
            jb_stats.resets,
            self._playout_ring.stats.underflows,
            self._input_callback_errors,
            self._output_callback_errors,
        )

    def _decode_playout_frame(self, jitter_frame: JitterFrame, decoder: OpusDecoder) -> np.ndarray:
        """Decode exactly one protocol frame, failing to silence on mismatch."""
        try:
            decoded = np.asarray(decode_jitter_frame(decoder, jitter_frame), dtype=np.float32)
            if decoded.ndim != 1 or int(decoded.shape[0]) != int(FRAME_SAMPLES):
                raise ValueError(f"decoded Opus frame has {int(decoded.size)} samples; expected {int(FRAME_SAMPLES)}")
            if not np.all(np.isfinite(decoded)):
                raise ValueError("decoded Opus frame contains non-finite samples")
            return decoded
        except Exception as e:
            self._opus_decode_errors += 1
            logger.debug("Opus playout decode rejected: {}", e)
            return np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)
