from __future__ import annotations

import sys
import socket
import uuid
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets
from pynput import keyboard

from ..common.constants import AUDIO_UDP_PORT
from ..common.devices import list_devices, resolve_device
from ..common.gui_utils import DeviceWorker, is_checked
from ..common.identity import client_id_from_uuid
from ..common.discovery import DiscoveryListener
from ..common.jsonio import atomic_write_json, read_json_file
from ..common.theme import VuMeter, apply_theme, patch_combo
from .client import ClientConfig, IntercomClient


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
                vk = int(k.vk)
                # Map A-Z VK codes (65-90) to char: so Shift+letter matches on all layouts
                if 65 <= vk <= 90:
                    return f"char:{chr(vk).lower()}"
                # Map 0-9 VK codes (48-57)
                if 48 <= vk <= 57:
                    return f"char:{chr(vk)}"
                return f"vk:{vk}"
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
        self.release_all()

    def release_all(self) -> None:
        """Force-release all pressed keys and deactivate all PTT buses."""
        self._pressed.clear()
        cli = self._window._client
        for bid, was_active in list(self._active_buses.items()):
            if was_active:
                self._active_buses[int(bid)] = False
                if cli is not None:
                    try:
                        cli.set_ptt_bus(int(bid), False)
                    except Exception:
                        pass
        self._active_buses = {}
        # Reset toggle states and deactivate toggle buses (except always_on)
        changed = False
        for bid, state in list(self._window._ptt_bus_toggle_state.items()):
            if state:
                self._window._ptt_bus_toggle_state[int(bid)] = False
                changed = True
                mode = str(self._window._ptt_bus_modes.get(int(bid), "ptt"))
                if mode == "toggle" and cli is not None:
                    try:
                        cli.set_ptt_bus(int(bid), False)
                    except Exception:
                        pass
        if changed:
            try:
                self._window._refresh_state_labels()
            except Exception:
                pass

    def _combo_active(self, groups: list[set[str]]) -> bool:
        if not groups:
            return False
        for g in groups:
            if not any(k in self._pressed for k in g):
                return False
        return True

    def _update(self, *, _press_key: Optional[str] = None) -> None:
        cli = self._window._client
        if cli is None:
            return

        try:
            bus_items = list(self._window._ptt_bus_keys.items())
        except Exception:
            bus_items = []

        for bid, edit in bus_items:
            mode = str(self._window._ptt_bus_modes.get(int(bid), "ptt"))

            if mode == "always_on":
                continue

            groups = _parse_qt_shortcut(edit.keySequence().toString())

            if mode == "toggle":
                if _press_key is not None and groups and self._combo_active(groups):
                    cur = bool(self._window._ptt_bus_toggle_state.get(int(bid), False))
                    new_state = not cur
                    self._window._ptt_bus_toggle_state[int(bid)] = new_state
                    try:
                        cli.set_ptt_bus(int(bid), new_state)
                    except Exception:
                        pass
                    try:
                        self._window._refresh_state_labels()
                    except Exception:
                        pass
            else:
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
            is_new = k not in self._pressed
            self._pressed.add(k)
            self._update(_press_key=k if is_new else None)

    def _on_release(self, key) -> None:
        k = _norm_key(key)
        if k:
            self._pressed.discard(k)
            # Also discard the uppercase variant in case layout produced a different char
            if k.startswith("char:") and len(k) == 6:
                self._pressed.discard("char:" + k[5].upper())
            self._update()


class ClientWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Py-Intercom Client")
        self.setMinimumSize(480, 560)
        self._set_app_icon()

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

        client_uuid = str(self._preset_get("client_uuid", ""))
        if not client_uuid:
            client_uuid = str(uuid.uuid4())
            self._preset_set("client_uuid", client_uuid)
        stable_id = int(client_id_from_uuid(client_uuid))
        self._client_id_label = QtWidgets.QLabel("Client ID")
        self._client_id = QtWidgets.QLineEdit(str(stable_id))
        self._client_id.setReadOnly(True)
        self._client_id_label.setVisible(False)
        self._client_id.setVisible(False)

        self._name = QtWidgets.QLineEdit(str(self._preset_get("name", "")))
        self._ptt_bus_keys: dict[int, QtWidgets.QKeySequenceEdit] = {}
        self._ptt_bus_clear: dict[int, QtWidgets.QToolButton] = {}
        self._ptt_bus_mode_widgets: dict[int, QtWidgets.QComboBox] = {}
        self._ptt_bus_modes: dict[int, str] = {}
        self._ptt_bus_toggle_state: dict[int, bool] = {}
        self._ptt_bus_state_labels: dict[int, QtWidgets.QLabel] = {}
        self._ptt_bus_vu: dict[int, VuMeter] = {}
        self._shortcut_widgets: list[QtWidgets.QWidget] = []
        self._known_buses: dict[int, dict] = {
            0: {"bus_id": 0, "name": "Regie", "feed_to_regie": False},
            1: {"bus_id": 1, "name": "Plateau", "feed_to_regie": True},
            2: {"bus_id": 2, "name": "VMix", "feed_to_regie": True},
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

        self._connect_btn = QtWidgets.QPushButton("▶  Connect")
        self._connect_btn.setProperty("class", "success")
        self._connect_btn.setFixedWidth(130)
        self._disconnect_btn = QtWidgets.QPushButton("■  Disconnect")
        self._disconnect_btn.setProperty("class", "danger")
        self._disconnect_btn.setFixedWidth(130)
        self._disconnect_btn.setEnabled(False)

        self._start_minimized = QtWidgets.QCheckBox("Start minimized")
        try:
            self._start_minimized.setChecked(bool(self._preset_get("start_minimized", False)))
        except Exception:
            pass
        self._start_minimized.stateChanged.connect(self._on_start_minimized_changed)

        self._autoconnect_cb = QtWidgets.QCheckBox("Auto-connect")
        self._autoconnect_cb.setToolTip("Connect automatically on startup")
        try:
            self._autoconnect_cb.setChecked(bool(self._preset_get("autoconnect", False)))
        except Exception:
            pass
        self._autoconnect_cb.stateChanged.connect(self._on_autoconnect_changed)

        self._listen_regie = QtWidgets.QCheckBox("Listen Regie")
        self._listen_regie.setEnabled(False)
        try:
            self._listen_regie.setChecked(bool(self._preset_get("listen_regie", True)))
        except Exception:
            pass

        self._listen_return_bus = QtWidgets.QCheckBox("Listen return bus")
        self._listen_return_bus.setEnabled(False)
        try:
            self._listen_return_bus.setChecked(bool(self._preset_get("listen_return_bus", False)))
        except Exception:
            pass

        self._mic_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._mic_gain.setMinimum(-60)
        self._mic_gain.setMaximum(12)
        self._mic_gain.setValue(0)
        self._mic_gain.setEnabled(False)
        self._mic_gain_lbl = QtWidgets.QLabel("0 dB")
        self._mic_gain_lbl.setFixedWidth(50)
        self._mic_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._hp_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._hp_gain.setMinimum(-60)
        self._hp_gain.setMaximum(12)
        self._hp_gain.setValue(0)
        self._hp_gain.setEnabled(False)
        self._hp_gain_lbl = QtWidgets.QLabel("0 dB")
        self._hp_gain_lbl.setFixedWidth(50)
        self._hp_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._return_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._return_gain.setMinimum(-60)
        self._return_gain.setMaximum(12)
        self._return_gain.setValue(0)
        self._return_gain.setEnabled(False)
        self._return_gain_lbl = QtWidgets.QLabel("0 dB")
        self._return_gain_lbl.setFixedWidth(50)
        self._return_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        try:
            mic_gain = int(self._preset_get("input_gain_db", 0))
        except Exception:
            mic_gain = 0
        try:
            hp_gain = int(self._preset_get("output_gain_db", 0))
        except Exception:
            hp_gain = 0
        try:
            return_gain = int(self._preset_get("return_gain_db", 0))
        except Exception:
            return_gain = 0
        self._mic_gain.setValue(int(mic_gain))
        self._mic_gain_lbl.setText(f"{int(mic_gain)} dB")
        self._hp_gain.setValue(int(hp_gain))
        self._hp_gain_lbl.setText(f"{int(hp_gain)} dB")
        self._return_gain.setValue(int(return_gain))
        self._return_gain_lbl.setText(f"{int(return_gain)} dB")

        self._in_vu = VuMeter()
        self._out_vu = VuMeter()
        self._return_vu = VuMeter()

        # -- Connection group --
        conn_box = QtWidgets.QGroupBox("Connection")
        conn_box_lay = QtWidgets.QVBoxLayout(conn_box)
        conn_box_lay.setContentsMargins(10, 8, 10, 8)
        conn_box_lay.setSpacing(0)

        # Inner: left fields | mid buttons | right options
        conn_inner = QtWidgets.QHBoxLayout()
        conn_inner.setSpacing(16)

        # Left: form fields
        conn_left = QtWidgets.QGridLayout()
        conn_left.setVerticalSpacing(6)
        conn_left.setHorizontalSpacing(8)
        conn_left.addWidget(QtWidgets.QLabel("Discovered"), 0, 0)
        conn_left.addWidget(self._discovered_servers, 0, 1)
        conn_left.addWidget(QtWidgets.QLabel("Server IP"), 1, 0)
        conn_left.addWidget(self._server_ip, 1, 1)
        conn_left.addWidget(self._client_id_label, 2, 0)
        conn_left.addWidget(self._client_id, 2, 1)
        conn_left.addWidget(QtWidgets.QLabel("Name"), 3, 0)
        conn_left.addWidget(self._name, 3, 1)
        conn_left.setColumnStretch(1, 1)

        # Middle: Connect / Disconnect stacked
        conn_mid = QtWidgets.QVBoxLayout()
        conn_mid.setSpacing(6)
        conn_mid.addWidget(self._connect_btn)
        conn_mid.addWidget(self._disconnect_btn)
        conn_mid.addStretch(1)

        # Right: options stacked
        conn_right = QtWidgets.QVBoxLayout()
        conn_right.setSpacing(4)
        conn_right.addWidget(self._autoconnect_cb)
        conn_right.addWidget(self._start_minimized)
        conn_right.addStretch(1)

        conn_inner.addLayout(conn_left, 1)
        conn_inner.addLayout(conn_mid, 0)
        conn_inner.addLayout(conn_right, 0)
        conn_box_lay.addLayout(conn_inner)

        # -- Audio devices group --
        dev_box = QtWidgets.QGroupBox("Audio Devices")
        dev_lay = QtWidgets.QGridLayout(dev_box)
        dev_lay.setContentsMargins(10, 8, 10, 8)
        dev_lay.setVerticalSpacing(6)
        dev_lay.setHorizontalSpacing(8)
        dev_lay.addWidget(QtWidgets.QLabel("Microphone"), 0, 0)
        dev_lay.addWidget(self._input_device, 0, 1, 1, 3)
        dev_lay.addWidget(QtWidgets.QLabel("Headphones"), 1, 0)
        dev_lay.addWidget(self._output_device, 1, 1, 1, 3)
        dev_lay.addWidget(self._show_all_devices, 2, 0)
        dev_lay.addWidget(self._refresh_devices_btn, 2, 1)
        dev_lay.addWidget(self._device_status, 2, 2, 1, 2)

        # -- Audio controls group --
        ctrl_box = QtWidgets.QGroupBox("Audio Controls")
        ctrl_lay = QtWidgets.QGridLayout(ctrl_box)
        ctrl_lay.setContentsMargins(10, 8, 10, 8)
        ctrl_lay.setVerticalSpacing(6)
        ctrl_lay.setHorizontalSpacing(8)
        ctrl_lay.addWidget(self._listen_regie, 0, 0)
        ctrl_lay.addWidget(self._listen_return_bus, 0, 1)
        ctrl_lay.addWidget(QtWidgets.QLabel("Mic gain"), 1, 0)
        ctrl_lay.addWidget(self._mic_gain, 1, 1, 1, 2)
        ctrl_lay.addWidget(self._mic_gain_lbl, 1, 3)
        ctrl_lay.addWidget(QtWidgets.QLabel("Headphones gain"), 2, 0)
        ctrl_lay.addWidget(self._hp_gain, 2, 1, 1, 2)
        ctrl_lay.addWidget(self._hp_gain_lbl, 2, 3)
        ctrl_lay.addWidget(QtWidgets.QLabel("Return gain"), 3, 0)
        ctrl_lay.addWidget(self._return_gain, 3, 1, 1, 2)
        ctrl_lay.addWidget(self._return_gain_lbl, 3, 3)

        # -- PTT / Routing group --
        ptt_box = QtWidgets.QGroupBox("PTT / Routing")
        ptt_lay = QtWidgets.QGridLayout(ptt_box)
        ptt_lay.setContentsMargins(10, 8, 10, 8)
        ptt_lay.setVerticalSpacing(6)
        ptt_lay.addWidget(self._bus_rows_widget, 0, 0, 1, 1)

        # -- Meters group --
        meters_box = QtWidgets.QGroupBox("Meters")
        meters_lay = QtWidgets.QHBoxLayout(meters_box)
        meters_lay.setContentsMargins(10, 8, 10, 8)
        meters_lay.setSpacing(12)
        lbl_in = QtWidgets.QLabel("Input")
        lbl_in.setFixedWidth(44)
        meters_lay.addWidget(lbl_in)
        meters_lay.addWidget(self._in_vu, 1)
        lbl_out = QtWidgets.QLabel("Output")
        lbl_out.setFixedWidth(44)
        meters_lay.addWidget(lbl_out)
        meters_lay.addWidget(self._out_vu, 1)
        lbl_ret = QtWidgets.QLabel("Return")
        lbl_ret.setFixedWidth(44)
        meters_lay.addWidget(lbl_ret)
        meters_lay.addWidget(self._return_vu, 1)

        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("ℹ")
        self._info_btn.setFixedSize(26, 26)
        self._info_btn.setToolTip("Info / Debug")
        self._info_btn.clicked.connect(self._show_geek_info)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self._info_btn)

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
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

        self._listen_regie.stateChanged.connect(self._on_listen_regie_changed)
        self._listen_return_bus.stateChanged.connect(self._on_listen_return_bus_changed)
        self._mic_gain.valueChanged.connect(self._on_mic_gain_changed)
        self._hp_gain.valueChanged.connect(self._on_hp_gain_changed)
        self._return_gain.valueChanged.connect(self._on_return_gain_changed)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._refresh_stats)

        self._device_thread: Optional[QtCore.QThread] = None
        self._device_worker: Optional[DeviceWorker] = None
        self._device_timeout: Optional[QtCore.QTimer] = None
        self._last_device_error: str = ""
        self._last_device_count: int = 0
        self._device_cache: dict[int, object] = {}
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

    def _apply_ptt_modes_to_client(self) -> None:
        cli = self._client
        if cli is None:
            return

        active_buses = self._global_ptt._active_buses if self._global_ptt is not None else {}
        for bid in list(self._ptt_bus_keys.keys()):
            mode = str(self._ptt_bus_modes.get(int(bid), "ptt"))
            if mode == "always_on":
                active = True
            elif mode == "toggle":
                active = bool(self._ptt_bus_toggle_state.get(int(bid), False))
            else:
                active = bool(active_buses.get(int(bid), False))
            try:
                cli.set_ptt_bus(int(bid), bool(active))
            except Exception:
                pass

    def _update_toggle_state_label(self, lbl: QtWidgets.QLabel, bus_id: int, mode: str) -> None:
        if mode == "always_on":
            lbl.setText("ON")
            lbl.setStyleSheet("color: #4caf50; font-weight: bold;")
        elif mode == "toggle":
            on = bool(self._ptt_bus_toggle_state.get(int(bus_id), False))
            lbl.setText("ON" if on else "OFF")
            lbl.setStyleSheet("color: #4caf50; font-weight: bold;" if on else "color: #f44336; font-weight: bold;")
        elif mode == "ptt":
            gp = getattr(self, "_global_ptt", None)
            active_buses = gp._active_buses if gp is not None else {}
            on = bool(active_buses.get(int(bus_id), False))
            if self._connected:
                lbl.setText("ON" if on else "OFF")
                lbl.setStyleSheet("color: #4caf50; font-weight: bold;" if on else "color: #f44336; font-weight: bold;")
            else:
                lbl.setText("")
                lbl.setStyleSheet("")
        else:
            lbl.setText("")
            lbl.setStyleSheet("")

    def _refresh_state_labels(self) -> None:
        for bid, lbl in self._ptt_bus_state_labels.items():
            mode = str(self._ptt_bus_modes.get(int(bid), "ptt"))
            self._update_toggle_state_label(lbl, int(bid), mode)

    def _on_ptt_bus_mode_changed(self, bus_id: int) -> None:
        cb = self._ptt_bus_mode_widgets.get(int(bus_id))
        if cb is None:
            return
        mode = str(cb.currentData() or "ptt")
        self._ptt_bus_modes[int(bus_id)] = mode
        if mode != "toggle":
            self._ptt_bus_toggle_state.pop(int(bus_id), None)
        if mode == "always_on" and self._global_ptt is not None:
            self._global_ptt._active_buses.pop(int(bus_id), None)
        lbl = self._ptt_bus_state_labels.get(int(bus_id))
        if lbl is not None:
            self._update_toggle_state_label(lbl, int(bus_id), mode)
        saved = {str(k): v for k, v in self._ptt_bus_modes.items()}
        self._preset_set("ptt_bus_modes", saved)
        self._preset_save()
        if self._connected:
            self._apply_ptt_modes_to_client()

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
                "feed_to_regie": bool(b.get("feed_to_regie", False)),
            }

        if 0 not in parsed:
            parsed[0] = {"bus_id": 0, "name": "Regie", "feed_to_regie": False}

        new_signature = tuple((int(bid), str(parsed[bid].get("name"))) for bid in sorted(parsed.keys()))
        if new_signature == self._bus_rows_signature and not apply_saved:
            self._known_buses = dict(parsed)
            return

        old_keys: dict[int, str] = {}
        for bid, edit in self._ptt_bus_keys.items():
            try:
                old_keys[int(bid)] = str(edit.keySequence().toString())
            except Exception:
                old_keys[int(bid)] = ""

        old_modes: dict[int, str] = dict(self._ptt_bus_modes)

        if apply_saved:
            try:
                saved_keys = self._preset_get("ptt_bus_keys", {})
                if isinstance(saved_keys, dict):
                    for k, v in saved_keys.items():
                        try:
                            old_keys[int(k)] = str(v or "")
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                saved_modes = self._preset_get("ptt_bus_modes", {})
                if isinstance(saved_modes, dict):
                    for k, v in saved_modes.items():
                        try:
                            old_modes[int(k)] = str(v or "ptt")
                        except Exception:
                            pass
            except Exception:
                pass

        self._clear_layout(self._bus_rows_layout)
        self._shortcut_widgets = []
        self._ptt_bus_keys = {}
        self._ptt_bus_clear = {}
        self._ptt_bus_mode_widgets = {}
        self._ptt_bus_modes = {}
        self._ptt_bus_state_labels = {}
        self._ptt_bus_vu = {}

        headers = ["Bus", "Mode", "État", "VU", "PTT key"]
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

            mode_cb = QtWidgets.QComboBox()
            patch_combo(mode_cb)
            mode_cb.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            mode_cb.addItem("PTT", "ptt")
            mode_cb.addItem("Always On", "always_on")
            mode_cb.addItem("Toggle", "toggle")
            saved_mode = str(old_modes.get(int(bus_id), "ptt"))
            idx = mode_cb.findData(saved_mode)
            if idx >= 0:
                mode_cb.setCurrentIndex(idx)
            self._ptt_bus_modes[int(bus_id)] = str(mode_cb.currentData() or "ptt")
            mode_cb.currentIndexChanged.connect(lambda _i, bid=int(bus_id): self._on_ptt_bus_mode_changed(bid))

            state_lbl = QtWidgets.QLabel("")
            state_lbl.setFixedWidth(36)
            state_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._update_toggle_state_label(state_lbl, int(bus_id), str(mode_cb.currentData() or "ptt"))

            edit = _ShortcutKeySequenceEdit()
            edit.setToolTip("Click then press shortcut. Esc cancels. Del clears.")
            clear_btn = QtWidgets.QToolButton()
            clear_btn.setText("✕")
            clear_btn.setFixedSize(22, 22)
            clear_btn.setToolTip("Clear shortcut")
            clear_btn.clicked.connect(lambda _checked=False, e=edit: e.setKeySequence(QtGui.QKeySequence("")))
            key_enabled = not bool(self._connected)
            edit.setEnabled(bool(key_enabled))
            clear_btn.setEnabled(bool(key_enabled))

            seq = str(old_keys.get(int(bus_id), "") or "")
            try:
                edit.setKeySequence(QtGui.QKeySequence(seq))
            except Exception:
                pass
            edit.keySequenceChanged.connect(lambda _seq, bid=int(bus_id): self._on_ptt_bus_key_changed(bid))

            vu_meter = VuMeter()
            vu_meter.setMinimumWidth(60)
            vu_meter.set_level(-60.0)

            self._bus_rows_layout.addWidget(lbl, row, 0)
            self._bus_rows_layout.addWidget(mode_cb, row, 1)
            self._bus_rows_layout.addWidget(state_lbl, row, 2)
            self._bus_rows_layout.addWidget(vu_meter, row, 3)
            self._bus_rows_layout.addWidget(self._make_shortcut_widget(edit, clear_btn), row, 4)

            self._ptt_bus_keys[int(bus_id)] = edit
            self._ptt_bus_clear[int(bus_id)] = clear_btn
            self._ptt_bus_mode_widgets[int(bus_id)] = mode_cb
            self._ptt_bus_state_labels[int(bus_id)] = state_lbl
            self._ptt_bus_vu[int(bus_id)] = vu_meter
            row += 1

        self._known_buses = dict(parsed)
        self._bus_rows_signature = tuple((int(bid), str(parsed[bid].get("name"))) for bid in sorted(parsed.keys()))

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
        self._preset["listen_regie"] = True
        self._preset["listen_return_bus"] = False
        self._preset["input_gain_db"] = 0.0
        self._preset["output_gain_db"] = 0.0
        stable_id = int(client_id_from_uuid(client_uuid))

        self._preset_save()

        self._server_ip.setText("")
        self._name.setText("")
        self._mic_gain.setValue(0)
        self._hp_gain.setValue(0)
        self._mic_gain_lbl.setText("0 dB")
        self._hp_gain_lbl.setText("0 dB")
        self._return_gain.setValue(0)
        self._return_gain_lbl.setText("0 dB")

        try:
            for edit in self._ptt_bus_keys.values():
                edit.setKeySequence(QtGui.QKeySequence(""))
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

    def _on_input_device_changed(self, _idx: int) -> None:
        try:
            dev = self._input_device.currentData()
            if dev is None:
                return
            self._preset_set("input_device", int(dev))
            info = self._device_cache.get(int(dev))
            if info is not None:
                self._preset_set("input_device_name", str(getattr(info, "name", "")))
                self._preset_set("input_device_hostapi", str(getattr(info, "hostapi", "")))
        except Exception:
            return

    def _on_output_device_changed(self, _idx: int) -> None:
        try:
            dev = self._output_device.currentData()
            if dev is None:
                return
            self._preset_set("output_device", int(dev))
            info = self._device_cache.get(int(dev))
            if info is not None:
                self._preset_set("output_device_name", str(getattr(info, "name", "")))
                self._preset_set("output_device_hostapi", str(getattr(info, "hostapi", "")))
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
        stable_id = int(client_id_from_uuid(client_uuid))
        self._client_id.setText(str(stable_id))

    def _show_geek_info(self) -> None:
        server_ip = self._server_ip.text().strip()
        server_port = int(AUDIO_UDP_PORT)
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
                "Mode: PTT only",
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
        self._device_worker = DeviceWorker(hostapi_filter=hostapi_filter)
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

        self._device_cache = {int(d.index): d for d in devs}

        cur_in = self._input_device.currentData()
        cur_out = self._output_device.currentData()
        try:
            saved_in = int(self._preset_get("input_device", -1))
        except Exception:
            saved_in = -1
        saved_in_name = str(self._preset_get("input_device_name", "") or "")
        saved_in_hostapi = str(self._preset_get("input_device_hostapi", "") or "")
        try:
            saved_out = int(self._preset_get("output_device", -1))
        except Exception:
            saved_out = -1
        saved_out_name = str(self._preset_get("output_device_name", "") or "")
        saved_out_hostapi = str(self._preset_get("output_device_hostapi", "") or "")

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

            resolved_in = resolve_device(saved_in_name, saved_in_hostapi, int(saved_in) if int(saved_in) >= 0 else None, devs)
            if resolved_in is None and cur_in is not None:
                resolved_in = resolve_device("", "", int(cur_in), devs)
            if resolved_in is not None:
                i = self._input_device.findData(int(resolved_in))
                if i >= 0:
                    self._input_device.setCurrentIndex(i)
                info = self._device_cache.get(int(resolved_in))
                if info is not None:
                    self._preset_set("input_device", int(resolved_in))
                    self._preset_set("input_device_name", str(getattr(info, "name", "")))
                    self._preset_set("input_device_hostapi", str(getattr(info, "hostapi", "")))

            resolved_out = resolve_device(saved_out_name, saved_out_hostapi, int(saved_out) if int(saved_out) >= 0 else None, devs)
            if resolved_out is None and cur_out is not None:
                resolved_out = resolve_device("", "", int(cur_out), devs)
            if resolved_out is not None:
                i = self._output_device.findData(int(resolved_out))
                if i >= 0:
                    self._output_device.setCurrentIndex(i)
                info = self._device_cache.get(int(resolved_out))
                if info is not None:
                    self._preset_set("output_device", int(resolved_out))
                    self._preset_set("output_device_name", str(getattr(info, "name", "")))
                    self._preset_set("output_device_hostapi", str(getattr(info, "hostapi", "")))
        finally:
            self._input_device.blockSignals(False)
            self._output_device.blockSignals(False)

        self._maybe_autoconnect()

    def _connect(self) -> None:
        if self._connected:
            return

        server_ip = self._server_ip.text().strip()
        if not server_ip:
            QtWidgets.QMessageBox.warning(self, "Missing server", "Please enter server IP")
            return

        server_port = int(AUDIO_UDP_PORT)
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
        self._preset_set("server_ip", server_ip)
        self._preset_set("server_port", int(AUDIO_UDP_PORT))
        self._preset_set("name", name)

        client_uuid = str(self._preset_get("client_uuid", ""))

        cfg = ClientConfig(
            server_ip=server_ip,
            server_port=int(server_port),
            control_port=int(server_port) + 1,
            name=name,
            client_uuid=client_uuid,
            input_device=int(input_device),
            output_device=int(output_device),
            input_gain_db=float(self._mic_gain.value()),
            output_gain_db=float(self._hp_gain.value()),
            return_gain_db=float(self._return_gain.value()),
            ptt_bus_keys=dict(self._preset_get("ptt_bus_keys", {}) or {}),
            listen_return_bus=bool(self._listen_return_bus.isChecked()),
            listen_regie=bool(self._listen_regie.isChecked()),
        )

        # Reuse existing client if audio devices haven't changed (avoids PortAudio reopen issues on Windows)
        can_reuse = (
            self._client is not None
            and self._client.config.input_device == cfg.input_device
            and self._client.config.output_device == cfg.output_device
        )

        new_client: Optional[IntercomClient] = None
        try:
            if can_reuse:
                # Update config fields that may have changed
                self._client.config.server_ip = cfg.server_ip
                self._client.config.server_port = cfg.server_port
                self._client.config.control_port = int(cfg.server_port) + 1
                self._client.config.name = cfg.name
                self._client.config.client_uuid = cfg.client_uuid
                self._client.config.ptt_bus_keys = cfg.ptt_bus_keys
                self._client.config.listen_return_bus = bool(cfg.listen_return_bus)
                self._client.config.listen_regie = bool(cfg.listen_regie)
                self._client.set_input_gain_db(cfg.input_gain_db)
                self._client.set_output_gain_db(cfg.output_gain_db)
                self._client.set_return_gain_db(cfg.return_gain_db)
                self._client.set_listen_return_bus(bool(cfg.listen_return_bus))
                self._client.set_listen_regie(bool(cfg.listen_regie))
                self._client.reconnect_network()
            else:
                # Full stop of old client if devices changed
                if self._client is not None:
                    try:
                        self._client.stop()
                    except Exception:
                        pass
                new_client = IntercomClient(client_id=client_id, config=cfg)
                new_client.start()
                self._client = new_client
        except Exception as e:
            if new_client is not None:
                try:
                    new_client.stop()
                except Exception:
                    pass
            elif self._client is not None:
                try:
                    self._client.stop()
                except Exception:
                    pass
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
            for edit in self._ptt_bus_keys.values():
                edit.setEnabled(False)
            for b in self._ptt_bus_clear.values():
                b.setEnabled(False)
        except Exception:
            pass

        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._listen_regie.setEnabled(True)
        self._listen_return_bus.setEnabled(True)
        self._mic_gain.setEnabled(True)
        self._hp_gain.setEnabled(True)
        self._return_gain.setEnabled(True)

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
            self._listen_regie.setEnabled(False)
            self._listen_return_bus.setEnabled(False)
            self._mic_gain.setEnabled(False)
            self._hp_gain.setEnabled(False)
            self._return_gain.setEnabled(False)
            self._in_vu.set_level(-60.0)
            self._out_vu.set_level(-60.0)
            self._return_vu.set_level(-60.0)
            for vu_w in self._ptt_bus_vu.values():
                try:
                    vu_w.set_level(-60.0)
                except Exception:
                    pass

            try:
                for bid in self._ptt_bus_keys.keys():
                    edit = self._ptt_bus_keys.get(int(bid))
                    clear_btn = self._ptt_bus_clear.get(int(bid))
                    if edit is not None:
                        edit.setEnabled(True)
                    if clear_btn is not None:
                        clear_btn.setEnabled(True)
            except Exception:
                pass

    def _on_listen_regie_changed(self, state: int) -> None:
        enabled = is_checked(state)
        try:
            self._preset_set("listen_regie", bool(enabled))
        except Exception:
            pass
        if self._client is None:
            return
        self._client.set_listen_regie(bool(enabled))

    def _on_listen_return_bus_changed(self, state: int) -> None:
        enabled = is_checked(state)
        try:
            self._preset_set("listen_return_bus", bool(enabled))
        except Exception:
            pass
        if self._client is None:
            return
        self._client.set_listen_return_bus(bool(enabled))

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

    def _on_return_gain_changed(self, _val) -> None:
        try:
            val = int(self._return_gain.value())
        except Exception:
            val = 0
        self._return_gain_lbl.setText(f"{int(val)} dB")
        try:
            self._preset_set("return_gain_db", int(val))
        except Exception:
            pass
        if self._client is not None:
            try:
                self._client.set_return_gain_db(float(val))
            except Exception:
                pass

    def _on_start_minimized_changed(self, state: int) -> None:
        try:
            self._preset_set("start_minimized", bool(is_checked(state)))
        except Exception:
            pass

    def _on_autoconnect_changed(self, state: int) -> None:
        try:
            self._preset_set("autoconnect", bool(is_checked(state)))
        except Exception:
            pass

    def _maybe_autoconnect(self) -> None:
        if not self._connected and bool(self._autoconnect_cb.isChecked()):
            self._connect()

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

        self._in_vu.set_level(float(st.get("in_vu_dbfs", -60.0)))
        self._out_vu.set_level(float(st.get("out_vu_dbfs", -60.0)))
        self._return_vu.set_level(float(st.get("return_vu_dbfs", -60.0)))

        try:
            shared_input_gain = int(round(float(st.get("input_gain_db", self._mic_gain.value()))))
        except Exception:
            shared_input_gain = int(self._mic_gain.value())
        if int(self._mic_gain.value()) != int(shared_input_gain):
            self._mic_gain.blockSignals(True)
            try:
                self._mic_gain.setValue(int(shared_input_gain))
            finally:
                self._mic_gain.blockSignals(False)
            self._mic_gain_lbl.setText(f"{int(shared_input_gain)} dB")
            try:
                self._preset_set("input_gain_db", float(shared_input_gain))
            except Exception:
                pass

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
                    "feed_to_regie": bool(b.get("feed_to_regie", False)),
                }

        if parsed_buses:
            self._sync_bus_widgets(parsed_buses)

        bus_vu = st.get("bus_vu_dbfs")
        if isinstance(bus_vu, dict):
            for bid, vu_widget in self._ptt_bus_vu.items():
                try:
                    vu_widget.set_level(float(bus_vu.get(int(bid), -60.0)))
                except Exception:
                    pass

        try:
            self._refresh_state_labels()
        except Exception:
            pass

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        try:
            if event.type() == QtCore.QEvent.Type.WindowDeactivate:
                if self._global_ptt is not None:
                    self._global_ptt.release_all()
        except Exception:
            pass

    def _set_app_icon(self) -> None:
        try:
            import sys as _sys
            _base = getattr(_sys, "_MEIPASS", None)
            if _base:
                icon_path = Path(_base) / "py_intercom" / "img" / "logo.ico"
                if not icon_path.is_file():
                    icon_path = Path(_base) / "img" / "logo.ico"
                if not icon_path.is_file():
                    icon_path = Path(_base) / "logo.ico"
            else:
                icon_path = Path(__file__).resolve().parents[1] / "img" / "logo.ico"
                if not icon_path.is_file():
                    icon_path = Path(__file__).resolve().parents[1] / "img" / "logo.png"
            if icon_path.is_file():
                icon = QtGui.QIcon(str(icon_path))
                self.setWindowIcon(icon)
                app = QtWidgets.QApplication.instance()
                if app is not None:
                    app.setWindowIcon(icon)
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("py-intercom.client")
            except Exception:
                pass
        except Exception:
            pass

def run_gui(
    server_ip: str = "",
    server_port: int = AUDIO_UDP_PORT,
    input_device: int = -1,
    output_device: int = -1,
    minimized: bool = False,
) -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    win = ClientWindow()

    if server_ip:
        win._server_ip.setText(server_ip)

    if int(input_device) >= 0:
        i = win._input_device.findData(int(input_device))
        if i >= 0:
            win._input_device.setCurrentIndex(i)
    if int(output_device) >= 0:
        i = win._output_device.findData(int(output_device))
        if i >= 0:
            win._output_device.setCurrentIndex(i)

    if minimized or bool(win._start_minimized.isChecked()):
        win.showMinimized()
    else:
        win.show()
    return int(app.exec())

