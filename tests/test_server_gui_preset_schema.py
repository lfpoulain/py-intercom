from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtWidgets

import py_intercom.server.gui as gui_mod
from py_intercom.common.buses import FIXED_BUSES, PLATEAU_BUS_ID
from py_intercom.common.devices import DeviceInfo
from py_intercom.common.jsonio import JsonFileStatus
from py_intercom.common.preset_schema import PRESET_SCHEMA_VERSION, stamp_preset
from py_intercom.common.theme import apply_theme
from py_intercom.server.gui import ServerWindow


def _read_fake(path):
    fake = SimpleNamespace(
        _preset_paths=lambda: path,
        _preset_edit=None,
        _preset_load_problem=None,
        _preset_problem_notified=None,
        _show_server_preset_problem=lambda: None,
    )
    loaded = ServerWindow._read_server_preset(fake)
    return fake, loaded


def test_missing_server_gui_preset_uses_current_schema(tmp_path) -> None:
    fake, loaded = _read_fake(tmp_path / "missing.json")

    assert fake._preset_load_problem is None
    assert loaded == {
        "preset_type": "server",
        "schema_version": PRESET_SCHEMA_VERSION,
    }


def test_server_gui_legacy_preview_is_migrated_only_in_memory(tmp_path) -> None:
    path = tmp_path / "server.json"
    legacy = {
        "version": 1,
        "return_enabled": True,
        "plugin_extension": {"enabled": True},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    fake, loaded = _read_fake(path)

    assert fake._preset_load_problem is None
    assert loaded["preset_type"] == "server"
    assert loaded["schema_version"] == PRESET_SCHEMA_VERSION
    assert loaded["plugin_extension"] == {"enabled": True}
    assert "version" not in loaded
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


@pytest.mark.parametrize(
    "incompatible",
    [
        {
            "preset_type": "server",
            "schema_version": PRESET_SCHEMA_VERSION + 1,
        },
        {
            "preset_type": "client",
            "schema_version": PRESET_SCHEMA_VERSION,
        },
    ],
)
def test_server_gui_blocks_incompatible_preview_without_overwrite(
    tmp_path,
    incompatible,
) -> None:
    path = tmp_path / "server.json"
    path.write_text(json.dumps(incompatible), encoding="utf-8")
    original = path.read_bytes()

    fake, loaded = _read_fake(path)

    assert loaded is None
    assert fake._preset_load_problem[0] is JsonFileStatus.INVALID
    assert path.read_bytes() == original


# -- Saved devices that do not resolve ----------------------------------------

_SAVED_DEVICES = {
    "return_enabled": True,
    "return_input_device": 9,
    "return_input_device_name": "Programme",
    "return_input_device_hostapi": "Windows WASAPI",
    "outputs": [
        {"device": 7, "device_name": "USB Plateau", "device_hostapi": "Windows WASAPI", "bus_id": PLATEAU_BUS_ID},
    ],
}


class _ReturnDroppingServer:
    """Engine whose preset load could not find the saved Return card."""

    def __init__(self, **kwargs) -> None:
        self._return_enabled = bool(kwargs.get("return_enabled", False))
        self._return_input_device = kwargs.get("return_input_device")

    def load_preset(self) -> bool:
        self._return_enabled = True
        self._return_input_device = None
        return True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_clients_snapshot(self) -> dict:
        return {}

    def get_buses_snapshot(self) -> dict:
        return {
            d.bus_id: {"bus_id": d.bus_id, "name": d.name, "gain_db": 0.0, "feed_to_regie": False} for d in FIXED_BUSES
        }

    def get_outputs_snapshot(self) -> dict:
        return {}

    def get_stats_snapshot(self) -> dict:
        return {}


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_theme(app)
    real_settings = QtCore.QSettings
    ini = str(tmp_path / "server-gui.ini")
    monkeypatch.setattr(QtCore, "QSettings", lambda *_args: real_settings(ini, real_settings.Format.IniFormat))
    path = tmp_path / "server_preset.json"
    path.write_text(json.dumps(stamp_preset(dict(_SAVED_DEVICES), preset_type="server")), encoding="utf-8")
    monkeypatch.setattr(ServerWindow, "_preset_paths", lambda _self: path)
    monkeypatch.setattr(ServerWindow, "_start_device_refresh", lambda _self: None)
    win = ServerWindow()
    yield win
    win._server = None
    win._timer.stop()
    win._vu_timer.stop()
    win.close()
    win.deleteLater()
    app.processEvents()


def test_preview_without_inventory_does_not_report_cards_missing(window) -> None:
    window._load_preset_preview()

    output = window._output_device_widgets[0]
    assert output.currentIndex() == -1
    assert output.placeholderText() == "inventaire indisponible : USB Plateau"
    assert window._output_status_widgets[0].state == "unknown"
    combo = window._return_input_device
    assert combo.currentIndex() == -1
    assert combo.placeholderText() == "inventaire indisponible : Programme"
    assert window._return_strip.caption_label.text() == "inventaire indisponible : Programme"


def test_preview_with_inventory_reports_missing_cards(window) -> None:
    window._populate_device_combos([DeviceInfo(3, "Speakers", "Windows WASAPI", 0, 2, 48000)])

    window._load_preset_preview()

    assert window._output_device_widgets[0].placeholderText() == "absent : USB Plateau"
    assert window._output_status_widgets[0].state == "error"
    assert window._return_input_device.placeholderText() == "absent : Programme"


def test_start_names_the_return_card_the_engine_could_not_find(window, monkeypatch) -> None:
    inventory = [DeviceInfo(4, "Micro", "Windows WASAPI", 2, 0, 48000)]
    monkeypatch.setattr(gui_mod, "IntercomServer", _ReturnDroppingServer)
    monkeypatch.setattr(gui_mod, "list_devices", lambda **_kwargs: list(inventory))
    window._populate_device_combos(inventory)
    window._load_preset_preview()

    window._start_server()

    combo = window._return_input_device
    assert isinstance(window._server, _ReturnDroppingServer)
    assert combo.currentIndex() == -1
    assert combo.placeholderText() == "absent : Programme"
    assert window._return_strip.caption_label.text() == "absent : Programme"
