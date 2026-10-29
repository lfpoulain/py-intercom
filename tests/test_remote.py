from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from types import SimpleNamespace

import pytest
from PySide6 import QtWidgets

from py_intercom.client.gui import ClientWindow
from py_intercom.client.remote import (
    QtDispatcher,
    RemoteCommand,
    RemoteControlServer,
    RemoteReply,
    route,
)
from py_intercom.client.status import ClientOperationalState, ClientOperationalStatus
from py_intercom.common.buses import PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# --- Routing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/ptt/regie/on", RemoteCommand("on", REGIE_BUS_ID)),
        ("POST", "/ptt/plateau/off", RemoteCommand("off", PLATEAU_BUS_ID)),
        ("POST", "/ptt/2/toggle", RemoteCommand("toggle", VMIX_BUS_ID)),
        ("POST", "/ptt/VMIX/ON/", RemoteCommand("on", VMIX_BUS_ID)),
        ("POST", "/ptt/all/off", RemoteCommand("off", None)),
        ("GET", "/status", RemoteCommand("status")),
        ("GET", "/status?x=1", RemoteCommand("status")),
    ],
)
def test_routes(method, path, expected) -> None:
    assert route(method, path) == expected


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/ptt/regie/on", HTTPStatus.METHOD_NOT_ALLOWED),
        ("POST", "/status", HTTPStatus.METHOD_NOT_ALLOWED),
        ("POST", "/ptt/studio/on", HTTPStatus.NOT_FOUND),
        ("POST", "/ptt/regie/maybe", HTTPStatus.NOT_FOUND),
        ("POST", "/ptt/all/on", HTTPStatus.BAD_REQUEST),
        ("GET", "/", HTTPStatus.NOT_FOUND),
    ],
)
def test_rejected_routes(method, path, status) -> None:
    reply = route(method, path)
    assert isinstance(reply, RemoteReply)
    assert reply.status == status
    assert reply.body["error"]


# --- HTTP server --------------------------------------------------------------


