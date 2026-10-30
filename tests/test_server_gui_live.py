"""Server window behaviour that matters during a live show.

The engine is replaced by a recording fake: these tests pin what the window
asks of it (and when), never PortAudio or the network.
"""

from __future__ import annotations

import json
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

import py_intercom.common.gui_utils as gui_utils_mod
import py_intercom.server.gui as gui_mod
from py_intercom.common.buses import FIXED_BUSES, PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID
from py_intercom.common.constants import MAX_GAIN_DB
from py_intercom.common.devices import DeviceInfo
from py_intercom.common.preset_schema import stamp_preset
from py_intercom.common.theme import apply_theme
from py_intercom.server.gui import ServerWindow

REAL_START_DEVICE_REFRESH = ServerWindow._start_device_refresh


def _out(index: int, name: str) -> DeviceInfo:
    return DeviceInfo(index, name, "Windows WASAPI", 0, 2, 48000)


def _in(index: int, name: str) -> DeviceInfo:
    return DeviceInfo(index, name, "Windows WASAPI", 2, 0, 48000)


def _write_preset(path, **data) -> None:
    path.write_text(json.dumps(stamp_preset(data, preset_type="server")), encoding="utf-8")


def _wait_until(condition, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        QtTest.QTest.qWait(10)


class FakeServer:
    """Records the calls the window makes on the engine."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self._return_enabled = bool(kwargs.get("return_enabled", False))
        self._return_input_device = kwargs.get("return_input_device")
        self.clients: dict[int, dict] = {}
        self.outputs: dict[int, dict] = {}
        self.stats: dict = {}
        self.start_error: Exception | None = None
        self.device_error: Exception | None = None

    def load_preset(self) -> bool:
        self.calls.append(("load_preset",))
        return True

    def save_preset(self) -> None:
        self.calls.append(("save_preset",))

    def start(self) -> None:
        self.calls.append(("start",))
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> None:
        self.calls.append(("stop",))

    def get_clients_snapshot(self) -> dict:
        return {cid: dict(st) for cid, st in self.clients.items()}

    def get_buses_snapshot(self) -> dict:
        return {
            d.bus_id: {"bus_id": d.bus_id, "name": d.name, "gain_db": 0.0, "feed_to_regie": False} for d in FIXED_BUSES
        }

    def get_outputs_snapshot(self) -> dict:
        return {oid: dict(st) for oid, st in self.outputs.items()}

    def get_stats_snapshot(self) -> dict:
        return dict(self.stats)

    def set_bus_gain(self, bus_id: int, gain_db: float) -> None:
        self.calls.append(("set_bus_gain", bus_id, gain_db))

    def set_client_input_gain(self, client_id: int, gain_db: float) -> None:
        self.calls.append(("set_client_input_gain", client_id, gain_db))

    def set_output_device(self, output_id: int, device: int) -> None:
        self.calls.append(("set_output_device", output_id, device))
        if self.device_error is not None:
            raise self.device_error

    def set_return_input_device(self, device: int | None) -> None:
        self.calls.append(("set_return_input_device", device))
        if self.device_error is not None:
            raise self.device_error
        self._return_input_device = device

    def remove_output(self, output_id: int) -> None:
        self.calls.append(("remove_output", output_id))

    def forget_client(self, client_id: int) -> None:
        self.calls.append(("forget_client", client_id))

    def reopen_outputs(self, *, force: bool = False) -> None:
        self.calls.append(("reopen_outputs", force))

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_theme(app)
    return app


@pytest.fixture
def preset_path(monkeypatch, tmp_path):
    """Keep the window away from the operator's real preset and settings."""
    real_settings = QtCore.QSettings
    ini = str(tmp_path / "server-gui.ini")
    monkeypatch.setattr(QtCore, "QSettings", lambda *_args: real_settings(ini, real_settings.Format.IniFormat))
    path = tmp_path / "server_preset.json"
    monkeypatch.setattr(ServerWindow, "_preset_paths", lambda _self: path)
    return path


@pytest.fixture
def make_window(qt_app, preset_path, monkeypatch):
    monkeypatch.setattr(ServerWindow, "_start_device_refresh", lambda _self: None)
    windows: list[ServerWindow] = []

    def make() -> ServerWindow:
        window = ServerWindow()
        windows.append(window)
        return window

    yield make
    for window in windows:
        window._server = None
        window.close()
        window.deleteLater()
    qt_app.processEvents()


@pytest.fixture
def window(make_window) -> ServerWindow:
    return make_window()


@pytest.fixture
def running(window) -> FakeServer:
    server = FakeServer()
    window._server = server
    return server


@pytest.fixture
def started(window, preset_path, monkeypatch) -> FakeServer:
    """Start through the real start path with a fake engine."""
    created: list[FakeServer] = []

    def factory(**kwargs) -> FakeServer:
        created.append(FakeServer(**kwargs))
        return created[-1]

    monkeypatch.setattr(gui_mod, "IntercomServer", factory)
    monkeypatch.setattr(gui_mod, "list_devices", lambda **_kwargs: list(window._devices_cache))
    window._start_server()
    window._timer.stop()
    window._vu_timer.stop()
    return created[-1]


# -- sgui-01 ------------------------------------------------------------------


def test_preview_never_shows_another_card_for_a_missing_output(window, preset_path) -> None:
    _write_preset(
        preset_path,
        outputs=[
            {"device": 7, "device_name": "USB Plateau", "device_hostapi": "Windows WASAPI", "bus_id": PLATEAU_BUS_ID},
        ],
    )
    window._populate_device_combos([_out(3, "Speakers")])

    window._load_preset_preview()

    combo = window._output_device_widgets[0]
    assert combo.currentIndex() == -1
    assert "USB Plateau" in combo.placeholderText()
    assert window._output_status_widgets[0].state == "error"


def test_start_does_not_rewrite_the_preset(window, started) -> None:
    assert started.named("load_preset")
    assert not started.named("save_preset")


# -- sgui-02 ------------------------------------------------------------------


@pytest.fixture
def blocking_inventory(monkeypatch):
    release = threading.Event()

    def list_devices(**_kwargs):
        release.wait(5.0)
        return [_out(1, "Speakers"), _in(2, "Programme")]

    monkeypatch.setattr(gui_utils_mod, "list_devices", list_devices)
    monkeypatch.setattr(gui_utils_mod, "DEVICE_REFRESH_TIMEOUT_MS", 30)
    yield release
    release.set()


def _wait_for_timeout(window) -> None:
    stalled: list[bool] = []
    window._device_refresh.timed_out.connect(lambda: stalled.append(True))
    _wait_until(lambda: stalled)


def test_late_device_inventory_is_kept_and_releases_the_autostart(window, blocking_inventory) -> None:
    autostarts: list[bool] = []
    window._maybe_autostart = lambda: autostarts.append(True)

    REAL_START_DEVICE_REFRESH(window)
    _wait_for_timeout(window)
    assert not window._start_btn.isEnabled()
    assert not window._return_input_device.isEnabled()

    blocking_inventory.set()
    _wait_until(lambda: not window._device_refresh.running and autostarts)

    assert window._last_device_error == ""
    assert window._add_output_device.count() == 1
    assert window._start_btn.isEnabled()
    assert window._return_input_device.isEnabled()


def test_window_closes_while_the_inventory_is_stuck(qt_app, window, blocking_inventory) -> None:
    window.show()
    REAL_START_DEVICE_REFRESH(window)
    _wait_for_timeout(window)
    thread = window._device_refresh._thread

    assert window.close()

    # Kept alive by the application, whose run loop then ends the process.
    assert thread.parent() is qt_app and thread.isRunning()
    ended: list[bool] = []
    thread.finished.connect(lambda: ended.append(True))  # the thread then deletes itself
    blocking_inventory.set()
    _wait_until(lambda: ended)


def test_close_waits_for_the_inventory_then_completes(window, blocking_inventory, monkeypatch) -> None:
    monkeypatch.setattr(gui_utils_mod, "DEVICE_REFRESH_TIMEOUT_MS", 5000)
    window.show()
    REAL_START_DEVICE_REFRESH(window)

    assert not window.close()  # deferred until the inventory ends

    blocking_inventory.set()
    _wait_until(lambda: not window.isVisible())
    assert window._is_closing


# -- sgui-03 ------------------------------------------------------------------


def test_fader_drag_pushes_the_first_and_last_gain_only(window, running) -> None:
    for value in range(-30, 1):
        window._on_bus_gain_changed(PLATEAU_BUS_ID, value)

    assert running.named("set_bus_gain") == [("set_bus_gain", PLATEAU_BUS_ID, -30.0)]
    assert window._bus_gain_labels[PLATEAU_BUS_ID].text() == "0 dB"

    _wait_until(lambda: len(running.named("set_bus_gain")) == 2)
    assert running.named("set_bus_gain")[-1] == ("set_bus_gain", PLATEAU_BUS_ID, 0.0)


def test_stop_applies_a_gain_still_pending(window, running) -> None:
    window._on_client_input_gain_changed(5, -10)
    window._on_client_input_gain_changed(5, -4)

    window._stop_server()

    assert running.named("set_client_input_gain")[-1] == ("set_client_input_gain", 5, -4.0)
    assert running.calls.index(("set_client_input_gain", 5, -4.0)) < running.calls.index(("stop",))


# -- sgui-05 ------------------------------------------------------------------


def test_failed_output_device_change_is_reported_not_raised(window, running) -> None:
    window._populate_device_combos([_out(1, "Speakers"), _out(2, "USB")])
    running.outputs = {0: {"output_id": 0, "device": 1, "bus_id": PLATEAU_BUS_ID, "gain_db": 0.0}}
    window._refresh_output_strips(running.get_outputs_snapshot(), running.get_buses_snapshot())
    running.device_error = RuntimeError("device busy")

    window._output_device_widgets[0].setCurrentIndex(1)

    assert running.named("set_output_device") == [("set_output_device", 0, 2)]
    assert "device busy" in window._device_status.text()


def test_failed_return_change_shows_the_engine_input_again(window, running) -> None:
    window._populate_device_combos([_in(4, "Programme"), _in(5, "Console")])
    running._return_input_device = 4
    window._select_return_input(4)
    running.device_error = RuntimeError("exclusive mode")

    window._return_input_device.setCurrentIndex(window._return_input_device.findData(5))

    assert window._return_input_device.currentData() == 4
    assert "exclusive mode" in window._device_status.text()


# -- sgui-07 / srt-03 ---------------------------------------------------------


def test_only_a_failed_output_offers_a_reopen_that_spares_healthy_streams(window, running) -> None:
    window._populate_device_combos([_out(1, "Speakers"), _out(2, "USB")])
    running.outputs = {
        0: {"output_id": 0, "device": 1, "bus_id": REGIE_BUS_ID, "gain_db": 0.0, "stream_active": True},
        1: {
            "output_id": 1,
            "device": 2,
            "bus_id": PLATEAU_BUS_ID,
            "gain_db": 0.0,
            "stream_active": False,
            "stream_error": "output stream is inactive",
        },
    }
    window._refresh_output_strips(running.get_outputs_snapshot(), running.get_buses_snapshot())

    assert window._output_reopen_widgets[0].isHidden()
    assert not window._output_reopen_widgets[1].isHidden()

    window._output_reopen_widgets[1].click()

    assert running.named("reopen_outputs") == [("reopen_outputs", False)]


# -- sgui-08 ------------------------------------------------------------------


def test_server_name_and_announce_survive_a_restart(make_window) -> None:
    first = make_window()
    first._server_name.setText("Régie A")
    first._server_name.editingFinished.emit()
    first._discovery_cb.click()

    second = make_window()

    assert second._server_name.text() == "Régie A"
    assert not second._discovery_cb.isChecked()


# -- sgui-09 / sgui-10 --------------------------------------------------------


def test_stopped_return_choices_are_saved_without_enumerating(window, preset_path, monkeypatch) -> None:
    def no_enumeration(**_kwargs):
        raise AssertionError("PortAudio must not be enumerated on the GUI thread")

    monkeypatch.setattr(gui_mod, "list_devices", no_enumeration)
    window._populate_device_combos([_in(4, "Programme")])

    window._return_enabled.setChecked(True)
    window._return_input_device.setCurrentIndex(window._return_input_device.findData(4))
    window._preset_writer.flush()

    saved = json.loads(preset_path.read_text(encoding="utf-8"))
    assert saved["return_enabled"] is True
    assert saved["return_input_device"] == 4
    assert saved["return_input_device_name"] == "Programme"
    assert saved["return_input_device_hostapi"] == "Windows WASAPI"


def test_preview_does_not_write_the_preset_back(window, preset_path) -> None:
    legacy = {"version": 1, "return_enabled": True, "return_input_device_name": "Programme"}
    preset_path.write_text(json.dumps(legacy), encoding="utf-8")
    window._populate_device_combos([_in(4, "Programme")])

    window._load_preset_preview()

    assert window._return_enabled.isChecked()
    assert window._return_input_device.currentData() == 4
    assert json.loads(preset_path.read_text(encoding="utf-8")) == legacy


# -- sgui-11 ------------------------------------------------------------------


def test_selectors_follow_portaudio_renumbering(window, monkeypatch) -> None:
    window._populate_device_combos([_out(3, "Speakers"), _out(4, "USB"), _in(5, "Programme")])
    window._select_return_input(5)
    monkeypatch.setattr(
        gui_mod,
        "list_devices",
        lambda **_kwargs: [_out(3, "Recorder"), _out(4, "Speakers"), _out(5, "USB"), _in(6, "Programme")],
    )

    window._remap_device_indices()

    combo = window._add_output_device
    labels = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
    assert set(labels) == {4, 5}
    assert labels[4].startswith("Speakers") and labels[5].startswith("USB")
    assert window._return_input_device.findData(6) > 0


# -- sgui-12 ------------------------------------------------------------------


def test_live_audio_cuts_ask_first(window, running, monkeypatch) -> None:
    answers: list[bool] = []
    asked: list[str] = []

    def confirm(title: str, _text: str) -> bool:
        asked.append(title)
        return answers.pop(0)

    monkeypatch.setattr(window, "_confirm", confirm)
    running.clients = {7: {"name": "Cadreur", "control_connected": True}}

    answers.append(False)
    window._on_remove_output(0)
    answers.append(False)
    window._select_client(7)
    window._forget_selected_client()
    answers.append(False)
    window._on_stop_clicked()

    assert len(asked) == 3
    assert not running.named("remove_output")
    assert not running.named("forget_client")
    assert window._server is running

    answers.append(True)
    window._on_remove_output(0)
    assert running.named("remove_output") == [("remove_output", 0)]


def test_closing_the_window_asks_before_cutting_online_stations(window, running, monkeypatch) -> None:
    answers = [False]
    monkeypatch.setattr(window, "_confirm", lambda *_args: answers.pop(0))
    running.clients = {7: {"name": "Cadreur", "control_connected": True}}

    event = QtGui.QCloseEvent()
    window.closeEvent(event)

    assert not event.isAccepted()
    assert window._server is running


def test_offline_client_is_forgotten_without_question(window, running, monkeypatch) -> None:
    monkeypatch.setattr(window, "_confirm", lambda *_args: pytest.fail("no question expected"))
    running.clients = {7: {"name": "Cadreur", "control_connected": False}}

    window._select_client(7)
    window._forget_selected_client()
    window._on_stop_clicked()

    assert running.named("forget_client") == [("forget_client", 7)]
    assert window._server is None


# -- sgui-13 ------------------------------------------------------------------


def test_failed_start_frees_the_engine_and_restores_the_preview(window, preset_path, monkeypatch) -> None:
    _write_preset(
        preset_path,
        outputs=[
            {"device": 3, "device_name": "Speakers", "device_hostapi": "Windows WASAPI", "bus_id": PLATEAU_BUS_ID}
        ],
        clients={"11111111-2222-3333-4444-555555555555": {"name": "Cadreur"}},
    )
    window._populate_device_combos([_out(3, "Speakers")])
    created: list[FakeServer] = []

    def factory(**kwargs) -> FakeServer:
        created.append(FakeServer(**kwargs))
        created[-1].start_error = RuntimeError("Opus unavailable")
        return created[-1]

    monkeypatch.setattr(gui_mod, "IntercomServer", factory)
    monkeypatch.setattr(gui_mod, "list_devices", lambda **_kwargs: list(window._devices_cache))
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", lambda *_args: None)

    window._start_server()

    assert window._server is None
    assert created[0].named("stop")  # releases the UDP port bound by the constructor
    assert list(window._output_strips) == [0]
    assert len(window._client_strips) == 1


# -- sgui-14 ------------------------------------------------------------------


def test_structure_refresh_leaves_live_output_meters_to_the_vu_loop(window, running) -> None:
    running.outputs = {
        0: {"output_id": 0, "device": 1, "bus_id": REGIE_BUS_ID, "gain_db": 0.0, "vu_dbfs": 0.0, "stream_active": True}
    }
    window._refresh_output_strips(running.get_outputs_snapshot(), running.get_buses_snapshot())

    window._refresh_vu_and_health()
    window._refresh_output_strips(running.get_outputs_snapshot(), running.get_buses_snapshot())

    assert window._output_vu_widgets[0].level() == 0.0  # a clipping output stays visible


# -- sgui-20 ------------------------------------------------------------------


def test_rack_is_not_rebuilt_under_a_held_fader(window, running) -> None:
    running.clients = {7: {"name": "Cadreur"}}
    window._refresh_client_strips(running.get_clients_snapshot())
    held = window._input_gain_widgets[7]
    held.setSliderDown(True)

    running.clients[8] = {"name": "Perche"}
    window._refresh_client_strips(running.get_clients_snapshot())
    assert window._input_gain_widgets[7] is held
    assert 8 not in window._client_strips

    held.setSliderDown(False)
    window._refresh_client_strips(running.get_clients_snapshot())
    assert set(window._client_strips) == {7, 8}


# -- Stopped preset edits leave the Qt thread -----------------------------------


def test_stopped_edits_are_written_off_the_qt_thread_and_none_is_lost(window, preset_path, monkeypatch) -> None:
    client_uuid = "11111111-2222-3333-4444-555555555555"
    _write_preset(preset_path, clients={client_uuid: {"name": "Cadreur"}})
    window._populate_device_combos([_in(4, "Programme")])
    window._load_preset_preview()
    writers: list[threading.Thread] = []
    real_write = gui_mod.atomic_write_json

    def write(path, data) -> None:
        writers.append(threading.current_thread())
        real_write(path, data)

    monkeypatch.setattr(gui_mod, "atomic_write_json", write)

    window._return_input_device.setCurrentIndex(window._return_input_device.findData(4))
    window._select_client(next(iter(window._client_strips)))
    window._forget_selected_client()

    # Not written yet, but every view already shows both edits.
    assert not window._client_strips
    assert window._read_server_preset()["return_input_device_name"] == "Programme"
    window._preset_writer.flush()
    _wait_until(lambda: window._preset_edit is None)

    assert writers and threading.main_thread() not in writers
    saved = json.loads(preset_path.read_text(encoding="utf-8"))
    assert saved["clients"] == {}
    assert saved["return_input_device_name"] == "Programme"


def test_reset_config_is_not_undone_by_a_pending_edit(window, preset_path, monkeypatch) -> None:
    _write_preset(preset_path, return_enabled=False)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.StandardButton.Yes)

    window._return_enabled.setChecked(True)
    window._reset_total_config()
    window._preset_writer.flush()

    assert not preset_path.exists()
    assert not window._return_enabled.isChecked()


