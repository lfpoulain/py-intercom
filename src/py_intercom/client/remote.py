"""HTTP remote control of the client's PTT (Stream Deck, Companion, scripts).

    POST /ptt/<bus>/on       start talking on a bus
    POST /ptt/<bus>/off      stop talking on a bus
    POST /ptt/<bus>/toggle   switch between the two
    POST /ptt/all/off        release every bus
    GET  /status             operational state and PTT state of every bus

<bus> is ``regie``, ``plateau``, ``vmix`` or the numeric bus id. Answers are
JSON. The server is off by default and listens on localhost unless the
operator opens it to the LAN.

Requests are executed on the Qt thread through the same path as the keyboard
shortcuts, so the window stays in sync and every safety rule still applies:
nothing is armed unless the client is online. Requests carrying an ``Origin``
or ``Referer`` header are refused, so a web page open in a browser cannot
trigger a PTT behind the operator's back.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loguru import logger
from PySide6 import QtCore

from ..common.buses import BUS_BY_ID, FIXED_BUSES

REMOTE_DEFAULT_PORT = 5080
REMOTE_ACTIONS = ("on", "off", "toggle")
_DISPATCH_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class RemoteCommand:
    action: str  # "status", "on", "off" or "toggle"
    bus_id: int | None = None  # None: every bus (only with "off")


@dataclass
class RemoteReply:
    status: HTTPStatus
    body: dict = field(default_factory=dict)


Dispatcher = Callable[[RemoteCommand], RemoteReply]


def parse_bus(token: str) -> int | None:
    token = str(token).strip().lower()
    for definition in FIXED_BUSES:
        if token in (definition.key, str(definition.bus_id)):
            return int(definition.bus_id)
    return None


def route(method: str, path: str) -> RemoteCommand | RemoteReply:
    """Map an HTTP request line to a command, or to the error to send back."""
    parts = [part for part in path.split("?", 1)[0].split("/") if part]
    if parts == ["status"]:
        if method != "GET":
            return RemoteReply(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "utilisez GET /status"})
        return RemoteCommand("status")
    if len(parts) == 3 and parts[0] == "ptt":
        if method != "POST":
            return RemoteReply(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "les commandes PTT exigent POST"})
        _, bus_token, action = parts
        action = action.lower()
        if bus_token.lower() == "all":
            if action != "off":
                return RemoteReply(HTTPStatus.BAD_REQUEST, {"error": "seul POST /ptt/all/off est accepté"})
            return RemoteCommand("off", None)
        bus_id = parse_bus(bus_token)
        if bus_id is None:
            known = ", ".join(definition.key for definition in FIXED_BUSES)
            return RemoteReply(HTTPStatus.NOT_FOUND, {"error": f"bus inconnu « {bus_token} » ; attendus : {known}"})
        if action not in REMOTE_ACTIONS:
            return RemoteReply(HTTPStatus.NOT_FOUND, {"error": "action attendue : on, off ou toggle"})
        return RemoteCommand(action, bus_id)
    return RemoteReply(HTTPStatus.NOT_FOUND, {"error": "routes : GET /status, POST /ptt/<bus>/<on|off|toggle>"})


def bus_label(bus_id: int) -> str:
    definition = BUS_BY_ID.get(int(bus_id))
    return definition.key if definition is not None else str(bus_id)


class _Handler(BaseHTTPRequestHandler):
    server_version = "py-intercom"
    dispatcher: Dispatcher  # set per server

    def _handle(self, method: str) -> None:
        if self.headers.get("Origin") or self.headers.get("Referer"):
            reply = RemoteReply(HTTPStatus.FORBIDDEN, {"error": "requêtes de navigateur refusées"})
        else:
            parsed = route(method, self.path)
            reply = parsed if isinstance(parsed, RemoteReply) else self._dispatch(parsed)
        payload = json.dumps(reply.body, ensure_ascii=False).encode("utf-8")
        self.send_response(int(reply.status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _dispatch(self, command: RemoteCommand) -> RemoteReply:
        try:
            return self.dispatcher(command)
        except Exception as exc:
            logger.exception("remote command {} failed", command)
            return RemoteReply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("remote {} {}", self.address_string(), format % args)


class RemoteControlServer:
    """Small threaded HTTP server; ``dispatcher`` runs in the request thread."""

    def __init__(self, dispatcher: Dispatcher, *, host: str = "127.0.0.1", port: int = REMOTE_DEFAULT_PORT) -> None:
        handler = type("RemoteHandler", (_Handler,), {"dispatcher": staticmethod(dispatcher)})
        self._httpd = ThreadingHTTPServer((host, int(port)), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="remote-http", daemon=True)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        self._thread.start()
        logger.info("remote control listening on {}:{}", *self._httpd.server_address[:2])

    def stop(self) -> None:
        # shutdown() waits for serve_forever(): only call it once it runs.
        if self._thread.is_alive():
            self._httpd.shutdown()
            self._thread.join(timeout=2.0)
        self._httpd.server_close()


class QtDispatcher(QtCore.QObject):
    """Runs a handler on the Qt thread on behalf of HTTP request threads."""

    _requested = QtCore.Signal(object)

    def __init__(self, handler: Callable[[RemoteCommand], RemoteReply], parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._handler = handler
        self._requested.connect(self._run, QtCore.Qt.ConnectionType.QueuedConnection)

    def __call__(self, command: RemoteCommand) -> RemoteReply:
        job = {"command": command, "done": threading.Event(), "reply": None}
        self._requested.emit(job)
        if not job["done"].wait(_DISPATCH_TIMEOUT_S):
            return RemoteReply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "interface occupée, réessayez"})
        return job["reply"]

    @QtCore.Slot(object)
    def _run(self, job: dict) -> None:
        try:
            job["reply"] = self._handler(job["command"])
        except Exception as exc:
            logger.exception("remote command failed on the Qt thread")
            job["reply"] = RemoteReply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        finally:
            job["done"].set()
