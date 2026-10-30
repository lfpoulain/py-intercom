"""HTTP remote control of the client's PTT (Stream Deck, Companion, scripts).

    POST /ptt/<bus>/on             start talking on a bus
    POST /ptt/<bus>/on?hold_ms=N   same, released after N ms unless renewed
    POST /ptt/<bus>/off            stop talking on a bus
    POST /ptt/<bus>/toggle         switch between the two
    POST /ptt/all/off              release every bus
    GET  /status                   operational state and PTT state of every bus

<bus> is ``regie``, ``plateau``, ``vmix`` or the numeric bus id. Answers are
JSON. The server is off by default and listens on localhost unless the
operator opens it to the LAN.

Requests are executed on the Qt thread through the same path as the keyboard
shortcuts, so the window stays in sync and every safety rule still applies:
nothing is armed unless the client is online. A controller that renews a
leased ``on`` while its button is held can never leave a microphone open by
crashing before its ``off``. Requests carrying an ``Origin``
or ``Referer`` header are refused, so a web page open in a browser cannot
trigger a PTT behind the operator's back. The port is bound exclusively, so a
second client on the same PC reports it as unavailable instead of sharing it,
and a connection that sends nothing is dropped after a few seconds.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loguru import logger
from PySide6 import QtCore

from ..common.buses import BUS_BY_ID, FIXED_BUSES

REMOTE_DEFAULT_PORT = 5080
REMOTE_ACTIONS = ("on", "off", "toggle")
# Bounds of a leased "on": short enough to cut a stranded microphone quickly,
# long enough for a controller to renew it while its button is held.
REMOTE_HOLD_MS_MIN = 100
REMOTE_HOLD_MS_MAX = 10_000
_DISPATCH_TIMEOUT_S = 2.0
_REQUEST_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class RemoteCommand:
    action: str  # "status", "on", "off" or "toggle"
    bus_id: int | None = None  # None: every bus (only with "off")
    hold_ms: int | None = None  # lease of an "on"; None: held until "off"


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


def _parse_hold_ms(values: list[str]) -> int | None:
    """The lease of a query string, or None unless it is one bounded integer."""
    if len(values) != 1 or not (values[0].isascii() and values[0].isdigit()) or len(values[0]) > 5:
        return None
    hold_ms = int(values[0])
    return hold_ms if REMOTE_HOLD_MS_MIN <= hold_ms <= REMOTE_HOLD_MS_MAX else None


def route(method: str, path: str) -> RemoteCommand | RemoteReply:
    """Map an HTTP request line to a command, or to the error to send back."""
    raw_path, _, query = path.partition("?")
    parts = [part for part in raw_path.split("/") if part]
    if parts == ["status"]:
        if method != "GET":
            return RemoteReply(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "utilisez GET /status"})
        return RemoteCommand("status")
    if len(parts) == 3 and parts[0] == "ptt":
        if method != "POST":
            return RemoteReply(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "les commandes PTT exigent POST"})
        _, bus_token, action = parts
        action = action.lower()
        hold_values = urllib.parse.parse_qs(query, keep_blank_values=True).get("hold_ms")
        if hold_values is not None and (action != "on" or bus_token.lower() == "all"):
            return RemoteReply(HTTPStatus.BAD_REQUEST, {"error": "hold_ms n'est accepté qu'avec POST /ptt/<bus>/on"})
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
        hold_ms = None
        if hold_values is not None:
            hold_ms = _parse_hold_ms(hold_values)
            if hold_ms is None:
                return RemoteReply(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"hold_ms attendu : entier de {REMOTE_HOLD_MS_MIN} à {REMOTE_HOLD_MS_MAX} (ms)"},
                )
        return RemoteCommand(action, bus_id, hold_ms)
    return RemoteReply(HTTPStatus.NOT_FOUND, {"error": "routes : GET /status, POST /ptt/<bus>/<on|off|toggle>"})


def bus_label(bus_id: int) -> str:
    definition = BUS_BY_ID.get(int(bus_id))
    return definition.key if definition is not None else str(bus_id)


class _Handler(BaseHTTPRequestHandler):
    server_version = "py-intercom"
    timeout = _REQUEST_TIMEOUT_S  # an idle connection must not hold a thread forever
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


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server that refuses to share its port.

    On Windows, SO_REUSEADDR (Python's default for HTTP servers) lets a second
    process bind the same port and receive requests at random: use
    SO_EXCLUSIVEADDRUSE there instead. Elsewhere SO_REUSEADDR only skips
    TIME_WAIT and is kept.
    """

    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class RemoteControlServer:
    """Small threaded HTTP server; ``dispatcher`` runs in the request thread."""

    def __init__(self, dispatcher: Dispatcher, *, host: str = "127.0.0.1", port: int = REMOTE_DEFAULT_PORT) -> None:
        handler = type("RemoteHandler", (_Handler,), {"dispatcher": staticmethod(dispatcher)})
        self._httpd = _ExclusiveHTTPServer((host, int(port)), handler)
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


@dataclass
class _Job:
    command: RemoteCommand
    done: threading.Event = field(default_factory=threading.Event)
    reply: RemoteReply | None = None
    started: bool = False
    cancelled: bool = False


class QtDispatcher(QtCore.QObject):
    """Runs a handler on the Qt thread on behalf of HTTP request threads.

    A command answered 503 is cancelled: it must never run later, once the Qt
    thread is free again, behind the back of a controller told it was refused.
    """

    _requested = QtCore.Signal(object)

    def __init__(self, handler: Callable[[RemoteCommand], RemoteReply], parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._handler = handler
        self._job_lock = threading.Lock()
        self._requested.connect(self._run, QtCore.Qt.ConnectionType.QueuedConnection)

    def __call__(self, command: RemoteCommand) -> RemoteReply:
        job = _Job(command)
        self._requested.emit(job)
        if not job.done.wait(_DISPATCH_TIMEOUT_S):
            with self._job_lock:
                if not job.started:
                    job.cancelled = True
                    return RemoteReply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "interface occupée, réessayez"})
            # Already running on the Qt thread: its outcome is the answer.
            job.done.wait()
        return job.reply

    @QtCore.Slot(object)
    def _run(self, job: _Job) -> None:
        with self._job_lock:
            if job.cancelled:
                logger.debug("remote command {} dropped: it was answered 503", job.command)
                return
            job.started = True
        try:
            job.reply = self._handler(job.command)
        except Exception as exc:
            logger.exception("remote command failed on the Qt thread")
            job.reply = RemoteReply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        finally:
            job.done.set()
