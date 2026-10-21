"""TCP control plane: hello/welcome handshake, client state and config pushes."""

from __future__ import annotations

import socket
import threading
import time

import numpy as np
from loguru import logger

from ..common import opus_codec
from ..common.buses import (
    FIXED_BUS_IDS,
    FIXED_BUSES,
)
from ..common.constants import (
    CONTROL_PORT_OFFSET,
    JB_MAX_FRAMES,
    JB_START_FRAMES,
    MAX_GAIN_DB,
)
from ..common.control_protocol import (
    CONTROL_HELLO_TIMEOUT_S,
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_SESSIONS,
    ControlProtocolError,
    JsonLineBuffer,
    encode_control_message,
    format_session_token,
    require_control_protocol_version,
)
from ..common.identity import (
    ClientIdentityError,
    validate_client_identity,
)
from ..common.jitter_buffer import OpusPacketJitterBuffer
from ..common.logging import best_effort
from .models import ClientProfile, ClientState


class ControlMixin:
    def _control_send(self, sock: socket.socket, msg: dict) -> None:
        sock.sendall(encode_control_message(msg))

    def _control_send_to_client(self, client_id: int, sock: socket.socket, msg: dict) -> None:
        with self._ctrl_lock:
            send_lock = self._ctrl_send_locks.get(int(client_id))
        if send_lock is None:
            self._control_send(sock, msg)
            return
        with send_lock:
            self._control_send(sock, msg)

    def _control_push_all_configs(self) -> None:
        with self._lock:
            client_ids = [int(cid) for cid in self._clients.keys()]
        for cid in client_ids:
            self._control_push_config(int(cid))

    def _control_push_config(self, client_id: int) -> None:
        with self._lock:
            st = self._clients.get(int(client_id))
            if st is None:
                return
            cfg = {
                "client_id": int(client_id),
                "input_gain_db": float(st.input_gain_db),
                "return_vu_dbfs": float(self._return_vu_dbfs),
                "buses": [
                    {
                        "bus_id": int(definition.bus_id),
                        "name": str(definition.name),
                        "gain_db": float(bus.gain_db),
                        "feed_to_regie": bool(bus.feed_to_regie),
                    }
                    for definition in FIXED_BUSES
                    if (bus := self._buses.get(definition.bus_id)) is not None
                ],
            }

        with self._ctrl_lock:
            sock = self._ctrl_sessions.get(int(client_id))
        if sock is None:
            return
        try:
            self._control_send_to_client(int(client_id), sock, {"type": "update", "config": cfg})
        except Exception as e:
            logger.debug("control push config failed for {}: {}", int(client_id), e)
            with best_effort("sock.close()"):
                sock.close()
            with self._ctrl_lock:
                if self._ctrl_sessions.get(int(client_id)) is sock:
                    self._ctrl_sessions.pop(int(client_id), None)
                    self._ctrl_send_locks.pop(int(client_id), None)
                    with self._lock:
                        st2 = self._clients.get(int(client_id))
                        if st2 is not None:
                            self._expire_client_session(st2)

    def _open_control_socket(self) -> socket.socket:
        port = int(self.port) + int(CONTROL_PORT_OFFSET)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.bind_ip, port))
            s.listen(min(32, int(MAX_CONTROL_SESSIONS)))
            s.settimeout(0.5)
            self._ctrl_sock = s
            logger.info("control listening on {}:{}", self.bind_ip, port)
            return s
        except Exception:
            with best_effort("s.close()"):
                s.close()
            raise

    def _ctrl_accept_loop(self) -> None:
        s = self._ctrl_sock
        if s is None:
            try:
                s = self._open_control_socket()
            except Exception as e:
                logger.error("control socket failed to start: {}", e)
                self._stop.set()
                return

        while not self._stop.is_set():
            try:
                conn, addr = s.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if self._stop.is_set():
                with best_effort("conn.close()"):
                    conn.close()
                return
            reject = False
            with self._worker_lock:
                self._ctrl_session_threads = {
                    thread for thread in self._ctrl_session_threads if thread.is_alive()
                }
                if len(self._ctrl_session_threads) >= int(MAX_CONTROL_SESSIONS):
                    self._ctrl_rejected_sessions += 1
                    reject = True
                else:
                    t = threading.Thread(
                        target=self._ctrl_session_entry,
                        args=(conn, addr),
                        name=f"ctrl-session-{addr[0]}:{addr[1]}",
                        daemon=True,
                    )
                    self._ctrl_session_threads.add(t)
            if reject:
                with best_effort("conn.close()"):
                    conn.close()
                continue
            try:
                t.start()
            except Exception:
                with self._worker_lock:
                    self._ctrl_session_threads.discard(t)
                with best_effort("conn.close()"):
                    conn.close()
                raise

    def _ctrl_session_entry(self, conn: socket.socket, addr) -> None:
        try:
            self._ctrl_session_loop(conn, addr)
        except Exception:
            logger.exception("control session crashed for {}", addr)
        finally:
            with self._worker_lock:
                self._ctrl_session_threads.discard(threading.current_thread())

    def _ctrl_session_loop(self, conn: socket.socket, addr) -> None:
        conn.settimeout(0.5)
        parser = JsonLineBuffer()
        client_id: int | None = None
        session_token = 0
        hello_deadline = time.monotonic() + float(CONTROL_HELLO_TIMEOUT_S)

        try:
            while not self._stop.is_set():
                if client_id is None and time.monotonic() >= hello_deadline:
                    self._ctrl_hello_timeouts += 1
                    raise ControlProtocolError(
                        f"hello not received within {float(CONTROL_HELLO_TIMEOUT_S):.1f}s"
                    )
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                except TimeoutError:
                    continue
                except Exception:
                    break

                for msg in parser.feed(chunk):
                    mtype = str(msg.get("type") or "").lower()
                    if client_id is None and mtype != "hello":
                        raise ControlProtocolError(
                            f"expected hello as first control message, received {mtype or '<missing type>'}"
                        )
                    if client_id is not None and mtype == "hello":
                        raise ControlProtocolError("received duplicate hello")

                    if client_id is not None:
                        with self._ctrl_lock:
                            if self._ctrl_sessions.get(int(client_id)) is not conn:
                                # This socket was superseded by a newer hello for
                                # the same client. It no longer owns any state.
                                return
                        with self._lock:
                            owned_state = self._clients.get(int(client_id))
                            if (
                                owned_state is None
                                or int(session_token) == 0
                                or int(owned_state.session_token) != int(session_token)
                            ):
                                raise ControlProtocolError(
                                    "control session token expired; a new hello is required"
                                )
                    if mtype == "ping":
                        if client_id is not None:
                            now = time.monotonic()
                            with self._lock:
                                st = self._clients.get(int(client_id))
                                if (
                                    st is None
                                    or int(st.session_token) != int(session_token)
                                ):
                                    return
                                st.control_connected = True
                                st.last_control_monotonic = now
                        try:
                            with self._lock:
                                return_vu_dbfs = float(self._return_vu_dbfs)
                            pong_msg = {
                                "type": "pong",
                                "t": msg.get("t"),
                                "return_vu_dbfs": float(return_vu_dbfs),
                            }
                            if client_id is None:
                                self._control_send(conn, pong_msg)
                            else:
                                self._control_send_to_client(int(client_id), conn, pong_msg)
                        except Exception as e:
                            logger.debug("pong send failed: {}", e)
                        continue

                    if mtype == "state":
                        cid = int(client_id)
                        raw_ptt_buses = msg.get("ptt_buses")
                        parsed_ptt_buses: dict[int, bool] | None = None
                        if raw_ptt_buses is not None:
                            if not isinstance(raw_ptt_buses, dict):
                                raise ControlProtocolError("state ptt_buses must be an object")
                            parsed_ptt_buses = {}
                            for raw_bus_id, raw_active in raw_ptt_buses.items():
                                if isinstance(raw_bus_id, bool):
                                    raise ControlProtocolError("state bus id must be an integer")
                                try:
                                    bus_id = int(raw_bus_id)
                                except (TypeError, ValueError) as exc:
                                    raise ControlProtocolError(
                                        "state bus id must be an integer"
                                    ) from exc
                                if bus_id not in FIXED_BUS_IDS:
                                    raise ControlProtocolError("state contains an unknown bus id")
                                if not isinstance(raw_active, bool):
                                    raise ControlProtocolError(
                                        "state PTT values must be booleans"
                                    )
                                parsed_ptt_buses[int(bus_id)] = bool(raw_active)

                        parsed_flags: dict[str, bool | None] = {}
                        for field_name in (
                            "listen_return_bus",
                            "listen_regie",
                            "applies_input_gain_locally",
                        ):
                            raw_value = msg.get(field_name)
                            if raw_value is not None and not isinstance(raw_value, bool):
                                raise ControlProtocolError(
                                    f"state {field_name} must be a boolean"
                                )
                            parsed_flags[field_name] = raw_value

                        parsed_gains: dict[str, float | None] = {}
                        for field_name in ("return_gain_db", "input_gain_db"):
                            raw_value = msg.get(field_name)
                            if raw_value is None:
                                parsed_gains[field_name] = None
                                continue
                            if isinstance(raw_value, bool) or not isinstance(
                                raw_value, (int, float)
                            ):
                                raise ControlProtocolError(
                                    f"state {field_name} must be a finite number"
                                )
                            gain = float(raw_value)
                            if not bool(np.isfinite(gain)):
                                raise ControlProtocolError(
                                    f"state {field_name} must be a finite number"
                                )
                            parsed_gains[field_name] = max(
                                -60.0,
                                min(float(MAX_GAIN_DB), gain),
                            )

                        listen_return_bus = parsed_flags["listen_return_bus"]
                        listen_regie = parsed_flags["listen_regie"]
                        applies_input_gain_locally = parsed_flags[
                            "applies_input_gain_locally"
                        ]
                        return_gain_db = parsed_gains["return_gain_db"]
                        input_gain_db = parsed_gains["input_gain_db"]
                        changed = False
                        # Fires on every PTT toggle / slider move; keep at TRACE to avoid log spam.
                        logger.trace("state from cid={}: listen_return_bus={} listen_regie={}", cid, listen_return_bus, listen_regie)
                        with self._lock:
                            st = self._clients.get(int(cid))
                            if (
                                st is None
                                or int(st.session_token) != int(session_token)
                            ):
                                return
                            if st is not None:
                                if parsed_ptt_buses is not None:
                                    st.ptt_buses = dict(parsed_ptt_buses)
                                if listen_return_bus is not None:
                                    st.listen_return_bus = listen_return_bus
                                if listen_regie is not None:
                                    st.listen_regie = listen_regie
                                if return_gain_db is not None:
                                    st.return_gain_db = float(return_gain_db)
                                if input_gain_db is not None:
                                    next_input_gain = float(input_gain_db)
                                    if abs(float(st.input_gain_db) - float(next_input_gain)) >= 0.01:
                                        st.input_gain_db = float(next_input_gain)
                                        u = str(st.client_uuid or "")
                                        if u:
                                            profile = self._client_profiles.get(u)
                                            if profile is None:
                                                profile = ClientProfile(
                                                    client_uuid=u,
                                                    name=str(st.name or ""),
                                                )
                                                self._client_profiles[u] = profile
                                            profile.input_gain_db = float(next_input_gain)
                                            if st.name:
                                                profile.name = str(st.name)
                                        changed = True
                                if applies_input_gain_locally is not None:
                                    next_applies = applies_input_gain_locally
                                    if bool(st.applies_input_gain_locally) != bool(next_applies):
                                        st.applies_input_gain_locally = bool(next_applies)
                                        changed = True

                                st.control_connected = True
                                st.last_control_monotonic = time.monotonic()
                        if changed:
                            self._autosave_preset()
                        continue

                    if mtype != "hello":
                        continue

                    require_control_protocol_version(msg.get("version"))

                    try:
                        client_id, client_uuid = validate_client_identity(
                            msg.get("client_id"),
                            msg.get("client_uuid"),
                        )
                    except ClientIdentityError as exc:
                        self._ctrl_invalid_identities += 1
                        raise ControlProtocolError(f"invalid client identity: {exc}") from exc

                    raw_name = msg.get("name", "")
                    if not isinstance(raw_name, str):
                        raise ControlProtocolError("hello name must be a string")
                    name = raw_name.strip()
                    if len(name) > 128:
                        raise ControlProtocolError("hello name exceeds 128 characters")
                    raw_applies_input_gain_locally = msg.get(
                        "applies_input_gain_locally",
                        False,
                    )
                    if not isinstance(raw_applies_input_gain_locally, bool):
                        raise ControlProtocolError(
                            "hello applies_input_gain_locally must be a boolean"
                        )
                    applies_input_gain_locally = raw_applies_input_gain_locally
                    raw_udp_port = msg.get("udp_port")
                    if isinstance(raw_udp_port, bool) or not isinstance(raw_udp_port, int):
                        raise ControlProtocolError("hello udp_port must be an integer")
                    if not 1 <= raw_udp_port <= 65535:
                        raise ControlProtocolError("hello udp_port must be between 1 and 65535")
                    udp_port = int(raw_udp_port)

                    with self._lock:
                        identity_conflicts = self._identity_conflicts_locked(
                            int(client_id),
                            client_uuid,
                        )
                    if identity_conflicts:
                        self._ctrl_identity_collisions += 1
                        raise ControlProtocolError(
                            "client identity collides with existing UUID(s): "
                            + ", ".join(identity_conflicts)
                        )

                    # Construct a complete fresh codec epoch before revoking a
                    # live session. A transient allocation/DLL failure must not
                    # cut audio that is currently working or reuse predictive
                    # state from the previous session.
                    try:
                        next_decoder = opus_codec.OpusDecoder()
                        next_encoder = opus_codec.OpusEncoder()
                        next_jitter = OpusPacketJitterBuffer(
                            start_frames=JB_START_FRAMES,
                            max_frames=JB_MAX_FRAMES,
                        )
                    except Exception as exc:
                        self._ctrl_codec_init_errors += 1
                        raise ControlProtocolError(
                            f"failed to initialize a fresh audio session: {exc}"
                        ) from exc

                    session_token = self._new_session_token()

                    now = time.monotonic()
                    profile_changed = False
                    prev = None
                    # Control ownership and its audio epoch are one atomic
                    # commit. A second concurrent hello can therefore observe
                    # either the complete old session or the complete new one,
                    # never an owner socket paired with another token/codec.
                    # The project-wide lock order is control -> server state.
                    with self._ctrl_lock:
                        with self._lock:
                            if self._stop.is_set():
                                raise ControlProtocolError("server is stopping")

                            # Codec construction happens outside the locks, so
                            # revalidate identity immediately before commit.
                            identity_conflicts = self._identity_conflicts_locked(
                                int(client_id),
                                client_uuid,
                            )
                            if identity_conflicts:
                                self._ctrl_identity_collisions += 1
                                raise ControlProtocolError(
                                    "client identity collides with existing UUID(s): "
                                    + ", ".join(identity_conflicts)
                                )

                            profile = self._client_profiles.get(client_uuid) if client_uuid else None
                            if client_uuid and profile is None:
                                profile = ClientProfile(
                                    client_uuid=client_uuid,
                                    name=str(name or ""),
                                    input_gain_db=0.0,
                                )
                                self._client_profiles[client_uuid] = profile
                                self._refresh_identity_collisions_locked()
                                profile_changed = True
                            elif profile is not None and name and str(profile.name) != name:
                                profile.name = name
                                profile_changed = True

                            st = self._clients.get(int(client_id))
                            if st is None:
                                st = ClientState(
                                    addr=(str(addr[0]), 0),
                                    last_packet_monotonic=now,
                                    decoder=next_decoder,
                                    encoder=next_encoder,
                                    jb=next_jitter,
                                )
                                self._clients[int(client_id)] = st
                            else:
                                st.decoder = next_decoder
                                st.encoder = next_encoder
                                st.jb = next_jitter
                            st.last_timestamp_ms = 0
                            st.last_sequence_number = 0
                            st.name = str(name or (profile.name if profile is not None else ""))
                            st.client_uuid = client_uuid
                            st.input_gain_db = float(profile.input_gain_db) if profile is not None else 0.0
                            st.applies_input_gain_locally = bool(applies_input_gain_locally)
                            st.ptt_buses = {}
                            st.vu_dbfs = -60.0
                            st.addr = (str(addr[0]), int(udp_port) if udp_port is not None else 0)
                            st.control_connected = True
                            st.session_token = int(session_token)
                            st.last_control_monotonic = now
                            st.last_packet_monotonic = now
                            self._advance_routing_epoch_locked()

                            prev = self._ctrl_sessions.get(int(client_id))
                            self._ctrl_sessions[int(client_id)] = conn
                            self._ctrl_send_locks.setdefault(int(client_id), threading.Lock())

                    if prev is not None and prev is not conn:
                        with best_effort("prev.close()"):
                            prev.close()

                    if profile_changed:
                        self._autosave_preset()

                    self._control_send_to_client(
                        int(client_id),
                        conn,
                        {
                            "type": "welcome",
                            "version": int(CONTROL_PROTOCOL_VERSION),
                            "session_token": format_session_token(session_token),
                            "server_time_ms": int(time.time() * 1000),
                        },
                    )
                    self._control_push_config(int(client_id))

        except ControlProtocolError as e:
            if not self._stop.is_set():
                logger.warning("control protocol error from {}: {}", addr, e)
        finally:
            with best_effort("conn.close()"):
                conn.close()

            if client_id is not None:
                with self._ctrl_lock:
                    if self._ctrl_sessions.get(int(client_id)) is conn:
                        self._ctrl_sessions.pop(int(client_id), None)
                        self._ctrl_send_locks.pop(int(client_id), None)
                        # Keep removal and token revocation atomic with respect
                        # to a replacement hello. The token guard is a second
                        # line of defence for any stale finalizer.
                        with self._lock:
                            st = self._clients.get(int(client_id))
                            if (
                                st is not None
                                and int(session_token) != 0
                                and int(st.session_token) == int(session_token)
                            ):
                                self._expire_client_session(st)
