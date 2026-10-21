"""TCP control plane: handshake, liveness pings and state reports to the server."""

from __future__ import annotations

import socket
import threading
import time

from loguru import logger

from ..common.constants import (
    CTRL_LIVENESS_TIMEOUT_S,
    CTRL_PING_INTERVAL_S,
    MAX_GAIN_DB,
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


class ControlMixin:
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
        if generation is not None:
            event = self._stop if stop_event is None else stop_event
            if not self._network_is_current(event, int(generation)):
                return
        mtype = str(msg.get("type") or "").lower()
        if mtype == "kick":
            with best_effort("record kick message"):
                with self._state_lock:
                    self._kicked = True
                    self._kick_message = str(msg.get("message") or "Tu as été kick")
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
                    with best_effort("apply server input gain"):
                        with self._state_lock:
                            self._input_gain_db = max(-60.0, min(float(MAX_GAIN_DB), float(cfg.get("input_gain_db"))))
                if "buses" in cfg:
                    buses = cfg.get("buses")
                    if not isinstance(buses, (list, dict)):
                        raise ControlProtocolError("config buses must be a list or object")
                    with best_effort("parse server bus list"):
                        parsed: dict[int, dict] = {}
                        if isinstance(buses, list):
                            for b in buses:
                                if not isinstance(b, dict):
                                    continue
                                try:
                                    bid = int(b.get("bus_id"))
                                except (TypeError, ValueError):
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
                                except (TypeError, ValueError):
                                    continue
                                parsed[int(bid)] = {
                                    "bus_id": int(bid),
                                    "name": str(b.get("name") or f"Bus {int(bid)}"),
                                    "feed_to_regie": bool(b.get("feed_to_regie", False)),
                                }
                        self._ctrl_buses = parsed
                if "return_vu_dbfs" in cfg:
                    with best_effort("read return VU from config"):
                        with self._state_lock:
                            self._return_vu_dbfs = float(cfg.get("return_vu_dbfs"))
                if not bool(self._ctrl_received_config):
                    # No audio from the previous control session is allowed to
                    # survive into this one.  RX is gated until this reset has
                    # completed and a real server config was received.
                    with best_effort("_jb.reset()"):
                        self._jb.reset()
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
        backoff_s = 1.0
        liveness_timeout_s = CTRL_LIVENESS_TIMEOUT_S
        ping_interval_s = CTRL_PING_INTERVAL_S

        while self._network_is_current(stop_event, generation):
            sock: socket.socket | None = None
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
                    except TimeoutError:
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
                    with best_effort("_jb.reset()"):
                        self._jb.reset()
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

            if not self._network_is_current(stop_event, generation):
                break
            if stop_event.wait(backoff_s):
                break
            backoff_s = min(backoff_s * 2.0, 10.0)
