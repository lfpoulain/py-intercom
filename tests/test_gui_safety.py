from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import time
import uuid
from types import SimpleNamespace

import pytest
from pynput.keyboard import Key
from PySide6 import QtCore, QtGui, QtWidgets

import py_intercom.client.gui as gui_mod
from py_intercom.client.client import ClientMeterLevels
from py_intercom.client.hotkeys import GlobalPttHotkeys, PttHotkeyState
from py_intercom.client.remote import RemoteCommand
from py_intercom.client.status import ClientOperationalState, ClientOperationalStatus
from py_intercom.common import devices as devices_mod
from py_intercom.common.buses import PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID
from py_intercom.common.constants import CHANNELS, SAMPLE_RATE
from py_intercom.common.identity import client_id_from_uuid


def test_momentary_hotkey_tracks_the_configured_chord() -> None:
    state = PttHotkeyState({1: ("ptt", "Ctrl+T")})

    assert state.press("key:ctrl_l") == []
    assert state.press("char:t") == [(1, True)]
    assert state.press("char:t") == []
    assert state.release("char:t") == [(1, False)]
    assert state.release("key:ctrl_l") == []


def test_toggle_hotkey_ignores_unrelated_keys_while_chord_is_held() -> None:
    state = PttHotkeyState({2: ("toggle", "Ctrl+T")})

    assert state.press("key:ctrl_l") == []
    assert state.press("char:t") == [(2, True)]
    assert state.press("char:x") == []
    assert state.release("char:x") == []
    assert state.release("char:t") == []
    assert state.press("char:t") == [(2, False)]


def test_release_all_deactivates_momentary_and_toggle_buses() -> None:
    state = PttHotkeyState(
        {
            1: ("ptt", "F1"),
            2: ("toggle", "F2"),
            3: ("always_on", "F3"),
        }
    )

    assert state.press("key:f1") == [(1, True)]
    assert state.press("key:f2") == [(2, True)]

    assert sorted(state.release_all()) == [(1, False), (2, False)]
    assert state.active_buses() == {}


def test_device_validation_uses_the_engine_audio_format(monkeypatch) -> None:
    checked: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        devices_mod.sd,
        "query_hostapis",
        lambda: [{"name": "Windows WASAPI"}],
    )
    monkeypatch.setattr(
        devices_mod.sd,
        "query_devices",
        lambda: [
            {
                "name": "Duplex",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 44100.0,
            }
        ],
    )

    def check_output_settings(**kwargs) -> None:
        checked.append(("output", dict(kwargs)))

    def check_input_settings(**kwargs) -> None:
        checked.append(("input", dict(kwargs)))

    monkeypatch.setattr(devices_mod.sd, "check_output_settings", check_output_settings)
    monkeypatch.setattr(devices_mod.sd, "check_input_settings", check_input_settings)

    result = devices_mod.list_devices(hard_refresh=False, validate=True)

    assert len(result) == 1
    assert {kind for kind, _kwargs in checked} == {"input", "output"}
    for _kind, kwargs in checked:
        assert kwargs["channels"] == CHANNELS
        assert kwargs["samplerate"] == float(SAMPLE_RATE)


def test_device_is_hidden_when_48khz_mono_is_not_supported(monkeypatch) -> None:
    monkeypatch.setattr(
        devices_mod.sd,
        "query_hostapis",
        lambda: [{"name": "Windows WASAPI"}],
    )
    monkeypatch.setattr(
        devices_mod.sd,
        "query_devices",
        lambda: [
            {
                "name": "Unsupported",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 1,
                "default_samplerate": 44100.0,
            }
        ],
    )

    def unsupported(**_kwargs) -> None:
        raise RuntimeError("48 kHz is unsupported")

    monkeypatch.setattr(devices_mod.sd, "check_output_settings", unsupported)
    monkeypatch.setattr(devices_mod.sd, "check_input_settings", unsupported)

    assert devices_mod.list_devices(hard_refresh=False, validate=True) == []


# --- Client window ---------------------------------------------------------------


class _FakeDiscovery:
    def __init__(self, on_update) -> None:
        self.on_update = on_update

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def get_servers(self) -> dict:
        return {}


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def client_window(qt_app, monkeypatch, tmp_path):
    """A real ClientWindow with no network, no audio and a temporary preset."""
    monkeypatch.setattr(gui_mod, "DiscoveryListener", _FakeDiscovery)
    monkeypatch.setattr(gui_mod.ClientWindow, "_preset_path", lambda _self: tmp_path / "client_preset.json")
    monkeypatch.setattr(gui_mod.ClientWindow, "_start_device_refresh", lambda _self: None)
    window = gui_mod.ClientWindow()
    qt_app.processEvents()  # run the deferred startup calls while patched
    yield window
    window.close()
    window.deleteLater()
    qt_app.processEvents()


