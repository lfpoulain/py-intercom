from __future__ import annotations

import atexit
import secrets
import socket
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict

from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
from loguru import logger

from ..common.audio import float32_to_int16_bytes
from ..common.constants import AUDIO_UDP_PORT, CONTROL_PORT_OFFSET, FRAME_SAMPLES, MAX_GAIN_DB, SAMPLE_RATE
from ..common.discovery import DiscoveryListener
from .bridge import BridgeConfig, IntercomBridge


@dataclass
class WebClientSession:
    sid: str
    bridge: IntercomBridge


def create_app() -> tuple[Flask, SocketIO]:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secrets.token_hex(32)

    # Flask-SocketIO with async_mode="threading"
    socketio = SocketIO(app, async_mode="threading")

    sessions: Dict[str, WebClientSession] = {}
    session_revisions: Dict[str, int] = {}
    sessions_lock = threading.Lock()

    discovery = DiscoveryListener()
    discovery.start()

    def _stop_all() -> None:
        with sessions_lock:
            all_sess = list(sessions.values())
            sessions.clear()
        for sess in all_sess:
            try:
                sess.bridge.stop()
            except Exception as e:
                logger.debug("web stop_all bridge stop failed sid={}: {}", sess.sid, e)
        try:
            discovery.stop()
        except Exception as e:
            logger.debug("web discovery stop failed: {}", e)

    atexit.register(_stop_all)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            frame_samples=int(FRAME_SAMPLES),
            sample_rate=int(SAMPLE_RATE),
            max_gain_db=float(MAX_GAIN_DB),
        )

    @app.get("/img/<path:filename>")
    def img_asset(filename: str):
        base_dir = Path(__file__).resolve().parents[1] / "img"
        return send_from_directory(base_dir, filename)

    @app.get("/api/discovery")
    def api_discovery():
        servers = discovery.get_servers()
        result = []
        for key, srv in servers.items():
            result.append({
                "key": key,
                "ip": srv.ip,
                "server_name": srv.server_name,
                "audio_port": srv.audio_port,
                "control_port": srv.control_port,
            })
        from flask import jsonify
        return jsonify(result)

    def _stop_session(sid: str) -> None:
        with sessions_lock:
            session_revisions[str(sid)] = int(session_revisions.get(str(sid), 0)) + 1
            sess = sessions.pop(str(sid), None)
        if sess is None:
            return
        try:
            sess.bridge.stop()
        except Exception as e:
            logger.debug("web session stop failed sid={}: {}", sid, e)

    @socketio.on("connect")
    def on_connect():
        sid = str(request.sid)
        logger.info("web client connected sid={} ip={}", sid, request.remote_addr)
        emit("server", {"type": "connected", "sid": sid})
        servers = discovery.get_servers()
        result = []
        for key, srv in servers.items():
            result.append({"key": key, "ip": srv.ip, "server_name": srv.server_name, "audio_port": srv.audio_port})
        emit("discovery", result)

    @socketio.on("disconnect")
    def on_disconnect():
        sid = str(request.sid)
        logger.info("web client disconnected sid={}", sid)
        _stop_session(sid)

    @socketio.on("join")
    def on_join(data: Any):
        sid = str(request.sid)
        if not isinstance(data, dict):
            emit("server", {"type": "error", "message": "invalid join payload"})
            return

        server_ip = str(data.get("server_ip") or "").strip()
        name = str(data.get("name") or "plateau").strip()
        client_uuid = str(data.get("client_uuid") or "").strip() or secrets.token_hex(16)
        listen_return_bus = bool(data.get("listen_return_bus", False))
        listen_regie = bool(data.get("listen_regie", True))
        try:
            input_gain_db = max(-60.0, min(float(MAX_GAIN_DB), float(data.get("input_gain_db", 0.0))))
        except Exception:
            input_gain_db = 0.0

        if not server_ip:
            emit("server", {"type": "error", "message": "server_ip required"})
            return

        _stop_session(sid)
        with sessions_lock:
            session_revision = int(session_revisions.get(sid, 0))

        # Threaded setup for Bridge
        def _setup_bridge():
            try:
                # Non-blocking TCP probe
                _ctrl_port = int(AUDIO_UDP_PORT) + int(CONTROL_PORT_OFFSET)
                try:
                    probe = socket.create_connection((server_ip, _ctrl_port), timeout=1.5)
                    probe.close()
                except Exception:
                    socketio.emit("server", {"type": "error", "message": f"Serveur intercom injoignable ({server_ip}:{_ctrl_port})"}, to=sid)
                    return

                cfg = BridgeConfig(
                    server_ip=server_ip,
                    server_port=int(AUDIO_UDP_PORT),
                    name=name,
                    listen_return_bus=bool(listen_return_bus),
                    listen_regie=bool(listen_regie),
                    input_gain_db=float(input_gain_db),
                )
                
                def _on_audio_frame(frame):
                    try:
                        pcm = float32_to_int16_bytes(frame)
                        socketio.emit("audio_out", pcm, to=sid)
                    except Exception as e:
                        logger.debug("web audio_out emit failed sid={}: {}", sid, e)

                def _on_control_msg(msg):
                    try:
                        socketio.emit("control", msg, to=sid)
                    except Exception as e:
                        logger.debug("web control emit failed sid={}: {}", sid, e)

                bridge = IntercomBridge(
                    client_uuid=client_uuid, 
                    config=cfg,
                    on_audio_frame=_on_audio_frame,
                    on_control_msg=_on_control_msg
                )
                
                def _on_kick(message: str) -> None:
                    try:
                        socketio.emit("server", {"type": "kick", "message": str(message or "")}, to=sid)
                    except Exception as e:
                        logger.debug("web kick emit failed sid={}: {}", sid, e)
                    # Defer stop to another thread to avoid deadlocks
                    import threading
                    threading.Thread(target=_stop_session, args=(sid,), daemon=True).start()

                bridge._on_kick = _on_kick

                bridge.start()

                sess = WebClientSession(sid=sid, bridge=bridge)
                with sessions_lock:
                    if int(session_revisions.get(sid, 0)) != int(session_revision):
                        store_session = False
                    else:
                        sessions[sid] = sess
                        store_session = True

                if not store_session:
                    try:
                        bridge.stop()
                    except Exception as e:
                        logger.debug("web stale bridge stop failed sid={}: {}", sid, e)
                    return

                response = {
                    "type": "joined",
                    "sid": sid,
                    "client_uuid": client_uuid,
                    "client_id": int(bridge.client_id),
                    "server_ip": server_ip,
                    "server_port": int(AUDIO_UDP_PORT),
                    "name": name,
                }

                socketio.emit("server", response, to=sid)
            except Exception as e:
                logger.error(f"Error setting up bridge: {e}")
                socketio.emit("server", {"type": "error", "message": "Internal bridge error"}, to=sid)

        threading.Thread(target=_setup_bridge, daemon=True).start()

    @socketio.on("leave")
    def on_leave():
        sid = str(request.sid)
        _stop_session(sid)
        emit("server", {"type": "left"})

    @socketio.on("audio_in")
    def on_audio_in(data: Any):
        sid = str(request.sid)
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        try:
            sess.bridge.handle_audio_in_int16(data)
        except Exception:
            pass

    @socketio.on("ptt_bus")
    def on_ptt_bus(data: Any):
        sid = str(request.sid)
        active = False
        bus_id = None
        if isinstance(data, dict):
            active = bool(data.get("active"))
            bus_id = data.get("bus_id")
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        try:
            if bus_id is not None:
                sess.bridge.set_ptt_bus(int(bus_id), bool(active))
        except Exception as e:
            logger.debug("web ptt_bus update failed sid={}: {}", sid, e)

    @socketio.on("input_gain_db")
    def on_input_gain_db(data: Any):
        sid = str(request.sid)
        gain_db = 0.0
        if isinstance(data, dict):
            try:
                gain_db = max(-60.0, min(float(MAX_GAIN_DB), float(data.get("gain_db", 0.0))))
            except Exception:
                gain_db = 0.0
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        try:
            sess.bridge.set_input_gain_db(float(gain_db))
        except Exception as e:
            logger.debug("web input_gain_db update failed sid={}: {}", sid, e)

    @socketio.on("listen_return_bus")
    def on_listen_return_bus(data: Any):
        sid = str(request.sid)
        enabled = False
        if isinstance(data, dict):
            enabled = bool(data.get("enabled"))
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        try:
            sess.bridge.set_listen_return_bus(bool(enabled))
        except Exception as e:
            logger.debug("web listen_return_bus update failed sid={}: {}", sid, e)

    @socketio.on("listen_regie")
    def on_listen_regie(data: Any):
        sid = str(request.sid)
        enabled = False
        if isinstance(data, dict):
            enabled = bool(data.get("enabled"))
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        try:
            sess.bridge.set_listen_regie(bool(enabled))
        except Exception as e:
            logger.debug("web listen_regie update failed sid={}: {}", sid, e)

    return app, socketio