def test_start_lands_a_pending_edit_before_the_engine_loads_it(window, preset_path, monkeypatch) -> None:
    _write_preset(preset_path, return_enabled=False)
    seen: list[bool] = []

    class LoadingServer(FakeServer):
        def load_preset(self) -> bool:
            seen.append(json.loads(preset_path.read_text(encoding="utf-8"))["return_enabled"])
            return super().load_preset()

    monkeypatch.setattr(gui_mod, "IntercomServer", LoadingServer)
    monkeypatch.setattr(gui_mod, "list_devices", lambda **_kwargs: [])

    window._return_enabled.setChecked(True)
    window._start_server()
    window._timer.stop()
    window._vu_timer.stop()

    assert seen == [True]


# -- sgui-16 ------------------------------------------------------------------


def test_preview_shows_what_the_engine_will_load(window, preset_path) -> None:
    _write_preset(
        preset_path,
        outputs=[
            "not an output",
            {"device": 3, "device_name": "Speakers", "device_hostapi": "Windows WASAPI", "bus_id": 9, "gain_db": 99},
        ],
    )
    window._populate_device_combos([_out(3, "Speakers")])

    window._load_preset_preview()

    assert list(window._output_strips) == [0]  # ids are compact, as in the engine
    assert window._output_bus_widgets[0].currentData() == PLATEAU_BUS_ID  # unknown bus kept on Plateau
    assert window._output_gain_widgets[0].value() == int(MAX_GAIN_DB)


# -- srt-11 / sgui-21 -----------------------------------------------------------


def test_online_client_strip_shows_its_transport_state(window, running) -> None:
    running.clients = {
        7: {"name": "Cadreur", "control_connected": True, "addr": ("10.0.0.7", 5000), "age_s": 0.1, "ptt_buses": {}}
    }

    window._refresh_client_strips(running.get_clients_snapshot())

    assert window._status_widgets[7].state == "idle"
    assert window._client_strips[7].caption_label.text() == "10.0.0.7"


def test_bus_meters_show_the_bus_even_without_an_output(window, running) -> None:
    running.stats = {"bus_vu_dbfs": {REGIE_BUS_ID: -30.0, PLATEAU_BUS_ID: -60.0, VMIX_BUS_ID: -12.0}}

    window._refresh_vu_and_health()

    assert window._bus_vu_widgets[VMIX_BUS_ID].level() == -12.0
    assert window._bus_vu_widgets[PLATEAU_BUS_ID].level() == -60.0
