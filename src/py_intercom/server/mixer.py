"""Audio plane: UDP reception, mix-minus routing and per-client broadcast."""

from __future__ import annotations

import queue
import time
from typing import NamedTuple

import numpy as np
from loguru import logger

from ..common.audio import apply_gain_db, rms_dbfs
from ..common.buses import (
    FIXED_BUS_MASK,
)
from ..common.constants import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
)
from ..common.jitter_buffer import (
    JitterFrameMetadata,
    OpusPacketJitterBuffer,
    decode_jitter_frame,
    skip_silent_slot,
)
from ..common.opus_codec import OpusDecoder
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
from ..common.routing import (
    RoutedSource,
    RoutingConfig,
    RoutingError,
    RoutingResult,
    build_destination_mix,
    heard_voice_dbfs,
    route_sources,
)
from .models import ClientState

# Windows reports these per datagram: the socket itself is still healthy.
# A client meter with no frame this tick falls at the same rate as the
# client's own bus meters, instead of freezing on its last value.
_VU_RELEASE_DB_PER_FRAME = 3.0


class _MixSource(NamedTuple):
    """What the mix worker needs from one client, snapshotted under the lock.

    A NamedTuple: built for every client on every tick while the engine lock
    is held, so it must be as cheap as a plain tuple.
    """

    client_id: int
    state: ClientState
    jitter: OpusPacketJitterBuffer
    decoder: OpusDecoder
    control_connected: bool
    last_control_monotonic: float
    input_gain_db: float
    applies_input_gain_locally: bool
    session_token: int