class _FakeHotkeys:
    def __init__(self) -> None:
        self.release_all_calls = 0
        self.stop_calls = 0
        self.toggle_sync: list[tuple[int, bool]] = []

    def active_buses(self) -> dict[int, bool]:
        return {}

    def release_all(self) -> None:
        self.release_all_calls += 1

    def set_toggle_state(self, bus_id: int, active: bool) -> None:
        self.toggle_sync.append((int(bus_id), bool(active)))

    def stop(self) -> None:
        self.stop_calls += 1

    def deleteLater(self) -> None:  # noqa: N802 - Qt API
        self.deleted = True


class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, bool]] = []
        self.config = SimpleNamespace(server_ip="127.0.0.1", server_port=5000)

    def set_ptt_bus(self, bus_id: int, active: bool) -> None:
        self.sent.append((int(bus_id), bool(active)))

    def stop(self) -> None:
        return


def _status(state: ClientOperationalState) -> ClientOperationalStatus:
    return ClientOperationalStatus(state, state.value, "")


def _online(window) -> _RecordingClient:
    client = _RecordingClient()
    window._client = client
    window._operational_status = _status(ClientOperationalState.ONLINE)
    return client


def _pump(qt_app, predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_window_deactivation_keeps_toggles_and_releases_only_pad_holds(client_window) -> None:
    window = client_window
    client = _RecordingClient()
    window._client = client
    window._operational_status = ClientOperationalStatus(ClientOperationalState.ONLINE, "En ligne", "")
    hotkeys = _FakeHotkeys()
    window._global_ptt = hotkeys
    window._ptt_bus_modes[PLATEAU_BUS_ID] = "toggle"
    window._ptt_bus_toggle_state[PLATEAU_BUS_ID] = True
    window._on_pad_pressed(REGIE_BUS_ID)
    client.sent.clear()

    window.changeEvent(QtCore.QEvent(QtCore.QEvent.Type.WindowDeactivate))

    assert hotkeys.release_all_calls == 0
    assert window._ptt_bus_toggle_state[PLATEAU_BUS_ID] is True
    assert window._ptt_bus_active(PLATEAU_BUS_ID) is True
    assert window._ptt_pad_held == {}
    assert client.sent == [(REGIE_BUS_ID, False)]


def test_shortcut_editor_keeps_a_single_chord(client_window) -> None:
    edit = client_window._ptt_bus_keys[REGIE_BUS_ID]

    edit.setKeySequence(QtGui.QKeySequence("F1, F2"))

    assert edit.maximumSequenceLength() == 1
    assert edit.keySequence().toString() == "F1"


def test_shortcut_editor_refuses_a_sequence_that_cannot_fire(client_window) -> None:
    window = client_window
    edit = window._ptt_bus_keys[REGIE_BUS_ID]
    edit.set_portable_text("F5")

    edit.set_portable_text("Ctrl+Shift")  # modifiers only: would never fire

    assert edit.portable_text() == ""
    window._preset_writer.flush(raise_on_error=True)
    saved = json.loads(window._preset_path().read_text(encoding="utf-8"))
    assert saved["ptt_bus_keys"][str(REGIE_BUS_ID)] == ""


def test_shortcuts_are_saved_and_reloaded_in_portable_text(qt_app, client_window) -> None:
    window = client_window
    window._ptt_bus_keys[PLATEAU_BUS_ID].set_portable_text("Ctrl+PgUp")
    window._preset_writer.flush(raise_on_error=True)
    assert json.loads(window._preset_path().read_text(encoding="utf-8"))["ptt_bus_keys"][str(PLATEAU_BUS_ID)] == (
        "Ctrl+PgUp"
    )

    reloaded = gui_mod.ClientWindow()  # same patched preset path
    try:
        assert reloaded._ptt_bus_keys[PLATEAU_BUS_ID].portable_text() == "Ctrl+PgUp"
        assert reloaded._hotkey_bindings_snapshot()[PLATEAU_BUS_ID] == ("ptt", "Ctrl+PgUp")
    finally:
        reloaded.close()
        reloaded.deleteLater()
        qt_app.processEvents()


def test_wheel_over_an_unfocused_selector_changes_nothing(client_window) -> None:
    window = client_window
    selectors = (
        window._discovered_servers,
        window._input_device,
        window._output_device,
        window._remote_port,
        *window._ptt_bus_mode_widgets.values(),
    )
    assert all(widget.focusPolicy() == QtCore.Qt.FocusPolicy.StrongFocus for widget in selectors)
    mode_cb = window._ptt_bus_mode_widgets[REGIE_BUS_ID]
    before = mode_cb.currentIndex()

    notch = QtGui.QWheelEvent(
        QtCore.QPointF(5, 5),
        QtCore.QPointF(5, 5),
        QtCore.QPoint(0, 0),
        QtCore.QPoint(0, -120),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QtWidgets.QApplication.sendEvent(mode_cb, notch)

    assert mode_cb.currentIndex() == before


def test_name_field_is_capped_to_what_the_server_accepts(client_window) -> None:
    client_window._name.setText("x" * 200)

    assert len(client_window._name.text()) == 128


def test_every_shortcut_change_is_saved(client_window) -> None:
    window = client_window
    window._ptt_bus_keys[REGIE_BUS_ID].setKeySequence(QtGui.QKeySequence("F5"))
    window._preset_writer.flush(raise_on_error=True)
    window._ptt_bus_keys[REGIE_BUS_ID].setKeySequence(QtGui.QKeySequence("F6"))
    window._preset_writer.flush(raise_on_error=True)

    saved = json.loads(window._preset_path().read_text(encoding="utf-8"))

    assert saved["ptt_bus_keys"][str(REGIE_BUS_ID)] == "F6"


def test_bus_rows_and_pads_are_built_from_the_fixed_buses(client_window) -> None:
    assert sorted(client_window._ptt_pads) == [REGIE_BUS_ID, PLATEAU_BUS_ID, VMIX_BUS_ID]
    assert sorted(client_window._ptt_bus_keys) == [REGIE_BUS_ID, PLATEAU_BUS_ID, VMIX_BUS_ID]


class _FakeRemoteServer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_reset_config_restores_default_preferences_and_stops_remote(client_window, monkeypatch) -> None:
    window = client_window
    for checkbox in (window._autoconnect_cb, window._start_minimized, window._duck_return):
        checkbox.setChecked(True)
    window._listen_regie.setChecked(False)
    window._remote_enabled.blockSignals(True)
    window._remote_enabled.setChecked(True)
    window._remote_enabled.blockSignals(False)
    remote = _FakeRemoteServer()
    window._remote_server = remote
    mode_cb = window._ptt_bus_mode_widgets[PLATEAU_BUS_ID]
    mode_cb.setCurrentIndex(mode_cb.findData("toggle"))
    monkeypatch.setattr(
        gui_mod.QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QtWidgets.QMessageBox.StandardButton.Yes),
    )

    window._reset_total_config()

    assert not window._autoconnect_cb.isChecked()
    assert not window._start_minimized.isChecked()
    assert not window._duck_return.isChecked()
    assert not window._remote_enabled.isChecked()
    assert window._listen_regie.isChecked()
    assert remote.stopped and window._remote_server is None
    assert mode_cb.currentData() == "ptt"
    assert set(window._ptt_bus_modes.values()) == {"ptt"}


class _FakeIntercomClient:
    instances: list[_FakeIntercomClient] = []

    def __init__(self, client_id: int, config) -> None:
        self.client_id = client_id & 0xFFFFFFFF
        self.config = config
        self.stopped = False
        self.reconnects = 0
        self.streams_active = True
        self.snapshot: dict = {}
        _FakeIntercomClient.instances.append(self)

    def start(self) -> None:
        return

    def stop(self) -> None:
        self.stopped = True

    def reconnect_network(self) -> None:
        self.reconnects += 1

    def disconnect_network(self) -> None:
        return

    def get_stats_snapshot(self) -> dict:
        return dict(self.snapshot)

    def audio_streams_active(self) -> bool:
        return self.streams_active

    def meter_levels(self) -> ClientMeterLevels:
        return ClientMeterLevels(-12.0, False, -24.0, -36.0, {REGIE_BUS_ID: -6.0})

    def _ignore(self, *_args) -> None:
        return

    set_ptt_bus = set_input_gain_db = set_output_gain_db = set_return_gain_db = _ignore
    set_listen_return_bus = set_duck_return = set_listen_regie = _ignore


def test_reconnect_after_identity_reset_uses_a_client_with_the_new_id(client_window, monkeypatch) -> None:
    window = client_window
    _FakeIntercomClient.instances = []
    monkeypatch.setattr(gui_mod, "IntercomClient", _FakeIntercomClient)
    window._server_ip.setText("127.0.0.1")
    window._input_device.addItem("Micro", 1)
    window._output_device.addItem("Casque", 2)

    window._connect()
    assert window._global_ptt is None  # nothing is armed while connecting
    window._disconnect()
    window._connect()
    window._disconnect()
    first = _FakeIntercomClient.instances[0]
    assert len(_FakeIntercomClient.instances) == 1 and first.reconnects == 1

    new_uuid = str(uuid.uuid4())
    window._preset_set("client_uuid", new_uuid)
    window._client_id.setText(str(int(client_id_from_uuid(new_uuid))))
    window._connect()

    assert first.stopped
    assert window._client is _FakeIntercomClient.instances[-1]
    assert window._client.client_id == int(client_id_from_uuid(new_uuid)) & 0xFFFFFFFF
    assert window._client.config.client_uuid == new_uuid
    window._disconnect()


class _HungRefresh:
    """A device inventory stuck in a driver, past its timeout once ``stalled``."""

    def __init__(self, *, stalled: bool) -> None:
        self.stalled = stalled
        self.running = True
        self.abandoned = False

    def close_must_wait(self) -> bool:
        return not self.stalled

    def abandon(self) -> bool:
        self.abandoned = True
        return True


def test_close_waits_for_a_running_enumeration(client_window, monkeypatch) -> None:
    window = client_window
    refresh = _HungRefresh(stalled=False)
    monkeypatch.setattr(window, "_device_refresh", refresh)
    event = QtGui.QCloseEvent()

    window.closeEvent(event)

    assert not event.isAccepted()
    assert not window._is_closing
    refresh.stalled = True  # the fixture's close then completes (and stops the remote server)


def test_close_after_enumeration_timeout_keeps_away_from_portaudio(client_window, monkeypatch) -> None:
    window = client_window
    refresh = _HungRefresh(stalled=True)
    monkeypatch.setattr(window, "_device_refresh", refresh)
    engine = _FakeIntercomClient(1, config=None)
    window._client = engine

    window.closeEvent(QtGui.QCloseEvent())

    assert refresh.abandoned and window._is_closing
    assert not engine.stopped  # its streams go through the PortAudio the inventory holds


def _connected_window(window, monkeypatch) -> _FakeIntercomClient:
    _FakeIntercomClient.instances = []
    monkeypatch.setattr(gui_mod, "IntercomClient", _FakeIntercomClient)
    window._server_ip.setText("127.0.0.1")
    window._input_device.addItem("Micro", 1)
    window._output_device.addItem("Casque", 2)
    window._connect()
    assert window._connected
    return window._client


# --- Dead audio streams ------------------------------------------------------------


def test_disconnect_drops_an_engine_whose_streams_died(client_window, monkeypatch) -> None:
    window = client_window
    engine = _connected_window(window, monkeypatch)
    refreshes: list[bool] = []
    monkeypatch.setattr(window, "_start_device_refresh", lambda: refreshes.append(True))
    engine.streams_active = False  # headset unplugged during the show

    window._disconnect()

    assert engine.stopped and window._client is None
    assert refreshes == [True]  # PortAudio is reinitialized before any new stream


def test_connect_never_reuses_dead_streams_and_resumes_after_the_refresh(qt_app, client_window, monkeypatch) -> None:
    window = client_window
    first = _connected_window(window, monkeypatch)
    window._disconnect()
    assert window._client is first and not first.stopped  # live streams are kept
    refreshes: list[bool] = []
    monkeypatch.setattr(window, "_start_device_refresh", lambda: refreshes.append(True))
    first.streams_active = False  # unplugged while disconnected

    window._connect()

    assert first.stopped and first.reconnects == 0
    assert window._client is None and not window._connected
    assert refreshes == [True]

    window._on_device_refresh_finished()  # the device refresh completes
    qt_app.processEvents()

    assert window._connected
    assert window._client is _FakeIntercomClient.instances[-1] and window._client is not first
    window._disconnect()


def test_vu_meters_read_the_lock_free_levels(client_window, monkeypatch) -> None:
    window = client_window
    _connected_window(window, monkeypatch)

    window._refresh_vu_meters()

    assert window._in_vu.level() == pytest.approx(-12.0)
    assert window._in_vu._dimmed is True  # measured, not sent
    window._disconnect()


# --- Server refusal ------------------------------------------------------------------


def test_server_refusal_disconnects_before_telling_the_operator(client_window, monkeypatch) -> None:
    window = client_window
    engine = _connected_window(window, monkeypatch)
    shown: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        gui_mod.QtWidgets.QMessageBox,
        "warning",
        staticmethod(lambda _parent, _title, text: shown.append((text, window._connected))),
    )
    engine.snapshot = {"control_refusal": "hello name exceeds 128 characters"}

    window._refresh_stats()

    assert not window._connected
    assert len(shown) == 1
    assert "hello name exceeds 128 characters" in shown[0][0]
    assert shown[0][1] is False  # already disconnected while the dialog is up


