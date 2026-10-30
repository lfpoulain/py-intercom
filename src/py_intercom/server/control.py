"""TCP control plane: hello/welcome handshake, client state and config pushes."""

from __future__ import annotations

import socket
import threading
import time

import numpy as np
from loguru import logger

from ..common import opus_codec
from ..common.audio import clamp_gain_db
from ..common.buses import (
    FIXED_BUS_IDS,
    FIXED_BUSES,
)
from ..common.constants import (
    CONTROL_PORT_OFFSET,
    CTRL_LIVENESS_TIMEOUT_S,
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
from ..common.jitter_buffer import new_realtime_jitter_buffer
from ..common.logging import best_effort
from .models import ClientProfile, ClientState

# Message told to a peer whose identity collides: never another client's UUID.
_IDENTITY_COLLISION_PEER_MESSAGE = "client identity collides with an existing client"


class ControlMixin:
    def _control_send(self, sock: socket.socket, msg: dict) -> None:
        sock.sendall(encode_control_message(msg))

    def _control_send_with_lock(
        self,
        sock: socket.socket,
        send_lock: threading.Lock | None,
        msg: dict,
    ) -> None:
        if send_lock is None:
            self._control_send(sock, msg)
            return
        with send_lock:
            self._control_send(sock, msg)

    def _control_send_to_client(self, client_id: int, sock: socket.socket, msg: dict) -> None:
        with self._ctrl_lock:
            send_lock = self._ctrl_send_locks.get(int(client_id))
        self._control_send_with_lock(sock, send_lock, msg)

    def _drop_control_session(
        self,
        client_id: int,
        sock: socket.socket,
        *,
        session_token: int | None = None,
    ) -> None:
        """Close ``sock`` and, if it still owns ``client_id``, revoke its session.

        Ownership removal and token revocation are one transaction with respect
        to a replacement hello (lock order: control, then server state). With
        ``session_token``, only the audio session this socket negotiated is
        expired, never a newer one.
        """
        with best_effort("close control socket"):
            sock.close()
        with self._ctrl_lock:
            if self._ctrl_sessions.get(int(client_id)) is not sock:
                return
            self._ctrl_sessions.pop(int(client_id), None)
            self._ctrl_send_locks.pop(int(client_id), None)
            with self._lock:
                st = self._clients.get(int(client_id))
                if st is None:
                    return
                if session_token is not None and (
                    int(session_token) == 0 or int(st.session_token) != int(session_token)
                ):
                    return
                self._expire_client_session(st)

    def _request_config_push(self) -> None:
        """Ask the push worker to send the current config to every client.

        Callers are often Qt slots: they must never block on a slow peer's TCP
        window. Requests coalesce, so the latest state always wins.
        """
        self._config_push_event.set()

    def _config_push_loop(self) -> None:
        while not self._stop.is_set():
            if not self._config_push_event.wait(0.5):
                continue
            self._config_push_event.clear()
            self._control_push_all_configs()

    def _control_push_all_configs(self) -> None:
        with self._lock:
            client_ids = [int(cid) for cid in self._clients]
            input_gain_ids = self._input_gain_push_ids
            self._input_gain_push_ids = set()
        for cid in client_ids:
            self._control_push_config(cid, include_input_gain=cid in input_gain_ids)

    def _control_push_config(self, client_id: int, *, include_input_gain: bool) -> None:
        """Send the current config to one client.

        A client applies its microphone gain itself and reports it in its
        state messages, so the gain is sent only in the welcome config and
        after an operator change of this client's gain: a push caused by a bus
        change must never overwrite a gain the client has just set locally.
        """
        with self._lock:
            st = self._clients.get(int(client_id))
            if st is None:
                return
            cfg = {
                "client_id": int(client_id),
                "return_vu_dbfs": float(self._return_vu_dbfs),
                "buses": [
                    {
                        "bus_id": int(definition.bus_id),
                        "name": str(definition.name),
                        "gain_db": float(self._buses[definition.bus_id].gain_db),
                        "feed_to_regie": bool(self._buses[definition.bus_id].feed_to_regie),
                    }
                    for definition in FIXED_BUSES
                ],
            }
            if include_input_gain:
                cfg["input_gain_db"] = float(st.input_gain_db)

        with self._ctrl_lock:
            sock = self._ctrl_sessions.get(int(client_id))
        if sock is None:
            return
        try:
            self._control_send_to_client(int(client_id), sock, {"type": "update", "config": cfg})
        except Exception as e:
            logger.info("control push config failed for client {}, closing its session: {}", int(client_id), e)
            self._drop_control_session(int(client_id), sock)

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
                self._ctrl_session_threads = {thread for thread in self._ctrl_session_threads if thread.is_alive()}
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
        last_rx = time.monotonic()
        # An opened session logs its end and why: the log of a show must tell
        # when and why each client lost its control channel.
        opened = False
        close_reason = "server stopping"

        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if client_id is None and now >= hello_deadline:
                    self._ctrl_hello_timeouts += 1
                    raise ControlProtocolError(f"hello not received within {float(CONTROL_HELLO_TIMEOUT_S):.1f}s")
                # Clients ping continuously. A silent peer (unplugged, asleep,
                # half-open TCP) must not keep its thread and session slot.
                if client_id is not None and now - last_rx > float(CTRL_LIVENESS_TIMEOUT_S):
                    raise ControlProtocolError(f"no control traffic for {float(CTRL_LIVENESS_TIMEOUT_S):.1f}s")
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        close_reason = "closed by the client"
                        break
                except TimeoutError:
                    continue
                except OSError as exc:
                    close_reason = f"receive failed: {exc}"
                    break
                last_rx = time.monotonic()

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
                                close_reason = "replaced by a newer session"
                                return
                        # One engine-lock section per message: pings arrive
                        # 10 times per second from every client.
                        with self._lock:
                            owned_state = self._clients.get(int(client_id))
                            if (
                                owned_state is None
                                or int(session_token) == 0
                                or int(owned_state.session_token) != int(session_token)
                            ):
                                raise ControlProtocolError("control session token expired; a new hello is required")
                            # A matching token is a live session: any of its
                            # messages proves the client alive.
                            owned_state.last_control_monotonic = time.monotonic()
                    if mtype == "ping":
                        # The pong carries the Return meter of the client.
                        pong_msg = {"type": "pong", "return_vu_dbfs": float(self._return_vu_dbfs)}
                        try:
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
                                    raise ControlProtocolError("state bus id must be an integer") from exc
                                if bus_id not in FIXED_BUS_IDS:
                                    raise ControlProtocolError("state contains an unknown bus id")
                                if not isinstance(raw_active, bool):
                                    raise ControlProtocolError("state PTT values must be booleans")
                                parsed_ptt_buses[int(bus_id)] = bool(raw_active)

                        parsed_flags: dict[str, bool | None] = {}
                        for field_name in (
                            "listen_return_bus",
                            "listen_regie",
                            "applies_input_gain_locally",
                            "duck_return",
                        ):
                            raw_value = msg.get(field_name)
                            if raw_value is not None and not isinstance(raw_value, bool):
                                raise ControlProtocolError(f"state {field_name} must be a boolean")
                            parsed_flags[field_name] = raw_value

                        parsed_gains: dict[str, float | None] = {}
                        for field_name in ("return_gain_db", "input_gain_db"):
                            raw_value = msg.get(field_name)
                            if raw_value is None:
                                parsed_gains[field_name] = None
                                continue
                            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                                raise ControlProtocolError(f"state {field_name} must be a finite number")
                            gain = float(raw_value)
                            if not bool(np.isfinite(gain)):
                                raise ControlProtocolError(f"state {field_name} must be a finite number")
                            parsed_gains[field_name] = clamp_gain_db(gain)

                        listen_return_bus = parsed_flags["listen_return_bus"]
                        listen_regie = parsed_flags["listen_regie"]
                        applies_input_gain_locally = parsed_flags["applies_input_gain_locally"]
                        return_gain_db = parsed_gains["return_gain_db"]
                        input_gain_db = parsed_gains["input_gain_db"]
                        changed = False
                        # Fires on every PTT toggle / slider move; keep at TRACE to avoid log spam.
                        logger.trace(
                            "state from cid={}: listen_return_bus={} listen_regie={}",
                            cid,
                            listen_return_bus,
                            listen_regie,
                        )
                        with self._lock:
                            st = self._clients.get(int(cid))
                            if st is None or int(st.session_token) != int(session_token):
                                close_reason = "session revoked"
                                return
                            if parsed_ptt_buses is not None:
                                st.ptt_buses = dict(parsed_ptt_buses)
                            if listen_return_bus is not None:
                                st.listen_return_bus = listen_return_bus
                            if listen_regie is not None:
                                st.listen_regie = listen_regie
                            duck_return = parsed_flags["duck_return"]
                            if duck_return is not None and bool(duck_return) != bool(st.duck_return):
                                st.duck_return = bool(duck_return)
                                if not st.duck_return:
                                    st.ducker.reset()
                            if return_gain_db is not None:
                                st.return_gain_db = float(return_gain_db)
                            if input_gain_db is not None and self._set_client_input_gain_locked(st, input_gain_db):
                                changed = True
                            if applies_input_gain_locally is not None:
                                next_applies = applies_input_gain_locally
                                if bool(st.applies_input_gain_locally) != bool(next_applies):
                                    st.applies_input_gain_locally = bool(next_applies)
                                    changed = True
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
                        raise ControlProtocolError(f"invalid client identity: {exc}", refused=True) from exc

                    raw_name = msg.get("name", "")
                    if not isinstance(raw_name, str):
                        raise ControlProtocolError("hello name must be a string", refused=True)
                    name = raw_name.strip()
                    if len(name) > 128:
                        raise ControlProtocolError("hello name exceeds 128 characters", refused=True)
                    raw_applies_input_gain_locally = msg.get(
                        "applies_input_gain_locally",
                        False,
                    )
                    if not isinstance(raw_applies_input_gain_locally, bool):
                        raise ControlProtocolError("hello applies_input_gain_locally must be a boolean")
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
                        self._reject_identity_conflict(identity_conflicts)

                    # Construct a complete fresh codec epoch before revoking a
                    # live session. A transient allocation/DLL failure must not
                    # cut audio that is currently working or reuse predictive
                    # state from the previous session.
                    try:
                        next_decoder = opus_codec.OpusDecoder()
                        next_encoder = opus_codec.OpusEncoder()
                        next_jitter = new_realtime_jitter_buffer()
                    except Exception as exc:
                        self._ctrl_codec_init_errors += 1
                        raise ControlProtocolError(f"failed to initialize a fresh audio session: {exc}") from exc

                    session_token = self._new_session_token()

                    now = time.monotonic()
                    profile_changed = False
                    prev = None
                    # The new session's send lock is published already held:
                    # a config push or pong for this client waits until the
                    # welcome, which the client requires first, is sent.
                    welcome_lock = threading.Lock()
                    welcome_lock.acquire()
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
                                self._reject_identity_conflict(identity_conflicts)

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
                                    addr=(str(addr[0]), int(udp_port)),
                                    last_packet_monotonic=now,
                                    decoder=next_decoder,
                                    encoder=next_encoder,
                                    jb=next_jitter,
                                )
                                self._clients[int(client_id)] = st
                            else:
                                # Revoke the previous session, if still live,
                                # before installing the new codec epoch. A new
                                # or already expired client has nothing to
                                # revoke, so nobody else's audio is flushed.
                                self._expire_client_session(st)
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
                            st.addr = (str(addr[0]), int(udp_port))
                            st.control_connected = True
                            st.session_token = int(session_token)
                            st.last_control_monotonic = now
                            st.last_packet_monotonic = now

                            prev = self._ctrl_sessions.get(int(client_id))
                            self._ctrl_sessions[int(client_id)] = conn
                            self._ctrl_send_locks[int(client_id)] = welcome_lock

                    try:
                        if prev is not None and prev is not conn:
                            with best_effort("prev.close()"):
                                prev.close()

                        if profile_changed:
                            self._autosave_preset()

                        self._control_send(
                            conn,
                            {
                                "type": "welcome",
                                "version": int(CONTROL_PROTOCOL_VERSION),
                                "session_token": format_session_token(session_token),
                            },
                        )
                    finally:
                        welcome_lock.release()
                    logger.info(
                        "control session opened: client {} ({!r}) from {}, audio port {}",
                        int(client_id),
                        name,
                        addr[0],
                        udp_port,
                    )
                    opened = True
                    # The welcome config is the one that restores the
                    # client's saved microphone gain.
                    self._control_push_config(int(client_id), include_input_gain=True)

        except ControlProtocolError as e:
            close_reason = str(e)
            if not self._stop.is_set():
                # A refused client retries every second: one line per peer
                # and window is enough to diagnose it.
                note = self._control_error_log.allow(str(addr[0]), now=time.monotonic())
                if note is not None:
                    logger.warning("control protocol error from {}: {}{}", addr, e, note)
                # Tell the peer why before closing, so a refused client can
                # show the reason instead of silently retrying.
                with best_effort("send control error"):
                    error_msg = {"type": "error", "message": e.peer_message, "retry": not e.refused}
                    if client_id is None:
                        self._control_send(conn, error_msg)
                    else:
                        self._control_send_to_client(int(client_id), conn, error_msg)
        except Exception as exc:
            close_reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if client_id is None:
                with best_effort("conn.close()"):
                    conn.close()
            else:
                if opened:
                    logger.info("control session of client {} from {} ended: {}", int(client_id), addr[0], close_reason)
                # The token guard keeps a stale finalizer from revoking a
                # newer session of the same client.
                self._drop_control_session(int(client_id), conn, session_token=int(session_token))

    def _reject_identity_conflict(self, conflicts: tuple[str, ...]) -> None:
        self._ctrl_identity_collisions += 1
        raise ControlProtocolError(
            "client identity collides with existing UUID(s): " + ", ".join(conflicts),
            peer_message=_IDENTITY_COLLISION_PEER_MESSAGE,
            refused=True,
        )
