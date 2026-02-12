from __future__ import annotations

import atexit
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Dict

import numpy as np
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from loguru import logger

from ..common.audio import float32_to_int16_bytes
from ..common.constants import FRAME_SAMPLES
from ..common.discovery import DiscoveryListener
from .bridge import BridgeConfig, IntercomBridge


@dataclass
class WebClientSession:
    sid: str
    bridge: IntercomBridge


def create_app() -> tuple[Flask, SocketIO]:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secrets.token_hex(32)

    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    sessions: Dict[str, WebClientSession] = {}
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
            except Exception:
                pass
        try:
            discovery.stop()
        except Exception:
            pass

    atexit.register(_stop_all)

    @app.get("/")
    def index():
        return render_template("index.html")

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
            sess = sessions.pop(str(sid), None)
        if sess is None:
            return
        try:
            sess.bridge.stop()
        except Exception:
            pass

    @socketio.on("connect")
    def on_connect():
        sid = str(request.sid)
        logger.info("web client connected sid={} ip={}", sid, request.remote_addr)
        emit("server", {"type": "connected", "sid": sid})
        # Send current discovered servers immediately
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
        try:
            server_port = int(data.get("server_port") or 0)
        except Exception:
            server_port = 0

        name = str(data.get("name") or "plateau").strip()
        client_uuid = str(data.get("client_uuid") or "").strip() or secrets.token_hex(16)
        mode = str(data.get("mode") or "ptt").strip() or "ptt"

        if not server_ip or server_port <= 0:
            emit("server", {"type": "error", "message": "server_ip/server_port required"})
            return

        _stop_session(sid)

        def _on_audio_frame(frame_f32: np.ndarray) -> None:
            if not isinstance(frame_f32, np.ndarray):
                return
            if int(frame_f32.shape[0]) != int(FRAME_SAMPLES):
                return
            try:
                pcm = float32_to_int16_bytes(frame_f32)
            except Exception:
                return
            try:
                socketio.emit("audio_out", pcm, to=sid)
            except Exception:
                pass

        def _on_control_msg(msg: dict[str, Any]) -> None:
            try:
                socketio.emit("control", msg, to=sid)
            except Exception:
                pass

        def _on_kick(message: str) -> None:
            try:
                socketio.emit("server", {"type": "kick", "message": str(message or "")}, to=sid)
            except Exception:
                pass
            # Defer stop to avoid calling stop() from within bridge control thread
            threading.Thread(target=_stop_session, args=(sid,), daemon=True).start()

        cfg = BridgeConfig(server_ip=server_ip, server_port=int(server_port), name=name, mode=mode)
        bridge = IntercomBridge(client_uuid=client_uuid, config=cfg, on_audio_frame=_on_audio_frame, on_control_msg=_on_control_msg, on_kick=_on_kick)
        bridge.start()

        sess = WebClientSession(sid=sid, bridge=bridge)
        with sessions_lock:
            sessions[sid] = sess

        emit(
            "server",
            {
                "type": "joined",
                "sid": sid,
                "client_uuid": client_uuid,
                "client_id": int(bridge.client_id),
                "server_ip": server_ip,
                "server_port": int(server_port),
                "mode": mode,
                "name": name,
            },
        )

    @socketio.on("leave")
    def on_leave():
        sid = str(request.sid)
        _stop_session(sid)
        emit("server", {"type": "left"})

    @socketio.on("ptt")
    def on_ptt(data: Any):
        sid = str(request.sid)
        active = False
        if isinstance(data, dict):
            active = bool(data.get("active"))
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        try:
            sess.bridge.set_state(ptt_general=bool(active))
        except Exception:
            pass

    @socketio.on("mute")
    def on_mute(data: Any):
        sid = str(request.sid)
        muted = False
        if isinstance(data, dict):
            muted = bool(data.get("muted"))
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        try:
            sess.bridge.set_state(muted=bool(muted))
        except Exception:
            pass

    @socketio.on("mode")
    def on_mode(data: Any):
        sid = str(request.sid)
        if not isinstance(data, dict):
            return
        mode = str(data.get("mode") or "").strip()
        if mode not in ("ptt", "always_on"):
            return
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return
        sess.bridge.config.mode = mode

    @socketio.on("audio_in")
    def on_audio_in(pcm: Any):
        sid = str(request.sid)
        with sessions_lock:
            sess = sessions.get(sid)
        if sess is None:
            return

        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            return

        b = bytes(pcm)
        if len(b) != int(FRAME_SAMPLES) * 2:
            return

        try:
            sess.bridge.handle_audio_in_int16(b)
        except Exception:
            pass

    return app, socketio