# --- Degraded link: suspend, never forget nor reopen unasked --------------------------


def test_udp_gap_cuts_transmission_but_keeps_the_hook_and_toggles(client_window) -> None:
    window = client_window
    client = _online(window)
    hotkeys = _FakeHotkeys()
    window._global_ptt = hotkeys
    window._ptt_bus_modes[PLATEAU_BUS_ID] = "toggle"
    window._handle_remote_command(RemoteCommand("toggle", PLATEAU_BUS_ID))
    window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID))
    client.sent.clear()

    window._set_operational_status(_status(ClientOperationalState.DEGRADED))

    assert sorted(client.sent) == [(REGIE_BUS_ID, False), (PLATEAU_BUS_ID, False), (VMIX_BUS_ID, False)]
    assert hotkeys.stop_calls == 0 and window._global_ptt is hotkeys
    assert window._ptt_bus_toggle_state[PLATEAU_BUS_ID] is True
    assert window._ptt_pads[PLATEAU_BUS_ID].accessibleDescription().startswith("SUSPENDU")
    # Still connected: an unarmed pad is suspended too, never "offline".
    assert window._ptt_pads[VMIX_BUS_ID].accessibleDescription().startswith("SUSPENDU")
    assert window._release_all_btn.isEnabled()
    client.sent.clear()

    window._set_operational_status(_status(ClientOperationalState.ONLINE))

    assert hotkeys.stop_calls == 0 and window._global_ptt is hotkeys
    assert (PLATEAU_BUS_ID, True) in client.sent and (REGIE_BUS_ID, True) in client.sent


