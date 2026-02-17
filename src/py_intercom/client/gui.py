from __future__ import annotations

import sys
import socket
import uuid
import zlib
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
from pynput import keyboard

from ..common.devices import list_devices
from ..common.discovery import DiscoveryListener
from ..common.jsonio import atomic_write_json, read_json_file
from ..common.theme import VuMeter, apply_theme, patch_combo
from .client import ClientConfig, IntercomClient


class _DeviceWorker(QtCore.QObject):
    finished = QtCore.Signal(object, str)

    def __init__(self, hostapi_filter: Optional[str]):
        super().__init__()
        self._hostapi_filter = hostapi_filter

    @QtCore.Slot()
    def run(self) -> None:
        try:
            devs = list_devices(hostapi_substring=self._hostapi_filter, hard_refresh=True, validate=True)
            self.finished.emit(devs, "")
        except Exception as e:
            self.finished.emit([], str(e))


def _is_checked(state) -> bool:
    try:
        state_val = int(state.value) if hasattr(state, "value") else int(state)
    except Exception:
        return False

    try:
        checked_val = int(QtCore.Qt.CheckState.Checked.value)
    except Exception:
        checked_val = 2

    return int(state_val) == int(checked_val)


def _db_to_progress(db: float) -> int:
    db = max(-60.0, min(0.0, float(db)))
    return int(round((db + 60.0) / 60.0 * 100.0))


class _ShortcutKeySequenceEdit(QtWidgets.QKeySequenceEdit):
    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        try:
            if event.key() == int(QtCore.Qt.Key.Key_Escape):
                self.clearFocus()
                return
            if event.key() in (int(QtCore.Qt.Key.Key_Delete), int(QtCore.Qt.Key.Key_Backspace)):
                self.setKeySequence(QtGui.QKeySequence(""))
                self.clearFocus()
                return
        except Exception:
            pass
        super().keyPressEvent(event)


def _norm_key(k) -> Optional[str]:
    try:
        if isinstance(k, keyboard.Key):
            return f"key:{k.name}"
        if isinstance(k, keyboard.KeyCode):
            if k.char:
                return f"char:{str(k.char).lower()}"
            if k.vk is not None:
                return f"vk:{int(k.vk)}"
    except Exception:
        return None
    return None


def _parse_qt_shortcut(seq_text: str) -> list[set[str]]:
    if not seq_text:
        return []

    tokens = [t.strip() for t in str(seq_text).split("+") if t.strip()]
    groups: list[set[str]] = []

    def _mod_group(name: str) -> set[str]:
        n = str(name).lower()
        if n in ("ctrl", "control"):
            return {"key:ctrl", "key:ctrl_l", "key:ctrl_r"}
        if n == "shift":
            return {"key:shift", "key:shift_l", "key:shift_r"}
        if n in ("alt", "option"):
            return {"key:alt", "key:alt_l", "key:alt_r", "key:alt_gr"}
        if n in ("meta", "win", "super", "cmd", "command"):
            return {"key:cmd", "key:cmd_l", "key:cmd_r"}
        return set()

    special = {
        "space": {"key:space"},
        "tab": {"key:tab"},
        "enter": {"key:enter"},
        "return": {"key:enter"},
        "esc": {"key:esc"},
        "escape": {"key:esc"},
        "backspace": {"key:backspace"},
        "delete": {"key:delete"},
        "insert": {"key:insert"},
        "home": {"key:home"},
        "end": {"key:end"},
        "pageup": {"key:page_up"},
        "pagedown": {"key:page_down"},
        "up": {"key:up"},
        "down": {"key:down"},
        "left": {"key:left"},
        "right": {"key:right"},
    }

    for t in tokens:
        mg = _mod_group(t)
        if mg:
            groups.append(mg)
            continue

        low = str(t).lower()
        if low in special:
            groups.append(set(special[low]))
            continue

        if low.startswith("f") and low[1:].isdigit():
            groups.append({f"key:{low}"})
            continue

        if len(low) == 1:
            groups.append({f"char:{low}"})
            continue

        groups.append({f"char:{low}"})

    return groups


class _GlobalPttHotkeys:
    def __init__(self, window: "ClientWindow") -> None:
        self._window = window
        self._listener: Optional[keyboard.Listener] = None
        self._pressed: set[str] = set()
        self._active_buses: dict[int, bool] = {}

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
        except Exception:
            pass
        self._listener = None
        self._pressed.clear()
        self._active_buses = {}

    def _combo_active(self, groups: list[set[str]]) -> bool:
        if not groups:
            return False
        for g in groups:
            if not any(k in self._pressed for k in g):
                return False
        return True

    def _update(self) -> None:
        cli = self._window._client
        if cli is None:
            return

        try:
            bus_items = list(self._window._ptt_bus_keys.items())
        except Exception:
            bus_items = []

        for bid, edit in bus_items:
            mode = self._window._bus_mode_value(int(bid))
            if str(mode) == "always_on":
                active = True
            else:
                groups = _parse_qt_shortcut(edit.keySequence().toString())
                active = self._combo_active(groups)
            prev = bool(self._active_buses.get(int(bid), False))
            if bool(active) != prev:
                self._active_buses[int(bid)] = bool(active)
                try:
                    cli.set_ptt_bus(int(bid), bool(active))
                except Exception:
                    pass

    def _on_press(self, key) -> None:
        k = _norm_key(key)
        if k:
            self._pressed.add(k)
            self._update()

    def _on_release(self, key) -> None:
        k = _norm_key(key)
        if k and k in self._pressed:
            try:
                self._pressed.remove(k)
            except KeyError:
                pass
            self._update()


class ClientWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Py-Intercom Client")
        self.setMinimumSize(480, 560)

        self._client: Optional[IntercomClient] = None
        self._connected: bool = False

        # Status bar
        self._status_bar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_label = QtWidgets.QLabel("Disconnected")
        self._status_bar.addWidget(self._status_label, 1)

        self._preset: dict = {}
        self._preset = self._load_preset()

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        self._discovered_servers = QtWidgets.QComboBox()
        patch_combo(self._discovered_servers)
        self._discovered_servers.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._discovered_servers.setMinimumContentsLength(30)
        self._discovered_servers.addItem("(manual)", None)
        self._discovered_servers.currentIndexChanged.connect(self._on_discovered_server_selected)

        self._discovery_listener: Optional[DiscoveryListener] = None
        self._discovery_pending_update: bool = False
        self._discovery_signal = QtCore.QTimer(self)
        self._discovery_signal.setInterval(100)
        self._discovery_signal.timeout.connect(self._poll_discovery_updates)
        self._start_discovery_listener()

        self._server_ip = QtWidgets.QLineEdit(str(self._preset_get("server_ip", "")))
        self._server_port = QtWidgets.QSpinBox()
        self._server_port.setRange(1, 65535)
        try:
            self._server_port.setValue(int(self._preset_get("server_port", 5000)))
        except Exception:
            self._server_port.setValue(5000)

        client_uuid = str(self._preset_get("client_uuid", ""))
        if not client_uuid:
            client_uuid = str(uuid.uuid4())
            self._preset_set("client_uuid", client_uuid)
        stable_id = int(zlib.crc32(client_uuid.encode("utf-8")) & 0xFFFFFFFF)

        self._client_id_label = QtWidgets.QLabel("Client ID")
        self._client_id = QtWidgets.QLineEdit(str(stable_id))
        self._client_id.setReadOnly(True)
        self._client_id_label.setVisible(False)
        self._client_id.setVisible(False)

        self._name = QtWidgets.QLineEdit(str(self._preset_get("name", "")))
        self._ptt_bus_keys: dict[int, QtWidgets.QKeySequenceEdit] = {}
        self._ptt_bus_clear: dict[int, QtWidgets.QToolButton] = {}
        self._mode_bus_widgets: dict[int, QtWidgets.QComboBox] = {}
        self._mute_bus_widgets: dict[int, QtWidgets.QCheckBox] = {}
        self._route_bus_widgets: dict[int, QtWidgets.QCheckBox] = {}
        self._shortcut_widgets: list[QtWidgets.QWidget] = []
        self._known_buses: dict[int, dict] = {
            0: {"bus_id": 0, "name": "Regie", "bus_type": "communication", "feed_to_regie": False},
            1: {"bus_id": 1, "name": "Plateau", "bus_type": "diffusion", "feed_to_regie": True},
            2: {"bus_id": 2, "name": "VMix", "bus_type": "diffusion", "feed_to_regie": True},
        }
        self._bus_rows_widget = QtWidgets.QWidget()
        self._bus_rows_layout = QtWidgets.QGridLayout(self._bus_rows_widget)
        self._bus_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._bus_rows_layout.setHorizontalSpacing(8)
        self._bus_rows_layout.setVerticalSpacing(4)
        self._bus_rows_signature: tuple[int, ...] = tuple()

        self._sync_bus_widgets(self._known_buses, apply_saved=True)

        self._input_device = QtWidgets.QComboBox()
        patch_combo(self._input_device)
        self._output_device = QtWidgets.QComboBox()
        patch_combo(self._output_device)
        self._input_device.currentIndexChanged.connect(self._on_input_device_changed)
        self._output_device.currentIndexChanged.connect(self._on_output_device_changed)
        self._show_all_devices = QtWidgets.QCheckBox("Show all devices")
        self._refresh_devices_btn = QtWidgets.QPushButton("Refresh devices")
        self._refresh_devices_btn.setProperty("class", "warning")
        self._device_status = QtWidgets.QLabel("")

        self._connect_btn = QtWidgets.QPushButton("Connect")
        self._connect_btn.setProperty("class", "success")
        self._disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self._disconnect_btn.setProperty("class", "danger")
        self._disconnect_btn.setEnabled(False)

        self._mute = QtWidgets.QCheckBox("Mute mic")
        try:
            self._mute.setChecked(bool(self._preset_get("muted", False)))
        except Exception:
            self._mute.setChecked(False)
        self._mute.setEnabled(False)

        self._listen_return_bus = QtWidgets.QCheckBox("Listen return bus")
        self._listen_return_bus.setEnabled(False)
        try:
            self._listen_return_bus.setChecked(bool(self._preset_get("listen_return_bus", False)))
        except Exception:
            pass

        self._sidetone = QtWidgets.QCheckBox("Sidetone (hear self locally)")
        try:
            self._sidetone.setChecked(bool(self._preset_get("sidetone_enabled", False)))
        except Exception:
            self._sidetone.setChecked(False)
        self._sidetone.setEnabled(False)

        self._sidetone_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._sidetone_gain.setMinimum(-60)
        self._sidetone_gain.setMaximum(0)
        try:
            self._sidetone_gain.setValue(int(self._preset_get("sidetone_gain_db", -12)))
        except Exception:
            self._sidetone_gain.setValue(-12)
        self._sidetone_gain.setEnabled(False)
        self._sidetone_gain_lbl = QtWidgets.QLabel(f"{int(self._sidetone_gain.value())} dB")
        self._sidetone_gain_lbl.setFixedWidth(50)
        self._sidetone_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._mic_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._mic_gain.setMinimum(-60)
        self._mic_gain.setMaximum(12)
        try:
            self._mic_gain.setValue(int(self._preset_get("input_gain_db", 0)))
        except Exception:
            self._mic_gain.setValue(0)
        self._mic_gain.setEnabled(False)
        self._mic_gain_lbl = QtWidgets.QLabel(f"{int(self._mic_gain.value())} dB")
        self._mic_gain_lbl.setFixedWidth(50)
        self._mic_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._hp_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._hp_gain.setMinimum(-60)
        self._hp_gain.setMaximum(12)
        try:
            self._hp_gain.setValue(int(self._preset_get("output_gain_db", 0)))
        except Exception:
            self._hp_gain.setValue(0)
        self._hp_gain.setEnabled(False)
        self._hp_gain_lbl = QtWidgets.QLabel(f"{int(self._hp_gain.value())} dB")
        self._hp_gain_lbl.setFixedWidth(50)
        self._hp_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._in_vu = VuMeter()
        self._out_vu = VuMeter()
        self._return_vu = VuMeter()

        # -- Connection group --
        conn_box = QtWidgets.QGroupBox("Connection")
        conn_lay = QtWidgets.QGridLayout(conn_box)
        conn_lay.setContentsMargins(6, 4, 6, 4)
        conn_lay.setVerticalSpacing(2)
        conn_lay.addWidget(QtWidgets.QLabel("Discovered"), 0, 0)
        conn_lay.addWidget(self._discovered_servers, 0, 1, 1, 3)
        conn_lay.addWidget(QtWidgets.QLabel("Server IP"), 1, 0)
        conn_lay.addWidget(self._server_ip, 1, 1)
        conn_lay.addWidget(QtWidgets.QLabel("Port"), 1, 2)
        conn_lay.addWidget(self._server_port, 1, 3)
        conn_lay.addWidget(self._client_id_label, 2, 0)
        conn_lay.addWidget(self._client_id, 2, 1, 1, 3)
        conn_lay.addWidget(QtWidgets.QLabel("Name"), 3, 0)
        conn_lay.addWidget(self._name, 3, 1, 1, 3)

        # -- Audio devices group --
        dev_box = QtWidgets.QGroupBox("Audio Devices")
        dev_lay = QtWidgets.QGridLayout(dev_box)
        dev_lay.setContentsMargins(6, 4, 6, 4)
        dev_lay.setVerticalSpacing(2)
        dev_lay.addWidget(QtWidgets.QLabel("Microphone"), 0, 0)
        dev_lay.addWidget(self._input_device, 0, 1, 1, 3)
        dev_lay.addWidget(QtWidgets.QLabel("Headphones"), 1, 0)
        dev_lay.addWidget(self._output_device, 1, 1, 1, 3)
        dev_lay.addWidget(self._show_all_devices, 2, 0)
        dev_lay.addWidget(self._refresh_devices_btn, 2, 1)
        dev_lay.addWidget(self._device_status, 2, 2, 1, 2)
        dev_lay.addWidget(self._connect_btn, 3, 0, 1, 2)
        dev_lay.addWidget(self._disconnect_btn, 3, 2, 1, 2)

        # -- Audio controls group --
        ctrl_box = QtWidgets.QGroupBox("Audio Controls")
        ctrl_lay = QtWidgets.QGridLayout(ctrl_box)
        ctrl_lay.setContentsMargins(6, 4, 6, 4)
        ctrl_lay.setVerticalSpacing(2)
        ctrl_lay.addWidget(self._mute, 0, 0)
        ctrl_lay.addWidget(self._sidetone, 0, 1, 1, 3)
        ctrl_lay.addWidget(self._listen_return_bus, 0, 4)
        ctrl_lay.addWidget(QtWidgets.QLabel("Mic gain"), 1, 0)
        ctrl_lay.addWidget(self._mic_gain, 1, 1, 1, 2)
        ctrl_lay.addWidget(self._mic_gain_lbl, 1, 3)
        ctrl_lay.addWidget(QtWidgets.QLabel("Headphones gain"), 2, 0)
        ctrl_lay.addWidget(self._hp_gain, 2, 1, 1, 2)
        ctrl_lay.addWidget(self._hp_gain_lbl, 2, 3)
        ctrl_lay.addWidget(QtWidgets.QLabel("Sidetone gain"), 3, 0)
        ctrl_lay.addWidget(self._sidetone_gain, 3, 1, 1, 2)
        ctrl_lay.addWidget(self._sidetone_gain_lbl, 3, 3)

        # -- PTT / Routing group --
        ptt_box = QtWidgets.QGroupBox("PTT / Routing")
        ptt_lay = QtWidgets.QGridLayout(ptt_box)
        ptt_lay.setContentsMargins(6, 4, 6, 4)
        ptt_lay.setVerticalSpacing(2)
        ptt_lay.addWidget(self._bus_rows_widget, 0, 0, 1, 1)

        # -- Meters group --
        meters_box = QtWidgets.QGroupBox("Meters")
        meters_lay = QtWidgets.QGridLayout(meters_box)
        meters_lay.setContentsMargins(6, 4, 6, 4)
        meters_lay.setVerticalSpacing(2)
        meters_lay.addWidget(QtWidgets.QLabel("Input VU"), 0, 0)
        meters_lay.addWidget(self._in_vu, 0, 1)
        meters_lay.addWidget(QtWidgets.QLabel("Output VU"), 1, 0)
        meters_lay.addWidget(self._out_vu, 1, 1)
        meters_lay.addWidget(QtWidgets.QLabel("Return bus VU"), 2, 0)
        meters_lay.addWidget(self._return_vu, 2, 1)

        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("ℹ")
        self._info_btn.setFixedSize(26, 26)
        self._info_btn.setToolTip("Info / Debug")
        self._info_btn.clicked.connect(self._show_geek_info)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self._info_btn)

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(conn_box)
        layout.addWidget(dev_box)
        layout.addWidget(ctrl_box)
        layout.addWidget(ptt_box)
        layout.addWidget(meters_box)
        layout.addLayout(bottom)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(lambda _: self._start_device_refresh())
        self._connect_btn.clicked.connect(self._connect)
        self._disconnect_btn.clicked.connect(self._disconnect)

        self._mute.stateChanged.connect(self._on_mute_changed)
        self._listen_return_bus.stateChanged.connect(self._on_listen_return_bus_changed)
        self._mic_gain.valueChanged.connect(self._on_mic_gain_changed)
        self._hp_gain.valueChanged.connect(self._on_hp_gain_changed)
        self._sidetone.stateChanged.connect(self._on_sidetone_changed)
        self._sidetone_gain.valueChanged.connect(self._on_sidetone_gain_changed)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._refresh_stats)

        self._device_thread: Optional[QtCore.QThread] = None
        self._device_worker: Optional[_DeviceWorker] = None
        self._device_timeout: Optional[QtCore.QTimer] = None
        self._last_device_error: str = ""
        self._last_device_count: int = 0
        self._kick_notified: bool = False
        self._server_lost_notified: bool = False
        self._was_control_connected: bool = False

        self._global_ptt: Optional[_GlobalPttHotkeys] = None

        QtCore.QTimer.singleShot(0, self._start_device_refresh)

    def _make_shortcut_widget(self, edit: QtWidgets.QKeySequenceEdit, clear_btn: QtWidgets.QToolButton) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(edit, 1)
        lay.addWidget(clear_btn, 0)
        self._shortcut_widgets.append(w)
        return w

    def _clear_layout(self, layout: QtWidgets.QLayout) -> None:
        while layout.count() > 0:
            item = layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()
                continue
            child = item.layout()
            if child is not None:
                self._clear_layout(child)

    def _bus_mode_value(self, bus_id: int) -> str:
        combo = self._mode_bus_widgets.get(int(bus_id))
        if combo is None:
            return "ptt"
        try:
            mode = str(combo.currentData() or "ptt")
        except Exception:
            mode = "ptt"
        if mode not in ("always_on", "ptt"):
            mode = "ptt"
        return mode

    def _apply_ptt_modes_to_client(self) -> None:
        cli = self._client
        if cli is None:
            return

        for bid in list(self._ptt_bus_keys.keys()):
            mode = self._bus_mode_value(int(bid))
            if mode == "always_on":
                active = True
            elif self._global_ptt is not None:
                active = bool(self._global_ptt._active_buses.get(int(bid), False))
            else:
                active = False
            try:
                cli.set_ptt_bus(int(bid), bool(active))
            except Exception:
                pass

    def _sync_bus_widgets(self, buses: dict[int, dict], *, apply_saved: bool = False) -> None:
        parsed: dict[int, dict] = {}
        for bid, b in buses.items():
            try:
                bus_id = int(bid)
            except Exception:
                continue
            if not isinstance(b, dict):
                b = {}
            parsed[int(bus_id)] = {
                "bus_id": int(bus_id),
                "name": str(b.get("name") or ("Regie" if int(bus_id) == 0 else f"Bus {int(bus_id)}")),
                "bus_type": str(b.get("bus_type") or ("communication" if int(bus_id) == 0 else "diffusion")),
                "feed_to_regie": bool(b.get("feed_to_regie", False)),
            }

        if 0 not in parsed:
            parsed[0] = {"bus_id": 0, "name": "Regie", "bus_type": "communication", "feed_to_regie": False}

        new_signature = tuple((int(bid), str(parsed[bid].get("name")), str(parsed[bid].get("bus_type"))) for bid in sorted(parsed.keys()))
        if new_signature == self._bus_rows_signature and not apply_saved:
            self._known_buses = dict(parsed)
            return

        old_keys: dict[int, str] = {}
        for bid, edit in self._ptt_bus_keys.items():
            try:
                old_keys[int(bid)] = str(edit.keySequence().toString())
            except Exception:
                old_keys[int(bid)] = ""

        old_mutes: dict[int, bool] = {}
        for bid, cb in self._mute_bus_widgets.items():
            try:
                old_mutes[int(bid)] = bool(cb.isChecked())
            except Exception:
                old_mutes[int(bid)] = False

        old_modes: dict[int, str] = {}
        for bid, combo in self._mode_bus_widgets.items():
            try:
                v = str(combo.currentData() or "ptt")
                old_modes[int(bid)] = v if v in ("always_on", "ptt") else "ptt"
            except Exception:
                old_modes[int(bid)] = "ptt"

        try:
            saved_keys = self._preset_get("ptt_bus_keys", {})
            if not isinstance(saved_keys, dict):
                saved_keys = {}
        except Exception:
            saved_keys = {}

        try:
            saved_mutes = self._preset_get("mute_buses", {})
            if not isinstance(saved_mutes, dict):
                saved_mutes = {}
        except Exception:
            saved_mutes = {}

        try:
            saved_modes = self._preset_get("mode_buses", {})
            if not isinstance(saved_modes, dict):
                saved_modes = {}
        except Exception:
            saved_modes = {}

        try:
            legacy_always_on = str(self._preset_get("mode", "ptt") or "ptt") == "always_on"
        except Exception:
            legacy_always_on = False

        for bid in parsed.keys():
            ibid = int(bid)

            if apply_saved:
                old_keys[ibid] = str(saved_keys.get(str(ibid), "") or "")
                old_mutes[ibid] = bool(saved_mutes.get(str(ibid), False))
            else:
                if ibid not in old_keys or not str(old_keys.get(ibid) or ""):
                    old_keys[ibid] = str(saved_keys.get(str(ibid), "") or "")
                if ibid not in old_mutes:
                    old_mutes[ibid] = bool(saved_mutes.get(str(ibid), False))

            mode_val = str(old_modes.get(ibid) or "")
            if mode_val not in ("always_on", "ptt"):
                mode_val = str(saved_modes.get(str(ibid), "") or "")
            if mode_val not in ("always_on", "ptt"):
                mode_val = "always_on" if bool(legacy_always_on) else "ptt"
            old_modes[ibid] = str(mode_val)

        self._clear_layout(self._bus_rows_layout)
        self._shortcut_widgets = []
        self._ptt_bus_keys = {}
        self._ptt_bus_clear = {}
        self._mode_bus_widgets = {}
        self._mute_bus_widgets = {}
        self._route_bus_widgets = {}

        headers = ["Bus", "Mode", "PTT key", "Routed", "Mute mic"]
        for col, title in enumerate(headers):
            lbl = QtWidgets.QLabel(str(title))
            f = lbl.font()
            f.setBold(True)
            lbl.setFont(f)
            self._bus_rows_layout.addWidget(lbl, 0, int(col))

        row = 1
        for bus_id in sorted(parsed.keys()):
            b = parsed[int(bus_id)]
            bus_name = str(b.get("name") or f"Bus {int(bus_id)}")

            lbl = QtWidgets.QLabel(str(bus_name))

            mode_combo = QtWidgets.QComboBox()
            patch_combo(mode_combo)
            mode_combo.addItem("Always-on", "always_on")
            mode_combo.addItem("PTT", "ptt")
            mode_i = mode_combo.findData(str(old_modes.get(int(bus_id), "ptt") or "ptt"))
            if mode_i < 0:
                mode_i = mode_combo.findData("ptt")
            if mode_i >= 0:
                mode_combo.setCurrentIndex(mode_i)
            mode_combo.currentIndexChanged.connect(lambda _idx, bid=int(bus_id): self._on_mode_bus_changed(bid))
            mode_combo.setEnabled(True)

            edit = _ShortcutKeySequenceEdit()
            edit.setToolTip("Click then press shortcut. Esc cancels. Del clears.")
            clear_btn = QtWidgets.QToolButton()
            clear_btn.setText("✕")
            clear_btn.setFixedSize(22, 22)
            clear_btn.setToolTip("Clear shortcut")
            clear_btn.clicked.connect(lambda _checked=False, e=edit: e.setKeySequence(QtGui.QKeySequence("")))
            mode_value = str(mode_combo.currentData() or "ptt")
            key_enabled = mode_value == "ptt"
            edit.setEnabled(bool(key_enabled))
            clear_btn.setEnabled(bool(key_enabled))

            seq = str(old_keys.get(int(bus_id), "") or "")
            try:
                edit.setKeySequence(QtGui.QKeySequence(seq))
            except Exception:
                pass
            edit.keySequenceChanged.connect(lambda _seq, bid=int(bus_id): self._on_ptt_bus_key_changed(bid))

            routed_cb = QtWidgets.QCheckBox("Routed")
            routed_cb.setEnabled(False)

            mute_cb = QtWidgets.QCheckBox("Mute mic")
            mute_cb.setChecked(bool(old_mutes.get(int(bus_id), False)))
            mute_cb.setEnabled(bool(self._connected))
            mute_cb.stateChanged.connect(lambda state, bid=int(bus_id): self._on_mute_bus_changed(bid, state))

            self._bus_rows_layout.addWidget(lbl, row, 0)
            self._bus_rows_layout.addWidget(mode_combo, row, 1)
            self._bus_rows_layout.addWidget(self._make_shortcut_widget(edit, clear_btn), row, 2)
            self._bus_rows_layout.addWidget(routed_cb, row, 3)
            self._bus_rows_layout.addWidget(mute_cb, row, 4)

            self._ptt_bus_keys[int(bus_id)] = edit
            self._ptt_bus_clear[int(bus_id)] = clear_btn
            self._mode_bus_widgets[int(bus_id)] = mode_combo
            self._route_bus_widgets[int(bus_id)] = routed_cb
            self._mute_bus_widgets[int(bus_id)] = mute_cb
            row += 1

        self._known_buses = dict(parsed)
        self._bus_rows_signature = tuple((int(bid), str(parsed[bid].get("name")), str(parsed[bid].get("bus_type"))) for bid in sorted(parsed.keys()))

        if self._connected:
            try:
                self._apply_ptt_modes_to_client()
            except Exception:
                pass

    def _start_discovery_listener(self) -> None:
        if self._discovery_listener is not None:
            return
        self._discovery_listener = DiscoveryListener(
            on_update=self._on_discovery_update,
        )
        self._discovery_listener.start()
        self._discovery_signal.start()

    def _stop_discovery_listener(self) -> None:
        self._discovery_signal.stop()
        if self._discovery_listener is not None:
            self._discovery_listener.stop()
            self._discovery_listener = None

    def _on_discovery_update(self, _servers) -> None:
        self._discovery_pending_update = True

    def _poll_discovery_updates(self) -> None:
        if not self._discovery_pending_update:
            return
        self._discovery_pending_update = False
        if self._discovery_listener is None:
            return
        servers = self._discovery_listener.get_servers()
        self._refresh_discovered_combo(servers)

    def _refresh_discovered_combo(self, servers: dict) -> None:
        current_data = self._discovered_servers.currentData()
        self._discovered_servers.blockSignals(True)
        try:
            self._discovered_servers.clear()
            self._discovered_servers.addItem("(manual)", None)
            for key in sorted(servers.keys()):
                srv = servers[key]
                label = f"{srv.server_name}  ({srv.ip}:{srv.audio_port})"
                self._discovered_servers.addItem(label, key)
            if current_data is not None:
                i = self._discovered_servers.findData(current_data)
                if i >= 0:
                    self._discovered_servers.setCurrentIndex(i)
        finally:
            self._discovered_servers.blockSignals(False)

    def _on_discovered_server_selected(self, index: int) -> None:
        key = self._discovered_servers.currentData()
        if key is None or self._discovery_listener is None:
            return
        servers = self._discovery_listener.get_servers()
        srv = servers.get(str(key))
        if srv is None:
            return
        self._server_ip.setText(srv.ip)
        self._server_port.setValue(srv.audio_port)

    def _reset_total_config(self) -> None:
        if self._connected:
            QtWidgets.QMessageBox.warning(self, "Connected", "Disconnect first to reset config")
            return

        if (
            QtWidgets.QMessageBox.question(
                self,
                "Reset config",
                "Delete client preset file and reset client configuration?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        # Fully stop persistent client (releases audio devices)
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
            self._client = None

        p = self._preset_path()
        try:
            tmp = p.with_suffix(p.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

        self._preset = {}
        client_uuid = str(uuid.uuid4())
        self._preset["client_uuid"] = client_uuid
        self._preset["ptt_bus_keys"] = {}
        self._preset["mute_buses"] = {}
        self._preset["mode_buses"] = {}
        stable_id = int(zlib.crc32(client_uuid.encode("utf-8")) & 0xFFFFFFFF)

        self._preset_save()

        self._server_ip.setText("")
        self._server_port.setValue(5000)
        self._name.setText("")

        try:
            for edit in self._ptt_bus_keys.values():
                edit.setKeySequence(QtGui.QKeySequence(""))
            for combo in self._mode_bus_widgets.values():
                i = combo.findData("ptt")
                if i >= 0:
                    combo.setCurrentIndex(i)
            for cb in self._mute_bus_widgets.values():
                cb.setChecked(False)
        except Exception:
            pass

        self._client_id.setText(str(stable_id))

        self._input_device.blockSignals(True)
        self._output_device.blockSignals(True)
        try:
            self._input_device.setCurrentIndex(-1)
            self._output_device.setCurrentIndex(-1)
        finally:
            self._input_device.blockSignals(False)
            self._output_device.blockSignals(False)

        self._device_status.setText("Config reset")
        QtCore.QTimer.singleShot(0, self._start_device_refresh)

    def _preset_path(self) -> Path:
        return Path.home() / "py-intercom" / "client_preset.json"

    def _load_preset(self) -> dict:
        p = self._preset_path()
        loaded = read_json_file(p)
        return loaded if isinstance(loaded, dict) else {}

    def _preset_save(self) -> None:
        try:
            atomic_write_json(self._preset_path(), self._preset)
        except Exception:
            return

    def _preset_get(self, key: str, default=None):
        try:
            if isinstance(self._preset, dict) and key in self._preset:
                return self._preset.get(key)
        except Exception:
            return default
        return default

    def _preset_set(self, key: str, value) -> None:
        try:
            if not isinstance(self._preset, dict):
                self._preset = {}
            self._preset[str(key)] = value
        except Exception:
            return
        self._preset_save()

    def _on_ptt_bus_key_changed(self, bus_id: int) -> None:
        if int(bus_id) not in self._ptt_bus_keys:
            return
        try:
            if str(self._ptt_bus_keys[int(bus_id)].keySequence().toString()) in {"Ctrl", "Alt", "Shift", "Meta"}:
                self._ptt_bus_keys[int(bus_id)].setKeySequence(QtGui.QKeySequence(""))
                return
        except Exception:
            return
        try:
            cur = self._preset_get("ptt_bus_keys", {})
            if not isinstance(cur, dict):
                cur = {}
            seq = self._ptt_bus_keys[int(bus_id)].keySequence()
            cur[str(int(bus_id))] = str(seq.toString())
            self._preset_set("ptt_bus_keys", cur)
        except Exception:
            return

        try:
            self._ptt_bus_keys[int(bus_id)].clearFocus()
            self.centralWidget().setFocus()
        except Exception:
            pass

        try:
            if self._global_ptt is not None:
                self._global_ptt._update()
            else:
                self._apply_ptt_modes_to_client()
        except Exception:
            pass

    def _on_mode_bus_changed(self, bus_id: int) -> None:
        if int(bus_id) not in self._mode_bus_widgets:
            return
        mode = self._bus_mode_value(int(bus_id))
        try:
            cur = self._preset_get("mode_buses", {})
            if not isinstance(cur, dict):
                cur = {}
            cur[str(int(bus_id))] = str(mode)
            self._preset_set("mode_buses", cur)
        except Exception:
            pass

        edit = self._ptt_bus_keys.get(int(bus_id))
        clear_btn = self._ptt_bus_clear.get(int(bus_id))
        if edit is not None:
            edit.setEnabled(mode == "ptt")
        if clear_btn is not None:
            clear_btn.setEnabled(mode == "ptt")

        try:
            if self._global_ptt is not None:
                self._global_ptt._update()
            else:
                self._apply_ptt_modes_to_client()
        except Exception:
            pass

    def _on_mute_bus_changed(self, bus_id: int, state: int) -> None:
        if int(bus_id) not in self._mute_bus_widgets:
            return
        muted = _is_checked(state)
        try:
            cur = self._preset_get("mute_buses", {})
            if not isinstance(cur, dict):
                cur = {}
            cur[str(int(bus_id))] = bool(muted)
            self._preset_set("mute_buses", cur)
        except Exception:
            pass

        if self._client is not None:
            try:
                self._client.set_mute_bus(int(bus_id), bool(muted))
            except Exception:
                pass

    def _on_input_device_changed(self, _idx: int) -> None:
        try:
            dev = self._input_device.currentData()
            if dev is None:
                return
            self._preset_set("input_device", int(dev))
        except Exception:
            return

    def _on_output_device_changed(self, _idx: int) -> None:
        try:
            dev = self._output_device.currentData()
            if dev is None:
                return
            self._preset_set("output_device", int(dev))
        except Exception:
            return

    def _reset_identity(self) -> None:
        if self._connected:
            QtWidgets.QMessageBox.warning(self, "Connected", "Disconnect first to reset identity")
            return

        if (
            QtWidgets.QMessageBox.question(
                self,
                "Reset identity",
                "Generate a new identity for this client?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        client_uuid = str(uuid.uuid4())
        self._preset_set("client_uuid", client_uuid)
        stable_id = int(zlib.crc32(client_uuid.encode("utf-8")) & 0xFFFFFFFF)
        self._client_id.setText(str(stable_id))

    def _show_geek_info(self) -> None:
        server_ip = self._server_ip.text().strip()
        server_port = int(self._server_port.value())
        client_uuid = str(self._preset_get("client_uuid", ""))
        client_id = self._client_id.text().strip()
        name = self._name.text().strip()

        st = self._client.get_stats_snapshot() if self._client is not None else {}

        preset_path = str(self._preset_path())
        preset_exists = False
        try:
            preset_exists = Path(preset_path).exists()
        except Exception:
            preset_exists = False

        info = "\n".join(
            [
                f"Server: {server_ip}:{server_port}",
                f"Name: {name}",
                "Mode: per-bus",
                f"Client UUID: {client_uuid or '-'}",
                f"Client ID: {client_id or '-'}",
                f"Control connected: {st.get('control_connected', False)}",
                f"Control age (s): {st.get('control_age_s') if st.get('control_age_s') is not None else '-'}",
                f"Control TX age (s): {st.get('control_tx_age_s') if st.get('control_tx_age_s') is not None else '-'}",
                f"Stopped: {st.get('stopped', False)}",
                f"Kicked: {st.get('kicked', False)}",
                "",
                f"Client preset: {preset_path}",
                f"Preset exists: {preset_exists}",
                "",
                f"In: ch={st.get('in_channels', '-')} sr={st.get('in_samplerate', '-')}",
                f"Out: ch={st.get('out_channels', '-')} sr={st.get('out_samplerate', '-')} ",
                f"Buf capture samples: {st.get('capture_samples', '-')} ",
                f"Buf playback samples: {st.get('playback_samples', '-')} ",
                f"Buf sidetone samples: {st.get('sidetone_samples', '-')} ",
                "",
                f"TX pkts: {st.get('tx_packets', '-')} ",
                f"TX udp sent: {st.get('tx_udp_sent', '-')} ",
                f"TX sock err: {st.get('tx_socket_errors', '-')} ",
                f"Opus enc err: {st.get('opus_encode_errors', '-')} ",
                f"RX pkts: {st.get('rx_packets', '-')} ",
                f"RX sock err: {st.get('rx_socket_errors', '-')} ",
                f"Opus dec err: {st.get('opus_decode_errors', '-')} ",
                f"Opus OK: {st.get('opus_ok', '-')} ",
                f"Opuslib ver: {st.get('opuslib_version', '-')} ",
            ]
        )

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Info")
        box.setText(info)
        reset_cfg_btn = box.addButton("Reset config", QtWidgets.QMessageBox.ActionRole)
        reset_btn = box.addButton("Reset identity", QtWidgets.QMessageBox.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is reset_cfg_btn:
            self._reset_total_config()
        elif box.clickedButton() is reset_btn:
            self._reset_identity()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Full stop: network + audio streams
        try:
            self._stop_discovery_listener()
        except Exception:
            pass
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            if self._global_ptt is not None:
                self._global_ptt.stop()
        except Exception:
            pass
        self._global_ptt = None
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                pass
            self._client = None
        self._connected = False
        super().closeEvent(event)

    def _start_device_refresh(self) -> None:
        if self._device_thread is not None:
            self._device_status.setText("Refreshing devices already in progress...")
            return

        self._refresh_devices_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WaitCursor))

        hostapi_filter = None if self._show_all_devices.isChecked() else "WASAPI"

        self._device_status.setText(f"Refreshing devices ({hostapi_filter or 'ALL'})...")
        QtWidgets.QApplication.processEvents()
        self._last_device_error = ""
        self._last_device_count = 0

        self._device_thread = QtCore.QThread(self)
        self._device_worker = _DeviceWorker(hostapi_filter=hostapi_filter)
        self._device_worker.moveToThread(self._device_thread)
        self._device_thread.started.connect(self._device_worker.run)
        self._device_worker.finished.connect(self._on_devices_refreshed)
        self._device_worker.finished.connect(self._device_thread.quit)
        self._device_thread.finished.connect(self._cleanup_device_refresh)
        self._device_thread.finished.connect(self._device_thread.deleteLater)

        self._device_timeout = QtCore.QTimer(self)
        self._device_timeout.setSingleShot(True)
        self._device_timeout.timeout.connect(self._device_refresh_timed_out)
        self._device_timeout.start(5000)

        self._device_thread.start()

    def _device_refresh_timed_out(self) -> None:
        if self._device_thread is None:
            return

        self._last_device_error = "Device enumeration timed out (5s)."
        self._device_status.setText(self._last_device_error)
        try:
            self._device_thread.terminate()
        except Exception:
            pass

        self._refresh_devices_btn.setEnabled(True)
        try:
            QtWidgets.QApplication.restoreOverrideCursor()
        except Exception:
            pass
        self._device_thread = None
        self._device_worker = None

    def _cleanup_device_refresh(self) -> None:
        if self._device_thread is None and self._device_worker is None:
            return

        if self._device_timeout is not None:
            try:
                self._device_timeout.stop()
            except Exception:
                pass
        self._device_timeout = None

        self._device_thread = None
        self._device_worker = None
        self._refresh_devices_btn.setEnabled(True)
        QtWidgets.QApplication.restoreOverrideCursor()

        if self._last_device_error:
            return

        self._device_status.setText(f"Devices loaded: {self._last_device_count}")

    def _on_devices_refreshed(self, devs, error: str) -> None:
        if error:
            self._last_device_error = error
            self._device_status.setText(f"Device refresh failed: {error}")
            QtWidgets.QMessageBox.warning(self, "Device refresh failed", error)
            return

        self._last_device_count = int(len(devs))
        self._device_status.setText(f"Devices loaded: {self._last_device_count}")

        cur_in = self._input_device.currentData()
        cur_out = self._output_device.currentData()
        try:
            saved_in = int(self._preset_get("input_device", -1))
        except Exception:
            saved_in = -1
        try:
            saved_out = int(self._preset_get("output_device", -1))
        except Exception:
            saved_out = -1

        self._input_device.blockSignals(True)
        self._output_device.blockSignals(True)
        try:
            self._input_device.clear()
            self._output_device.clear()

            for d in devs:
                if d.max_input_channels > 0:
                    self._input_device.addItem(f"{d.index}-{d.name}", d.index)
                if d.max_output_channels > 0:
                    self._output_device.addItem(f"{d.index}-{d.name}", d.index)

            if cur_in is not None:
                i = self._input_device.findData(cur_in)
                if i >= 0:
                    self._input_device.setCurrentIndex(i)
            elif int(saved_in) >= 0:
                i = self._input_device.findData(int(saved_in))
                if i >= 0:
                    self._input_device.setCurrentIndex(i)
            if cur_out is not None:
                i = self._output_device.findData(cur_out)
                if i >= 0:
                    self._output_device.setCurrentIndex(i)
            elif int(saved_out) >= 0:
                i = self._output_device.findData(int(saved_out))
                if i >= 0:
                    self._output_device.setCurrentIndex(i)
        finally:
            self._input_device.blockSignals(False)
            self._output_device.blockSignals(False)

    def _connect(self) -> None:
        if self._connected:
            return

        server_ip = self._server_ip.text().strip()
        if not server_ip:
            QtWidgets.QMessageBox.warning(self, "Missing server", "Please enter server IP")
            return

        server_port = int(self._server_port.value())
        ctrl_port = int(server_port) + 1
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(1.0)
                sock.connect((server_ip, int(ctrl_port)))
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception:
            QtWidgets.QMessageBox.warning(self, "Serveur indisponible", "Le serveur est down (control TCP injoignable)")
            return

        client_id = int(self._client_id.text().strip())

        input_device = self._input_device.currentData()
        output_device = self._output_device.currentData()
        if input_device is None or output_device is None:
            QtWidgets.QMessageBox.warning(self, "Missing device", "Select microphone and headphones")
            return

        self._preset_set("input_device", int(input_device))
        self._preset_set("output_device", int(output_device))

        name = self._name.text().strip()
        mode = "ptt"

        self._preset_set("server_ip", server_ip)
        self._preset_set("server_port", int(self._server_port.value()))
        self._preset_set("name", name)

        client_uuid = str(self._preset_get("client_uuid", ""))

        cfg = ClientConfig(
            server_ip=server_ip,
            server_port=int(server_port),
            control_port=int(server_port) + 1,
            name=name,
            mode=mode,
            client_uuid=client_uuid,
            input_device=int(input_device),
            output_device=int(output_device),
            input_gain_db=float(self._mic_gain.value()),
            output_gain_db=float(self._hp_gain.value()),
            muted=self._mute.isChecked(),
            sidetone_enabled=self._sidetone.isChecked(),
            sidetone_gain_db=float(self._sidetone_gain.value()),
            ptt_bus_keys=dict(self._preset_get("ptt_bus_keys", {}) or {}),
            mute_buses=dict(self._preset_get("mute_buses", {}) or {}),
            listen_return_bus=bool(self._listen_return_bus.isChecked()),
        )

        # Reuse existing client if audio devices haven't changed (avoids PortAudio reopen issues on Windows)
        can_reuse = (
            self._client is not None
            and self._client.config.input_device == cfg.input_device
            and self._client.config.output_device == cfg.output_device
        )

        try:
            if can_reuse:
                # Update config fields that may have changed
                self._client.config.server_ip = cfg.server_ip
                self._client.config.server_port = cfg.server_port
                self._client.config.control_port = int(cfg.server_port) + 1
                self._client.config.name = cfg.name
                self._client.config.mode = cfg.mode
                self._client.config.client_uuid = cfg.client_uuid
                self._client.config.muted = cfg.muted
                self._client.config.ptt_bus_keys = cfg.ptt_bus_keys
                self._client.config.mute_buses = cfg.mute_buses
                self._client.config.listen_return_bus = bool(cfg.listen_return_bus)
                self._client.set_input_gain_db(cfg.input_gain_db)
                self._client.set_output_gain_db(cfg.output_gain_db)
                self._client.set_muted(cfg.muted)
                self._client.set_sidetone_enabled(cfg.sidetone_enabled)
                self._client.set_sidetone_gain_db(cfg.sidetone_gain_db)
                self._client.set_listen_return_bus(bool(cfg.listen_return_bus))
                self._client.reconnect_network()
            else:
                # Full stop of old client if devices changed
                if self._client is not None:
                    try:
                        self._client.stop()
                    except Exception:
                        pass
                self._client = IntercomClient(client_id=client_id, config=cfg)
                self._client.start()
        except Exception as e:
            self._client = None
            self._connected = False
            QtWidgets.QMessageBox.critical(self, "Connect failed", str(e))
            return

        self._connected = True
        self._status_label.setText(f"Connected to {server_ip}:{server_port}")

        try:
            if self._global_ptt is not None:
                self._global_ptt.stop()
            self._global_ptt = _GlobalPttHotkeys(self)
            self._global_ptt.start()
        except Exception as e:
            self._global_ptt = None
            QtWidgets.QMessageBox.warning(self, "PTT", f"Global hotkeys failed to start: {e}")

        self._apply_ptt_modes_to_client()

        try:
            if self._client is not None:
                self._client.set_listen_return_bus(bool(self._listen_return_bus.isChecked()))
        except Exception:
            pass

        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._mute.setEnabled(True)
        self._listen_return_bus.setEnabled(True)
        self._mic_gain.setEnabled(True)
        self._hp_gain.setEnabled(True)
        self._sidetone.setEnabled(True)
        self._sidetone_gain.setEnabled(True)

        for cb in self._mute_bus_widgets.values():
            cb.setEnabled(True)

        self._timer.start()

    def _disconnect(self) -> None:
        if not self._connected:
            return
        self._kick_notified = False
        self._server_lost_notified = False
        self._was_control_connected = False
        try:
            self._timer.stop()
            try:
                if self._client is not None:
                    for bid in self._ptt_bus_keys.keys():
                        self._client.set_ptt_bus(int(bid), False)
            except Exception:
                pass

            try:
                if self._global_ptt is not None:
                    self._global_ptt.stop()
            except Exception:
                pass
            self._global_ptt = None

            # Only disconnect network, keep audio streams alive for fast reconnect
            if self._client is not None:
                self._client.disconnect_network()
        finally:
            self._connected = False
            self._status_label.setText("Disconnected")
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setEnabled(False)
            self._mute.setEnabled(False)
            self._listen_return_bus.setEnabled(False)
            self._mic_gain.setEnabled(False)
            self._hp_gain.setEnabled(False)
            self._sidetone.setEnabled(False)
            self._sidetone_gain.setEnabled(False)
            self._in_vu.set_level(-60.0)
            self._out_vu.set_level(-60.0)
            self._return_vu.set_level(-60.0)

            try:
                for bid, combo in self._mode_bus_widgets.items():
                    combo.setEnabled(True)
                    edit = self._ptt_bus_keys.get(int(bid))
                    clear_btn = self._ptt_bus_clear.get(int(bid))
                    enabled = self._bus_mode_value(int(bid)) == "ptt"
                    if edit is not None:
                        edit.setEnabled(bool(enabled))
                    if clear_btn is not None:
                        clear_btn.setEnabled(bool(enabled))
            except Exception:
                pass

            for cb in self._mute_bus_widgets.values():
                cb.setEnabled(False)

            for cb in self._route_bus_widgets.values():
                try:
                    cb.setChecked(False)
                except Exception:
                    pass

    def _on_mute_changed(self, state: int) -> None:
        try:
            self._preset_set("muted", bool(_is_checked(state)))
        except Exception:
            pass
        if self._client is None:
            return
        self._client.set_muted(_is_checked(state))

    def _on_listen_return_bus_changed(self, state: int) -> None:
        enabled = _is_checked(state)
        try:
            self._preset_set("listen_return_bus", bool(enabled))
        except Exception:
            pass
        if self._client is None:
            return
        try:
            self._client.set_listen_return_bus(bool(enabled))
        except Exception:
            pass

    def _on_mic_gain_changed(self, value: int) -> None:
        self._mic_gain_lbl.setText(f"{value} dB")
        try:
            self._preset_set("input_gain_db", float(value))
        except Exception:
            pass
        if self._client is None:
            return
        self._client.set_input_gain_db(float(value))

    def _on_hp_gain_changed(self, value: int) -> None:
        self._hp_gain_lbl.setText(f"{value} dB")
        try:
            self._preset_set("output_gain_db", float(value))
        except Exception:
            pass
        if self._client is None:
            return
        self._client.set_output_gain_db(float(value))

    def _on_sidetone_changed(self, state: int) -> None:
        try:
            self._preset_set("sidetone_enabled", bool(_is_checked(state)))
        except Exception:
            pass
        if self._client is None:
            return
        self._client.set_sidetone_enabled(_is_checked(state))

    def _on_sidetone_gain_changed(self, value: int) -> None:
        self._sidetone_gain_lbl.setText(f"{value} dB")
        try:
            self._preset_set("sidetone_gain_db", float(value))
        except Exception:
            pass
        if self._client is None:
            return
        self._client.set_sidetone_gain_db(float(value))

    def _refresh_stats(self) -> None:
        if self._client is None:
            return
        st = self._client.get_stats_snapshot()

        ctrl_ok = bool(st.get("control_connected", False))
        if ctrl_ok:
            self._was_control_connected = True
        elif self._was_control_connected and not self._server_lost_notified:
            self._server_lost_notified = True
            QtWidgets.QMessageBox.information(self, "Déconnecté", "Serveur déconnecté")
            self._disconnect()
            return

        if bool(st.get("kicked", False)) and not bool(self._kick_notified):
            self._kick_notified = True
            msg = str(st.get("kick_message") or "Tu as été kick")
            QtWidgets.QMessageBox.information(self, "Déconnecté", msg)
            self._disconnect()
            return

        try:
            muted = bool(st.get("muted", False))
            if bool(self._mute.isChecked()) != bool(muted):
                self._mute.blockSignals(True)
                try:
                    self._mute.setChecked(bool(muted))
                finally:
                    self._mute.blockSignals(False)
        except Exception:
            pass

        self._in_vu.set_level(float(st.get("in_vu_dbfs", -60.0)))
        self._out_vu.set_level(float(st.get("out_vu_dbfs", -60.0)))
        self._return_vu.set_level(float(st.get("return_vu_dbfs", -60.0)))

        buses_raw = st.get("buses")
        parsed_buses: dict[int, dict] = {}
        if isinstance(buses_raw, dict):
            for k, b in buses_raw.items():
                if not isinstance(b, dict):
                    continue
                try:
                    bid = int(k)
                except Exception:
                    try:
                        bid = int(b.get("bus_id"))
                    except Exception:
                        continue
                parsed_buses[int(bid)] = {
                    "bus_id": int(bid),
                    "name": str(b.get("name") or ("Regie" if int(bid) == 0 else f"Bus {int(bid)}")),
                    "bus_type": str(b.get("bus_type") or ("communication" if int(bid) == 0 else "diffusion")),
                    "feed_to_regie": bool(b.get("feed_to_regie", False)),
                }
        elif isinstance(buses_raw, list):
            for b in buses_raw:
                if not isinstance(b, dict):
                    continue
                try:
                    bid = int(b.get("bus_id"))
                except Exception:
                    continue
                parsed_buses[int(bid)] = {
                    "bus_id": int(bid),
                    "name": str(b.get("name") or ("Regie" if int(bid) == 0 else f"Bus {int(bid)}")),
                    "bus_type": str(b.get("bus_type") or ("communication" if int(bid) == 0 else "diffusion")),
                    "feed_to_regie": bool(b.get("feed_to_regie", False)),
                }

        if parsed_buses:
            self._sync_bus_widgets(parsed_buses)

        routes = st.get("routes")
        if isinstance(routes, dict):
            for bid, cb in self._route_bus_widgets.items():
                try:
                    cb.setChecked(bool(routes.get(str(int(bid)), False)))
                except Exception:
                    pass


def run_gui(
    server_ip: str = "",
    server_port: int = 5000,
    input_device: int = -1,
    output_device: int = -1,
) -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    win = ClientWindow()

    if server_ip:
        win._server_ip.setText(server_ip)
    win._server_port.setValue(int(server_port))

    if int(input_device) >= 0:
        i = win._input_device.findData(int(input_device))
        if i >= 0:
            win._input_device.setCurrentIndex(i)
    if int(output_device) >= 0:
        i = win._output_device.findData(int(output_device))
        if i >= 0:
            win._output_device.setCurrentIndex(i)

    win.show()
    return int(app.exec())
