"""TCP control plane: handshake, liveness pings and state reports to the server."""

from __future__ import annotations

import random
import socket
import threading
import time

from loguru import logger

from ..common.audio import clamp_gain_db
from ..common.constants import (
    CTRL_LIVENESS_TIMEOUT_S,
    CTRL_PING_INTERVAL_S,
    CTRL_RECONNECT_MAX_S,
    CTRL_RECONNECT_MIN_S,
)
from ..common.control_protocol import (
    CONTROL_PROTOCOL_VERSION,
    ControlProtocolError,
    JsonLineBuffer,
    encode_control_message,
    parse_session_token,
    require_control_protocol_version,
)
from ..common.logging import best_effort

_MAX_SERVER_MESSAGE_CHARS = 300


class ControlRefused(Exception):
    """The server refused this station's hello for good (``"retry": false``).

    A refusal names what the station must fix (protocol version, identity,
    name), none of which can change while it is connected: retrying would
    only repeat it, so the control loop gives up until the operator reconnects.
    """


class ControlMixin:
    def _control_send(self, sock: socket.socket, msg: dict) -> None:
        data = encode_control_message(msg)
        with self._ctrl_send_lock:
            sock.sendall(data)

    def _control_send_state(self) -> None:
        sock = self._ctrl_sock
        # Never before the hello: the server would refuse the session. The full
        # state is sent once the config arrives, so no change is lost.
        if sock is None or not self._ctrl_received_config:
            return
        with self._state_lock:
            input_gain_db = float(self._input_gain_db)
            ptt_buses = dict(self._ptt_buses)
            listen_return_bus = bool(self._listen_return_bus)
            listen_regie = bool(self._listen_regie)
            duck_return = bool(self._duck_return)
            return_gain_db = float(self._return_gain_db)
        msg: dict[str, object] = {
            "type": "state",
            "client_id": int(self.client_id),
            "input_gain_db": input_gain_db,
            "applies_input_gain_locally": True,
            "ptt_buses": ptt_buses,
            "listen_return_bus": listen_return_bus,
            "listen_regie": listen_regie,
            "duck_return": duck_return,
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

    def _control_handle_msg(
        self,
        msg: dict,
        *,
        generation: int | None = None,
        stop_event: threading.Event | None = None,
        udp_ready: threading.Event | None = None,
    ) -> None:
        event = self._stop if stop_event is None else stop_event
        if generation is not None and not self._network_is_current(event, int(generation)):
            return
        mtype = str(msg.get("type") or "").lower()
        if mtype == "error":
            # Shown to the operator: bounded so a broken server cannot flood the dialog.
            reason = str(msg.get("message") or "").strip()[:_MAX_SERVER_MESSAGE_CHARS] or "aucun motif donné"
            if not self._ctrl_received_welcome and msg.get("retry") is False:
                raise ControlRefused(reason)
            # A transient error (server busy or stopping, slow hello): the
            # server closes this session right after and the loop reconnects.
            logger.warning("control error from server: {}", reason)
            return
        if mtype == "kick":
            kick_message = str(msg.get("message") or "Tu as été kick")[:_MAX_SERVER_MESSAGE_CHARS]
            logger.warning("session closed by the server (kick): {}", kick_message)
            with self._state_lock:
                self._kicked = True
                self._kick_message = kick_message
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
            if not self._activate_session_token(int(session_token), event):
                # A disconnect ran since the check above and already reset
                # the session: this generation is over.
                return
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
                    with self._state_lock:
                        self._input_gain_db = clamp_gain_db(cfg.get("input_gain_db"), default=self._input_gain_db)
                # The buses are the fixed catalog: their entries are not used,
                # but a config that breaks their shape breaks the protocol.
                if "buses" in cfg and not isinstance(cfg.get("buses"), list):
                    raise ControlProtocolError("config buses must be a list")
                if "return_vu_dbfs" in cfg:
                    with best_effort("read return VU from config"):
                        with self._state_lock:
                            self._return_vu_dbfs = float(cfg.get("return_vu_dbfs"))
                if not bool(self._ctrl_received_config):
                    # No audio from the previous control session survives into
                    # this one: the welcome emptied every audio queue when it
                    # activated the token, and RX stays gated until this point.
                    self._ctrl_received_config = True
                    ready_event = self._udp_ready if udp_ready is None else udp_ready
                    ready_event.set()
                    self._control_send_state()
                    self._send_silence_probe()
        elif mtype == "pong":
            if "return_vu_dbfs" in msg:
                with best_effort("read return VU from pong"):
                    with self._state_lock:
                        self._return_vu_dbfs = float(msg.get("return_vu_dbfs"))
            return

    def _control_loop(
        self,
        stop_event: threading.Event,
        generation: int,
        udp_ready: threading.Event,
    ) -> None:
        server = f"{self._server_addr[0]}:{int(self.config.control_port or 0)}"
        backoff_s = CTRL_RECONNECT_MIN_S
        # While the server is down the same failure repeats every couple of
        # seconds: the file log keeps its first occurrence only.
        last_failure = ""

        while self._network_is_current(stop_event, generation):
            sock: socket.socket | None = None
            connected = False
            refused = False
            failure = ""
            failure_level = "INFO"
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                # Published before connect() so that a disconnect can close it
                # and wake this thread at once, not after the connect timeout.
                with self._network_state_lock:
                    if not self._network_is_current_locked(stop_event, generation):
                        continue
                    self._ctrl_sock = sock
                sock.connect((self._server_addr[0], int(self.config.control_port or 0)))
                sock.settimeout(0.02)
                with self._network_state_lock:
                    if not self._network_is_current_locked(stop_event, generation):
                        continue
                    self._ctrl_connected = True
                    self._ctrl_received_welcome = False
                    self._ctrl_received_config = False
                    udp_ready.clear()
                    self._ctrl_last_rx_monotonic = time.monotonic()
                    self._ctrl_last_tx_monotonic = time.monotonic()
                connected = True

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
                welcomed = False
                end_reason = ""
                while self._network_is_current(stop_event, generation):
                    now = time.monotonic()
                    silent_s = now - float(self._ctrl_last_rx_monotonic)
                    if silent_s >= float(CTRL_LIVENESS_TIMEOUT_S):
                        end_reason = f"liveness lost, nothing received for {silent_s:.1f} s"
                        break
                    if now - float(self._ctrl_last_tx_monotonic) >= float(CTRL_PING_INTERVAL_S):
                        try:
                            self._control_send(sock, {"type": "ping"})
                        except OSError as e:
                            end_reason = f"ping failed: {e}"
                            break
                        self._ctrl_last_tx_monotonic = now

                    try:
                        chunk = sock.recv(4096)
                    except TimeoutError:
                        continue
                    except OSError as e:
                        end_reason = f"connection lost: {e}"
                        break
                    if not chunk:
                        end_reason = "closed by the server"
                        break

                    for msg in parser.feed(chunk):
                        self._ctrl_last_rx_monotonic = time.monotonic()
                        self._control_handle_msg(
                            msg,
                            generation=generation,
                            stop_event=stop_event,
                            udp_ready=udp_ready,
                        )
                    if not welcomed and self._ctrl_received_welcome:
                        # Only a welcome proves the server accepts this
                        # station: until then the backoff keeps growing.
                        welcomed = True
                        backoff_s = CTRL_RECONNECT_MIN_S
                        last_failure = ""
                        logger.info("control session established with {}", server)
                if end_reason and not stop_event.is_set():
                    if welcomed:
                        logger.info("control session with {} ended: {}", server, end_reason)
                    else:
                        # A server dropping every hello would repeat this.
                        failure = f"no welcome, {end_reason}"

            except ControlRefused as e:
                with self._network_state_lock:
                    refused = self._network_is_current_locked(stop_event, generation)
                    if refused:
                        self._ctrl_refusal = str(e)
                if refused:
                    logger.warning("control refused by {}, not retrying: {}", server, e)
            except ControlProtocolError as e:
                failure, failure_level = f"protocol error: {e}", "WARNING"
            except Exception as e:
                failure = f"{type(e).__name__}: {e}"
            finally:
                udp_ready.clear()
                if connected and self._network_is_current(stop_event, generation):
                    # The connection dropped on its own. On a disconnect,
                    # _stop_network has already reset the session.
                    self._reset_session_token()
                with best_effort("close control socket"):
                    if sock is not None:
                        sock.close()
                with self._network_state_lock:
                    if int(self._network_generation) == int(generation):
                        if self._ctrl_sock is sock:
                            self._ctrl_sock = None
                        self._ctrl_connected = False
                        self._ctrl_received_welcome = False
                        self._ctrl_received_config = False

            if refused or not self._network_is_current(stop_event, generation):
                break
            if failure:
                if failure != last_failure:
                    logger.log(failure_level, "control connection to {} failed, retrying: {}", server, failure)
                    last_failure = failure
                else:
                    logger.debug("control connection to {} failed again: {}", server, failure)
            # The jitter keeps every client from retrying in lockstep after a
            # server restart.
            if stop_event.wait(backoff_s * random.uniform(0.8, 1.2)):
                break
            backoff_s = min(backoff_s * 2.0, CTRL_RECONNECT_MAX_S)