class MixerMixin:
    @classmethod
    def _live_frame_bus_mask(
        cls,
        frame_bus_mask: int,
        control_connected: bool,
        last_control_monotonic: float,
        *,
        now: float | None = None,
    ) -> int:
        # Routing is carried atomically by this audio frame. TCP liveness is a
        # second fail-closed gate, never the source of the route itself.
        if not cls._control_is_live(control_connected, last_control_monotonic, now=now):
            return 0
        mask = int(frame_bus_mask)
        if mask < 0 or mask & ~int(FIXED_BUS_MASK):
            return 0
        return mask

    def _normalize_decoded_frame(self, frame: object) -> np.ndarray | None:
        try:
            normalized = np.asarray(frame, dtype=np.float32)
        except Exception:
            self._rx_invalid_frame_sizes += 1
            return None
        if normalized.ndim != 1 or int(normalized.shape[0]) != int(FRAME_SAMPLES):
            self._rx_invalid_frame_sizes += 1
            return None
        if not bool(np.isfinite(normalized).all()):
            normalized = np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=-1.0)
        return normalized.astype(np.float32, copy=False)

    def _enqueue_latest_mix(self, item: object) -> None:
        try:
            self._mix_queue.put_nowait(item)
            return
        except queue.Full:
            pass

        # Realtime policy: discard stale audio, never the newest frame.
        try:
            self._mix_queue.get_nowait()
            self._mix_queue_drops += 1
        except queue.Empty:
            pass
        try:
            self._mix_queue.put_nowait(item)
        except queue.Full:
            # A consumer/producer race is harmless; account for the dropped
            # newest item if the queue filled again before this retry.
            self._mix_queue_drops += 1

    def _frame_bus_mask(self, source: _MixSource, jitter_frame, now: float) -> int:
        """Live bus route of one playout slot of a client."""
        if jitter_frame.concealed:
            # Deep PLC continues the last real frame, so it inherits that
            # frame's route, never one from another session.
            route_token, route_mask = source.state.last_frame_route
            bus_mask = route_mask if route_token == source.session_token else 0
        else:
            metadata = jitter_frame.metadata
            bus_mask = int(metadata.bus_mask) if int(metadata.session_token) == source.session_token else 0
            source.state.last_frame_route = (source.session_token, bus_mask)
        return self._live_frame_bus_mask(
            bus_mask,
            source.control_connected,
            source.last_control_monotonic,
            now=now,
        )

    def _pull_mix_source(self, source: _MixSource, *, now: float) -> tuple[RoutedSource | None, float | None]:
        """Decode the next frame of one client and attach its live bus route.

        Returns the routed source (None when there is nothing to mix) and the
        client's new meter level (None: no frame this tick, let it fall).
        """
        try:
            jitter_frame = source.jitter.pop_frame()
        except Exception as exc:
            self._mix_pop_errors += 1
            logger.debug("jitter pop failed for client {}: {}", source.client_id, exc)
            return None, None
        if jitter_frame is None:
            return None, None

        bus_mask = self._frame_bus_mask(source, jitter_frame, now)

        try:
            decoded = decode_jitter_frame(source.decoder, jitter_frame)
            # Extra depth left by a stall goes away in a pause between words.
            played_frame, decoded = skip_silent_slot(
                source.jitter,
                jitter_frame,
                decoded,
                lambda following: decode_jitter_frame(source.decoder, following),
            )
            if played_frame is not jitter_frame:
                bus_mask = self._frame_bus_mask(source, played_frame, now)
        except Exception as exc:
            self._rx_decode_errors += 1
            logger.debug("Opus decode failed for client {}: {}", source.client_id, exc)
            return None, None
        frame = self._normalize_decoded_frame(decoded)
        if frame is None:
            return None, -60.0

        if not source.applies_input_gain_locally:
            frame = apply_gain_db(frame, source.input_gain_db)
        try:
            routed = RoutedSource(client_id=source.client_id, frame=frame, bus_mask=bus_mask)
        except (RoutingError, TypeError, ValueError):
            self._rx_invalid_frame_sizes += 1
            return None, -60.0
        return routed, rms_dbfs(frame)

    def _mix_loop(self) -> None:
        tick_s = float(FRAME_SAMPLES) / float(SAMPLE_RATE)
        next_t = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(0.005, next_t - now))
                continue

            # Resync after a clock jump (system suspend/resume, GC pause, freeze).
            # Without this, the loop would burn CPU producing thousands of stale
            # frames trying to "catch up" several seconds of missed ticks.
            if now > next_t + 0.1:
                logger.debug("mix loop resync: skipping {:.0f} ms of missed ticks", (now - next_t) * 1000.0)
                next_t = now

            produced = 0
            while now >= next_t and produced < 5 and not self._stop.is_set():
                next_t += tick_s
                produced += 1

                with self._lock:
                    bus_gain_db = {bus_id: float(bus.gain_db) for bus_id, bus in self._buses.items()}
                    feed_to_regie = {bus_id: bool(bus.feed_to_regie) for bus_id, bus in self._buses.items()}
                    sources = [
                        _MixSource(
                            client_id=int(client_id),
                            state=st,
                            jitter=st.jb,
                            decoder=st.decoder,
                            control_connected=bool(st.control_connected),
                            last_control_monotonic=float(st.last_control_monotonic),
                            input_gain_db=float(st.input_gain_db),
                            applies_input_gain_locally=bool(st.applies_input_gain_locally),
                            session_token=int(st.session_token),
                        )
                        for client_id, st in self._clients.items()
                    ]
                    outputs = list(self._outputs.values())

                routed_sources: list[RoutedSource] = []
                vu_updates: dict[int, float | None] = {}
                for source in sources:
                    routed, vu = self._pull_mix_source(source, now=now)
                    if routed is not None:
                        routed_sources.append(routed)
                    vu_updates[source.client_id] = vu

                with self._lock:
                    for cid, vu in vu_updates.items():
                        st2 = self._clients.get(int(cid))
                        if st2 is None:
                            continue
                        st2.vu_dbfs = (
                            float(vu) if vu is not None else max(-60.0, float(st2.vu_dbfs) - _VU_RELEASE_DB_PER_FRAME)
                        )

                routing_result = route_sources(
                    routed_sources,
                    RoutingConfig(bus_gain_db=bus_gain_db, feed_to_regie=feed_to_regie),
                )
                regie_vu_dbfs = rms_dbfs(build_destination_mix(routing_result.regie_linear, None, listen_regie=True))

                with self._lock:
                    self._regie_vu_dbfs = float(regie_vu_dbfs)
                    # Bus level after its fader, whether or not an output plays
                    # it. Updated in place: the mixer allocates nothing here.
                    for bus_id, bus_frame in routing_result.bus_frames.items():
                        self._bus_vu_dbfs[bus_id] = rms_dbfs(bus_frame)
                    self._enqueue_latest_mix(routing_result)

                    # Physical outputs get the linear bus sum (the ring copies
                    # it). Each output callback applies its own gain, then the
                    # final limiter, so a gain above 0 dB never hard-clips.
                    for out in outputs:
                        out.ring.push(routing_result.bus_frames[int(out.bus_id)])

    def _accept_udp_packet(self, pkt, addr: tuple[str, int], *, now: float | None = None) -> bool:
        received_at = time.monotonic() if now is None else float(now)
        if int(getattr(pkt, "flags", 0)) != int(AudioPacketDirection.CLIENT_TO_SERVER):
            self._rx_invalid_directions += 1
            return False
        client_id = int(pkt.client_id) & 0xFFFFFFFF
        seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
        payload = bytes(getattr(pkt, "payload", b""))
        bus_mask = int(getattr(pkt, "bus_mask", -1))
        if not is_valid_route(int(getattr(pkt, "stream_id", -1)), bus_mask):
            self._rx_invalid_route_masks += 1
            return False

        with self._lock:
            st = self._clients.get(int(client_id))
            if st is None:
                self._rx_unknown_clients += 1
                return False

            if not self._control_is_live(
                st.control_connected,
                st.last_control_monotonic,
                now=received_at,
            ):
                self._rx_inactive_clients += 1
                # Revoke a session once. Datagrams still in flight from an
                # already expired client must not flush everybody's audio.
                if st.control_connected:
                    self._expire_client_session(st)
                return False

            session_token = int(getattr(pkt, "session_token", 0))
            if session_token == 0 or int(st.session_token) == 0 or session_token != int(st.session_token):
                self._rx_invalid_tokens += 1
                return False

            # The UDP endpoint is declared by the TCP hello. UDP is never
            # allowed to learn or overwrite it, otherwise a forged datagram can
            # hijack both the jitter stream and return audio.
            if (str(addr[0]), int(addr[1])) != (str(st.addr[0]), int(st.addr[1])):
                self._rx_wrong_source += 1
                return False

            if received_at - float(st.last_packet_monotonic) > 2.0:
                st.jb.reset()

            st.last_packet_monotonic = received_at
            st.last_timestamp_ms = int(pkt.timestamp_ms)
            st.last_sequence_number = int(pkt.sequence_number)
            st.jb.push(seq, payload, JitterFrameMetadata(session_token=session_token, bus_mask=bus_mask))
            self._rx_packets += 1
            self._last_valid_udp_monotonic = received_at
            return True

    def _active_broadcast_destinations(self, *, now: float | None = None) -> list[tuple]:
        return self._broadcast_snapshot(now=now)[0]

    def _broadcast_snapshot(self, *, now: float | None = None) -> tuple[list[tuple], int]:
        """Live destinations, and the routing epoch they are valid for."""
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            destinations = []
            for client_id, st in self._clients.items():
                if not self._control_is_live(
                    st.control_connected,
                    st.last_control_monotonic,
                    now=current,
                ):
                    if st.control_connected:
                        self._expire_client_session(st)
                    continue
                if int(st.session_token) == 0:
                    continue
                destinations.append(
                    (
                        client_id,
                        st.addr,
                        st.encoder,
                        bool(st.listen_return_bus),
                        bool(st.listen_regie),
                        float(st.return_gain_db),
                        int(st.session_token),
                        st.ducker if st.duck_return else None,
                    )
                )
            return destinations, int(self._routing_epoch)

    def _log_bad_packet(self, addr, error: object) -> None:
        # A misconfigured device or a stray stream on the audio port can send
        # hundreds of datagrams per second: one line per source and window.
        note = self._bad_packet_log.allow(str(addr[0]), now=time.monotonic())
        if note is not None:
            logger.warning("bad packet from {}: {}{}", addr, error, note)

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
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
                # Per-datagram reports: never pause reception of every client.
                if err_code in (WSAENETRESET, WSAECONNRESET):
                    continue
                logger.debug("_rx_loop OSError: {} sock_err_count={}", e, self._rx_socket_errors)
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue

            self._rx_datagrams += 1
            self._rx_bytes += len(data)

            try:
                pkt = unpack_audio_packet_v2(data)
            except AudioPacketValidationError as e:
                count_rejected_datagram(self, e.reason)
                self._log_bad_packet(addr, e)
                continue
            except Exception as e:
                self._rx_invalid_packets += 1
                self._log_bad_packet(addr, e)
                continue

            try:
                self._accept_udp_packet(pkt, addr)
            except Exception as e:
                self._rx_accept_errors += 1
                logger.debug("UDP packet rejected after parsing from {}: {}", addr, e)

            now2 = time.monotonic()
            if now2 - self._last_stats_log >= 5.0:
                self._last_stats_log = now2
                with self._lock:
                    clients = len(self._clients)
                    mix_q = self._mix_queue.qsize()
                logger.debug(
                    "stats: clients={} rx_pkts={} tx_pkts={} rx_dec_err={} rx_accept_err={} rx_unknown={} "
                    "rx_inactive={} rx_wrong_source={} rx_invalid={} rx_token={} rx_direction={} rx_route={} "
                    "tx_enc_err={} tx_sock_err={} mix_q={}",
                    clients,
                    self._rx_packets,
                    self._tx_packets,
                    self._rx_decode_errors,
                    self._rx_accept_errors,
                    self._rx_unknown_clients,
                    self._rx_inactive_clients,
                    self._rx_wrong_source,
                    self._rx_invalid_packets,
                    self._rx_invalid_tokens,
                    self._rx_invalid_directions,
                    self._rx_invalid_route_masks,
                    self._tx_encode_errors,
                    self._tx_socket_errors,
                    mix_q,
                )

    def _broadcast_loop(self) -> None:
        _zero_frame = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
        _return_scratch = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
        while not self._stop.is_set():
            try:
                item = self._mix_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if not isinstance(item, RoutingResult):
                continue
            raw_mix = item.regie_linear
            contrib = item.regie_contribution_by_client

            ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
            self._seq_out = (self._seq_out + 1) & 0xFFFFFFFF

            dests, routing_epoch = self._broadcast_snapshot()

            # The Return ring only ever holds limited, finite frames.
            return_frame = _zero_frame
            if bool(self._return_enabled) and self._return_frames.pop_into(_return_scratch) is not None:
                return_frame = _return_scratch

            for client_id, addr, enc, listen_return, listen_regie, return_gain_db, session_token, ducker in dests:
                try:
                    c = contrib.get(int(client_id))
                    if ducker is not None and listen_return:
                        return_gain_db += ducker.update(heard_voice_dbfs(raw_mix, c, listen_regie=bool(listen_regie)))
                    mix_minus = build_destination_mix(
                        raw_mix,
                        c,
                        listen_regie=bool(listen_regie),
                        return_frame=return_frame,
                        listen_return=bool(listen_return),
                        return_gain_db=float(return_gain_db),
                    )

                    payload = enc.encode(mix_minus)
                except Exception as e:
                    self._tx_encode_errors += 1
                    logger.debug("broadcast encode failed for {}: {}", addr, e)
                    continue
                try:
                    pkt = pack_audio_packet_v2(
                        session_token=int(session_token),
                        client_id=int(client_id),
                        timestamp_ms=ts_ms,
                        sequence_number=self._seq_out,
                        stream_id=0,
                        bus_mask=0,
                        direction=AudioPacketDirection.SERVER_TO_CLIENT,
                        payload=payload,
                    )
                    stale_epoch = False
                    # Keep the final validation and OS send indivisible with
                    # respect to session revocation. Encoding and packet
                    # construction deliberately stay outside this short gate.
                    with self._routing_io_lock:
                        if routing_epoch != int(self._routing_epoch):
                            stale_epoch = True
                        else:
                            self._sock.sendto(pkt, addr)
                            self._tx_packets += 1
                    if stale_epoch:
                        self._stale_mix_epochs_dropped += 1
                        break
                except Exception as e:
                    self._tx_socket_errors += 1
                    logger.debug("broadcast send failed to {}: {}", addr, e)
