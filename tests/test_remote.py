from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
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
)
from py_intercom.client.status import ClientOperationalState, ClientOperationalStatus
from py_intercom.common.buses import PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# --- Listening address -----------------------------------------------------


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

    def release_all(self) -> None:
        self.held.clear()


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
        _ptt_pad_held={},
        _operational_status=ClientOperationalStatus(state, state.value, ""),
        _refresh_state_labels=lambda: None,
        sent=sent,
    )
    for name in (
        "_ptt_bus_active",
        "_ptt_bus_armed",
        "_apply_ptt_bus",
        "_remote_status_payload",
        "_handle_remote_command",
        "_set_remote_target",
        "_on_pad_pressed",
        "_on_pad_released",
        "_release_pad_holds",
        "_release_all_talk",
    ):
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

    for action in ("on", "toggle"):
        reply = window._handle_remote_command(RemoteCommand(action, REGIE_BUS_ID))
        assert reply.status == HTTPStatus.CONFLICT
        assert "pas en ligne" in reply.body["error"]
    assert window.sent == []
    assert window._ptt_remote_active == {}


def test_off_is_accepted_while_not_online() -> None:
    window = _window(online=False)
    window._ptt_bus_toggle_state[PLATEAU_BUS_ID] = True  # kept through a degraded link
    window._ptt_remote_active[REGIE_BUS_ID] = True

    reply = window._handle_remote_command(RemoteCommand("off", None))

    assert reply.status == HTTPStatus.OK
    assert window._ptt_bus_toggle_state[PLATEAU_BUS_ID] is False
    assert window._ptt_remote_active[REGIE_BUS_ID] is False
    assert window._global_ptt.toggle_sync == [(PLATEAU_BUS_ID, False)]


# --- Talk pads (mouse) follow the same rules -------------------------------------


def test_pad_holds_a_ptt_bus_only_while_pressed() -> None:
    window = _window()

    window._on_pad_pressed(REGIE_BUS_ID)
    assert window.sent == [(REGIE_BUS_ID, True)]

    window._on_pad_released(REGIE_BUS_ID)
    assert window.sent[-1] == (REGIE_BUS_ID, False)
    assert window._ptt_pad_held == {}


def test_pad_release_does_not_cut_a_remote_hold() -> None:
    window = _window()
    window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID))
    window._on_pad_pressed(REGIE_BUS_ID)
    window.sent.clear()

    window._on_pad_released(REGIE_BUS_ID)

    assert window.sent == [(REGIE_BUS_ID, True)]


def test_pad_click_flips_a_toggle_bus_and_syncs_hotkeys() -> None:
    window = _window()

    window._on_pad_pressed(PLATEAU_BUS_ID)
    window._on_pad_released(PLATEAU_BUS_ID)
    assert window._ptt_bus_toggle_state[PLATEAU_BUS_ID] is True
    assert window._global_ptt.toggle_sync == [(PLATEAU_BUS_ID, True)]

    window._on_pad_pressed(PLATEAU_BUS_ID)
    assert window._ptt_bus_toggle_state[PLATEAU_BUS_ID] is False
    assert window.sent[-1] == (PLATEAU_BUS_ID, False)


def test_pads_do_nothing_while_offline_or_always_on() -> None:
    offline = _window(online=False)
    offline._on_pad_pressed(REGIE_BUS_ID)
    assert offline.sent == [] and offline._ptt_pad_held == {}

    online = _window()
    online._on_pad_pressed(VMIX_BUS_ID)
    assert online.sent == [] and online._ptt_pad_held == {}


def test_release_all_frees_pads_toggles_and_remote_holds() -> None:
    window = _window()
    window._on_pad_pressed(REGIE_BUS_ID)
    window._on_pad_pressed(PLATEAU_BUS_ID)
    window.sent.clear()

    window._release_all_talk()

    assert window._ptt_pad_held == {}
    assert window._ptt_bus_toggle_state[PLATEAU_BUS_ID] is False
    assert (REGIE_BUS_ID, False) in window.sent and (PLATEAU_BUS_ID, False) in window.sent


def test_release_all_frees_a_key_the_hook_never_saw_released() -> None:
    window = _window(held={REGIE_BUS_ID: True})
    assert window._ptt_bus_active(REGIE_BUS_ID)

    window._release_all_talk()

    assert not window._ptt_bus_active(REGIE_BUS_ID)
    assert (REGIE_BUS_ID, False) in window.sent


def test_disabling_http_control_drops_its_holds_but_keeps_toggles() -> None:
    window = _window()
    window._remote_server = None
    window._stop_remote_server = ClientWindow._stop_remote_server.__get__(window)
    window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID))
    window._handle_remote_command(RemoteCommand("toggle", PLATEAU_BUS_ID))

    window._stop_remote_server()

    assert window._ptt_bus_active(REGIE_BUS_ID) is False
    assert window._ptt_bus_active(PLATEAU_BUS_ID) is True
    assert window.sent[-1] == (REGIE_BUS_ID, False)