def test_control_loss_after_a_degraded_link_forgets_everything(client_window) -> None:
    window = client_window
    client = _online(window)
    hotkeys = _FakeHotkeys()
    window._global_ptt = hotkeys
    window._ptt_bus_modes[PLATEAU_BUS_ID] = "toggle"
    window._handle_remote_command(RemoteCommand("toggle", PLATEAU_BUS_ID))
    window._set_operational_status(_status(ClientOperationalState.DEGRADED))

    window._set_operational_status(_status(ClientOperationalState.RECONNECTING))

    assert hotkeys.stop_calls == 1 and window._global_ptt is None
    assert window._ptt_bus_toggle_state[PLATEAU_BUS_ID] is False
    assert client.sent[-3:] == [(REGIE_BUS_ID, False), (PLATEAU_BUS_ID, False), (VMIX_BUS_ID, False)]


def test_release_all_works_while_degraded(client_window) -> None:
    window = client_window
    client = _online(window)
    window._ptt_bus_modes[PLATEAU_BUS_ID] = "toggle"
    window._handle_remote_command(RemoteCommand("toggle", PLATEAU_BUS_ID))
    window._set_operational_status(_status(ClientOperationalState.DEGRADED))

    window._release_all_talk()
    window._set_operational_status(_status(ClientOperationalState.ONLINE))

    assert window._ptt_bus_active(PLATEAU_BUS_ID) is False
    assert client.sent[-3:] == [(REGIE_BUS_ID, False), (PLATEAU_BUS_ID, False), (VMIX_BUS_ID, False)]