@pytest.fixture
def http_server():
    commands: list[RemoteCommand] = []

    def dispatcher(command: RemoteCommand) -> RemoteReply:
        commands.append(command)
        return RemoteReply(HTTPStatus.OK, {"ok": True, "action": command.action})

    server = RemoteControlServer(dispatcher, port=0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", commands
    server.stop()


def _request(url: str, method: str = "POST", headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_post_reaches_the_dispatcher(http_server) -> None:
    base, commands = http_server

    status, body = _request(f"{base}/ptt/regie/on")

    assert status == 200 and body == {"ok": True, "action": "on"}
    assert commands == [RemoteCommand("on", REGIE_BUS_ID)]


def test_status_is_a_get(http_server) -> None:
    base, commands = http_server

    assert _request(f"{base}/status", method="GET")[0] == 200
    assert commands == [RemoteCommand("status")]


def test_errors_are_json_and_never_dispatched(http_server) -> None:
    base, commands = http_server

    status, body = _request(f"{base}/ptt/regie/on", method="GET")

    assert status == 405 and "POST" in body["error"]
    assert commands == []


@pytest.mark.parametrize("header", ["Origin", "Referer"])
def test_browser_requests_are_refused(http_server, header) -> None:
    base, commands = http_server

    status, _ = _request(f"{base}/ptt/regie/on", headers={header: "https://example.com"})

    assert status == 403
    assert commands == []


def test_localhost_only_by_default() -> None:
    server = RemoteControlServer(lambda command: RemoteReply(HTTPStatus.OK), port=0)
    try:
        assert server._httpd.server_address[0] == "127.0.0.1"
    finally:
        server.stop()


# --- Qt bridge ----------------------------------------------------------------


def test_qt_dispatcher_runs_the_handler_on_the_qt_thread(qt_app) -> None:
    seen_threads = []

    def handler(command: RemoteCommand) -> RemoteReply:
        seen_threads.append(threading.current_thread())
        return RemoteReply(HTTPStatus.OK, {"action": command.action})

    dispatcher = QtDispatcher(handler)
    replies = []
    worker = threading.Thread(target=lambda: replies.append(dispatcher(RemoteCommand("on", 0))))
    worker.start()
    deadline = time.monotonic() + 3.0
    while worker.is_alive() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    worker.join(timeout=1.0)

    assert seen_threads == [threading.main_thread()]
    assert replies[0].body == {"action": "on"}


# --- Window rules ---------------------------------------------------------------


class _FakeHotkeys:
    def __init__(self, held: dict[int, bool] | None = None) -> None:
        self.held = dict(held or {})
        self.toggle_sync: list[tuple[int, bool]] = []

    def active_buses(self) -> dict[int, bool]:
        return dict(self.held)

    def set_toggle_state(self, bus_id: int, active: bool) -> None:
        self.toggle_sync.append((bus_id, active))


def _window(*, online: bool = True, modes: dict[int, str] | None = None, held: dict[int, bool] | None = None):
    sent: list[tuple[int, bool]] = []
    state = ClientOperationalState.ONLINE if online else ClientOperationalState.RECONNECTING
    window = SimpleNamespace(
        _client=SimpleNamespace(set_ptt_bus=lambda bus_id, active: sent.append((int(bus_id), bool(active)))),
        _global_ptt=_FakeHotkeys(held),
        _ptt_bus_keys={REGIE_BUS_ID: None, PLATEAU_BUS_ID: None, VMIX_BUS_ID: None},
        _ptt_bus_modes=modes or {REGIE_BUS_ID: "ptt", PLATEAU_BUS_ID: "toggle", VMIX_BUS_ID: "always_on"},
        _ptt_bus_toggle_state={},
        _ptt_remote_active={},
        _operational_status=ClientOperationalStatus(state, state.value, ""),
        _refresh_state_labels=lambda: None,
        sent=sent,
    )
    for name in ("_ptt_bus_active", "_apply_ptt_bus", "_remote_status_payload", "_handle_remote_command"):
        setattr(window, name, getattr(ClientWindow, name).__get__(window))
    return window


def test_remote_on_and_off_in_ptt_mode() -> None:
    window = _window()

    assert window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID)).status == HTTPStatus.OK
    reply = window._handle_remote_command(RemoteCommand("off", REGIE_BUS_ID))

    assert window.sent == [(REGIE_BUS_ID, True), (REGIE_BUS_ID, False)]
    assert reply.body["buses"]["regie"] == {"mode": "ptt", "active": False}


def test_remote_off_does_not_cut_a_key_held_by_the_operator() -> None:
    window = _window(held={REGIE_BUS_ID: True})

    window._handle_remote_command(RemoteCommand("off", REGIE_BUS_ID))

    assert window.sent == [(REGIE_BUS_ID, True)]


def test_remote_toggle_in_toggle_mode_keeps_hotkeys_in_sync() -> None:
    window = _window()

    window._handle_remote_command(RemoteCommand("toggle", PLATEAU_BUS_ID))
    window._handle_remote_command(RemoteCommand("toggle", PLATEAU_BUS_ID))

    assert window.sent == [(PLATEAU_BUS_ID, True), (PLATEAU_BUS_ID, False)]
    assert window._global_ptt.toggle_sync == [(PLATEAU_BUS_ID, True), (PLATEAU_BUS_ID, False)]


def test_always_on_bus_cannot_be_cut_remotely() -> None:
    window = _window()

    reply = window._handle_remote_command(RemoteCommand("off", VMIX_BUS_ID))

    assert reply.status == HTTPStatus.CONFLICT
    assert window.sent == []


def test_all_off_releases_remote_and_toggle_buses_but_not_always_on() -> None:
    window = _window()
    window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID))
    window._handle_remote_command(RemoteCommand("on", PLATEAU_BUS_ID))
    window.sent.clear()

    reply = window._handle_remote_command(RemoteCommand("off", None))

    assert reply.status == HTTPStatus.OK
    assert window.sent == [(REGIE_BUS_ID, False), (PLATEAU_BUS_ID, False)]


def test_nothing_is_armed_while_the_client_is_not_online() -> None:
    window = _window(online=False)

    reply = window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID))

    assert reply.status == HTTPStatus.CONFLICT
    assert "pas en ligne" in reply.body["error"]
    assert window.sent == []
    assert window._ptt_remote_active == {}
