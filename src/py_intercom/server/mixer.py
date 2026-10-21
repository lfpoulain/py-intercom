"""Audio plane: UDP reception, mix-minus routing and per-client broadcast."""

from __future__ import annotations

import queue
import time

import numpy as np
from loguru import logger

from ..common.audio import apply_gain_db, limit_peak, rms_dbfs
from ..common.buses import (
    FIXED_BUS_MASK,
)
from ..common.constants import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
)
from ..common.jitter_buffer import JitterFrameMetadata, OpusPacketJitterBuffer
from ..common.opus_codec import OpusDecoder
from ..common.packets import (
    AudioPacketDirection,
    AudioPacketValidationError,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)
from ..common.routing import (
    RoutedSource,
    RoutingConfig,
    RoutingError,
    build_destination_mix,
    heard_voice_dbfs,
    route_sources,
)
from .models import ClientState, _RoutingEnvelope


class MixerMixin:
    def _count_invalid_audio_packet(self, reason: str) -> None:
        if reason == "invalid_token":
            self._rx_invalid_tokens += 1
        elif reason == "invalid_direction":
            self._rx_invalid_directions += 1
        elif reason == "invalid_route":
            self._rx_invalid_route_masks += 1
        else:
            self._rx_invalid_packets += 1

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
        try:
            mask = int(frame_bus_mask)
        except (TypeError, ValueError):
            return 0
        if mask < 0 or mask & ~int(FIXED_BUS_MASK):
            return 0
        return int(mask)

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
                    buses = {
                        bus_id: (float(bus.gain_db), bool(bus.feed_to_regie))
                        for bus_id, bus in self._buses.items()
                    }
                    clients_snapshot: list[
                        tuple[
                            int,
                            ClientState,
                            OpusPacketJitterBuffer,
                            OpusDecoder,
                            dict[int, bool],
                            bool,
                            float,
                            float,
                            bool,
                            int,
                        ]
                    ] = []
                    for client_id, st in list(self._clients.items()):
                        try:
                            clients_snapshot.append(
                                (
                                    int(client_id),
                                    st,
                                    st.jb,
                                    st.decoder,
                                    dict(st.ptt_buses),
                                    bool(st.control_connected),
                                    float(st.last_control_monotonic),
                                    float(st.input_gain_db),
                                    bool(st.applies_input_gain_locally),
                                    int(st.session_token),
                                )
                            )
                        except Exception:
                            clients_snapshot.append(
                                (
                                    int(client_id),
                                    st,
                                    st.jb,
                                    st.decoder,
                                    {},
                                    False,
                                    0.0,
                                    0.0,
                                    False,
                                    0,
                                )
                            )
                    outputs = list(self._outputs.values())
                    routing_epoch = int(self._routing_epoch)

                routed_sources: list[RoutedSource] = []
                vu_updates: dict[int, float] = {}

                for (
                    client_id,
                    _st,
                    jitter_buffer,
                    decoder,
                    ptt_buses,
                    ctrl_ok,
                    last_ctrl,
                    input_gain_db,
                    applies_input_gain_locally,
                    session_token,
                ) in clients_snapshot:
                    try:
                        jitter_frame = jitter_buffer.pop_frame()
                    except Exception:
                        jitter_frame = None
                    if jitter_frame is None:
                        if bool(ctrl_ok) and not any(bool(v) for v in ptt_buses.values()):
                            vu_updates[int(client_id)] = -60.0
                        continue
                    payload = bytes(jitter_frame.payload)
                    try:
                        frame_bus_mask = int(jitter_frame.metadata.bus_mask)
                        if int(jitter_frame.metadata.session_token) != int(session_token):
                            frame_bus_mask = 0
                    except Exception:
                        frame_bus_mask = 0
                    frame_bus_mask = self._live_frame_bus_mask(
                        frame_bus_mask,
                        bool(ctrl_ok),
                        float(last_ctrl),
                        now=now,
                    )
                    try:
                        frame = decoder.decode(payload)
                    except Exception:
                        self._rx_decode_errors += 1
                        continue

                    frame = self._normalize_decoded_frame(frame)
                    if frame is None:
                        vu_updates[int(client_id)] = -60.0
                        continue

                    if not bool(applies_input_gain_locally):
                        try:
                            frame = apply_gain_db(frame, float(input_gain_db))
                        except Exception:
                            # Keep the source at unity gain rather than drop it.
                            self._mix_gain_errors += 1

                    vu = rms_dbfs(frame)
                    vu_updates[int(client_id)] = float(vu)

                    try:
                        routed_sources.append(
                            RoutedSource(
                                client_id=int(client_id),
                                frame=frame,
                                bus_mask=int(frame_bus_mask),
                            )
                        )
                    except (RoutingError, TypeError, ValueError):
                        self._rx_invalid_frame_sizes += 1
                        vu_updates[int(client_id)] = -60.0

                with self._lock:
                    if int(self._routing_epoch) != int(routing_epoch):
                        self._stale_mix_epochs_dropped += 1
                        continue
                    if vu_updates:
                        for cid, vu in vu_updates.items():
                            st2 = self._clients.get(int(cid))
                            if st2 is not None:
                                st2.vu_dbfs = float(vu)

                routing_config = RoutingConfig(
                    bus_gain_db={bus_id: values[0] for bus_id, values in buses.items()},
                    feed_to_regie={bus_id: values[1] for bus_id, values in buses.items()},
                )
                routing_result = route_sources(routed_sources, routing_config)
                regie_monitor_mix = build_destination_mix(
                    routing_result.regie_linear,
                    None,
                    listen_regie=True,
                )

                try:
                    regie_vu_dbfs = float(rms_dbfs(regie_monitor_mix))
                except Exception:
                    regie_vu_dbfs = -60.0

                with self._lock:
                    if int(self._routing_epoch) != int(routing_epoch):
                        self._stale_mix_epochs_dropped += 1
                        continue
                    self._regie_vu_dbfs = float(regie_vu_dbfs)
                    self._enqueue_latest_mix(
                        _RoutingEnvelope(
                            routing_epoch=int(routing_epoch),
                            result=routing_result,
                        )
                    )

                    for out in outputs:
                        linear_mix = routing_result.bus_frames.get(int(out.bus_id))
                        if linear_mix is None:
                            linear_mix = np.zeros((FRAME_SAMPLES,), dtype=np.float32)
                        # A physical output is a destination: preserve the shared
                        # bus sum and limit only the frame copied into this output.
                        mix = limit_peak(np.array(linear_mix, dtype=np.float32, copy=True))
                        out.ring.push(mix.astype(np.float32, copy=False))

    def _accept_udp_packet(self, pkt, addr: tuple[str, int], *, now: float | None = None) -> bool:
        received_at = time.monotonic() if now is None else float(now)
        if int(getattr(pkt, "flags", 0)) != int(AudioPacketDirection.CLIENT_TO_SERVER):
            self._rx_invalid_directions += 1
            return False
        client_id = int(pkt.client_id) & 0xFFFFFFFF
        seq = int(getattr(pkt, "sequence_number", 0)) & 0xFFFFFFFF
        payload = bytes(getattr(pkt, "payload", b""))
        stream_id = int(getattr(pkt, "stream_id", -1))
        bus_mask = int(getattr(pkt, "bus_mask", -1))
        if stream_id != 0 or bus_mask < 0 or bus_mask & ~int(FIXED_BUS_MASK):
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
                self._expire_client_session(st)
                return False

            session_token = int(getattr(pkt, "session_token", 0))
            if (
                session_token == 0
                or int(st.session_token) == 0
                or session_token != int(st.session_token)
            ):
                self._rx_invalid_tokens += 1
                return False

            try:
                expected_addr = (str(st.addr[0]), int(st.addr[1]))
                source_addr = (str(addr[0]), int(addr[1]))
            except (IndexError, TypeError, ValueError):
                self._rx_wrong_source += 1
                return False

            # The UDP endpoint is declared by the TCP hello. UDP is never
            # allowed to learn or overwrite it, otherwise a forged datagram can
            # hijack both the jitter stream and return audio.
            if expected_addr[1] <= 0 or source_addr != expected_addr:
                self._rx_wrong_source += 1
                return False

            try:
                if received_at - float(st.last_packet_monotonic) > 2.0:
                    st.jb.reset()
            except (TypeError, ValueError):
                st.jb.reset()

            st.last_packet_monotonic = received_at
            st.last_timestamp_ms = int(pkt.timestamp_ms)
            st.last_sequence_number = int(pkt.sequence_number)
            st.jb.push(
                seq,
                payload,
                JitterFrameMetadata(
                    session_token=session_token,
                    client_id=client_id,
                    timestamp_ms=int(pkt.timestamp_ms),
                    stream_id=stream_id,
                    bus_mask=bus_mask,
                    flags=int(pkt.flags),
                ),
            )
            self._rx_packets += 1
            self._last_valid_udp_monotonic = received_at
            return True

    def _active_broadcast_destinations(self, *, now: float | None = None) -> list[tuple]:
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
            return destinations

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError as e:
                self._rx_socket_errors += 1
                # WinError 10054 (WSAECONNRESET): Windows reports ICMP
                # "port unreachable" when a previous sendto() targeted a
                # closed client port.  This is harmless — just retry
                # immediately so we don't block reception from other clients.
                err_code = getattr(e, 'winerror', None) or getattr(e, 'errno', None)
                if err_code == 10054:
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
                self._count_invalid_audio_packet(e.reason)
                logger.warning("bad packet from {}: {}", addr, e)
                continue
            except Exception as e:
                self._rx_invalid_packets += 1
                logger.warning("bad packet from {}: {}", addr, e)
                continue

            try:
                self._accept_udp_packet(pkt, addr)
            except Exception as e:
                self._rx_decode_errors += 1
                logger.debug("UDP packet rejected after parsing from {}: {}", addr, e)

            now2 = time.monotonic()
            if now2 - self._last_stats_log >= 5.0:
                self._last_stats_log = now2
                with self._lock:
                    clients = len(self._clients)
                    mix_q = self._mix_queue.qsize()
                logger.debug(
                    "stats: clients={} rx_pkts={} tx_pkts={} rx_dec_err={} rx_unknown={} rx_inactive={} "
                    "rx_wrong_source={} rx_invalid={} rx_token={} rx_direction={} rx_route={} tx_enc_err={} tx_sock_err={} mix_q={}",
                    clients,
                    self._rx_packets,
                    self._tx_packets,
                    self._rx_decode_errors,
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

            if not isinstance(item, _RoutingEnvelope):
                continue
            routing_epoch = int(item.routing_epoch)
            with self._lock:
                if routing_epoch != int(self._routing_epoch):
                    self._stale_mix_epochs_dropped += 1
                    continue
            raw_mix = item.result.regie_linear
            contrib = item.result.regie_contribution_by_client

            ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
            self._seq_out = (self._seq_out + 1) & 0xFFFFFFFF

            dests = self._active_broadcast_destinations()
            with self._lock:
                if routing_epoch != int(self._routing_epoch):
                    self._stale_mix_epochs_dropped += 1
                    continue

            return_frame = _zero_frame
            if bool(self._return_enabled):
                if self._return_frames.pop_into(_return_scratch) is not None:
                    if bool(np.isfinite(_return_scratch).all()):
                        return_frame = _return_scratch

            for client_id, addr, enc, listen_return, listen_regie, return_gain_db, session_token, ducker in dests:
                try:
                    udp_port = int(addr[1])
                except Exception:
                    udp_port = 0
                if udp_port <= 0:
                    # The TCP hello did not declare a valid UDP endpoint. Log
                    # once per client instead of silently dropping forever.
                    if not self._silent_dest_logged.get(int(client_id), False):
                        self._silent_dest_logged[int(client_id)] = True
                        logger.info(
                            "client {} declared no valid UDP port; broadcast skipped",
                            int(client_id),
                        )
                    continue
                else:
                    if self._silent_dest_logged.get(int(client_id), False):
                        self._silent_dest_logged.pop(int(client_id), None)
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