def _real_hook(window, bindings: dict[int, tuple[str, str]]) -> GlobalPttHotkeys:
    """The window's hook without the system listener: key events are injected."""
    hotkeys = GlobalPttHotkeys(bindings, None, window, key_is_down=None)
    hotkeys.ptt_changed.connect(window._on_global_ptt_changed, QtCore.Qt.ConnectionType.QueuedConnection)
    window._global_ptt = hotkeys
    return hotkeys


def _tap(qt_app, hotkeys: GlobalPttHotkeys, key) -> None:
    hotkeys._on_press(key)
    hotkeys._on_release(key)
    qt_app.processEvents()


def test_toggle_key_pressed_while_degraded_can_only_cut(qt_app, client_window) -> None:
    window = client_window
    client = _online(window)
    window._ptt_bus_modes[PLATEAU_BUS_ID] = "toggle"
    hotkeys = _real_hook(window, {PLATEAU_BUS_ID: ("toggle", "F2")})

    # Off when the link degrades: a press must not arm it for the return.
    window._set_operational_status(_status(ClientOperationalState.DEGRADED))
    _tap(qt_app, hotkeys, Key.f2)
    window._set_operational_status(_status(ClientOperationalState.ONLINE))
    assert window._ptt_bus_active(PLATEAU_BUS_ID) is False
    assert (PLATEAU_BUS_ID, True) not in client.sent

    # On when the link degrades: a press cuts it for good.
    _tap(qt_app, hotkeys, Key.f2)
    assert window._ptt_bus_active(PLATEAU_BUS_ID) is True
    window._set_operational_status(_status(ClientOperationalState.DEGRADED))
    _tap(qt_app, hotkeys, Key.f2)
    window._set_operational_status(_status(ClientOperationalState.ONLINE))
    assert window._ptt_bus_active(PLATEAU_BUS_ID) is False

    # The hook and the window still agree: the next press arms the bus.
    _tap(qt_app, hotkeys, Key.f2)
    assert window._ptt_bus_active(PLATEAU_BUS_ID) is True


def test_key_held_through_a_udp_gap_resumes_and_a_released_one_does_not(qt_app, client_window) -> None:
    window = client_window
    _online(window)
    hotkeys = _real_hook(window, {REGIE_BUS_ID: ("ptt", "F1"), VMIX_BUS_ID: ("ptt", "F3")})
    hotkeys._on_press(Key.f1)
    hotkeys._on_press(Key.f3)
    qt_app.processEvents()

    window._set_operational_status(_status(ClientOperationalState.DEGRADED))
    hotkeys._on_release(Key.f3)  # released during the gap
    qt_app.processEvents()
    window._set_operational_status(_status(ClientOperationalState.ONLINE))

    assert window._ptt_bus_active(REGIE_BUS_ID) is True
    assert window._ptt_bus_active(VMIX_BUS_ID) is False
    hotkeys._on_release(Key.f1)


# --- Remote lease (hold_ms) ----------------------------------------------------------


def test_leased_remote_on_is_released_when_not_renewed(qt_app, client_window) -> None:
    window = client_window
    client = _online(window)

    window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID, hold_ms=100))
    assert window._ptt_bus_active(REGIE_BUS_ID) is True

    assert _pump(qt_app, lambda: not window._ptt_bus_active(REGIE_BUS_ID))
    assert client.sent[-1] == (REGIE_BUS_ID, False)


def test_leased_toggle_is_cut_when_not_renewed(qt_app, client_window) -> None:
    window = client_window
    _online(window)
    hotkeys = _FakeHotkeys()
    window._global_ptt = hotkeys
    window._ptt_bus_modes[PLATEAU_BUS_ID] = "toggle"

    window._handle_remote_command(RemoteCommand("on", PLATEAU_BUS_ID, hold_ms=100))

    assert _pump(qt_app, lambda: not window._ptt_bus_active(PLATEAU_BUS_ID))
    assert hotkeys.toggle_sync == [(PLATEAU_BUS_ID, True), (PLATEAU_BUS_ID, False)]


def test_a_later_command_supersedes_a_pending_lease(qt_app, client_window) -> None:
    window = client_window
    _online(window)

    window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID, hold_ms=100))
    window._handle_remote_command(RemoteCommand("on", REGIE_BUS_ID, hold_ms=5000))  # renewed
    window._handle_remote_command(RemoteCommand("on", VMIX_BUS_ID, hold_ms=100))
    window._handle_remote_command(RemoteCommand("on", VMIX_BUS_ID))  # now held until "off"

    _pump(qt_app, lambda: False, timeout=0.4)

    assert window._ptt_bus_active(REGIE_BUS_ID) is True
    assert window._ptt_bus_active(VMIX_BUS_ID) is True
    window._handle_remote_command(RemoteCommand("off", None))
