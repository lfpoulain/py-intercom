from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .server import IntercomServer
from ..common.devices import list_devices, resolve_device
from ..common.gui_utils import DeviceWorker, is_checked
from ..common.identity import client_id_from_uuid
from ..common.jsonio import atomic_write_json, read_json_file
from ..common.constants import AUDIO_UDP_PORT
from ..common.theme import VuMeter, StatusIndicator, apply_theme, patch_combo, centered_checkbox, cell_vu



class ServerWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Py-Intercom Server")
        self.setMinimumSize(700, 500)
        self._set_app_icon()

        self._server: Optional[IntercomServer] = None
        self._settings = QtCore.QSettings("py-intercom", "server-gui")

        # Status bar
        self._status_bar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_label = QtWidgets.QLabel("Stopped")
        self._status_bar.addWidget(self._status_label, 1)

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        # -- Server config group --
        config_box = QtWidgets.QGroupBox("Server")
        config_lay = QtWidgets.QGridLayout(config_box)
        config_lay.setContentsMargins(10, 8, 10, 8)
        config_lay.setVerticalSpacing(6)
        config_lay.setHorizontalSpacing(8)

        self._server_name = QtWidgets.QLineEdit("py-intercom")
        self._server_name.setPlaceholderText("Server name")
        self._server_name.setMinimumWidth(180)
        self._server_name.setMaximumWidth(340)
        self._discovery_cb = QtWidgets.QCheckBox("Discovery")
        self._discovery_cb.setChecked(True)
        self._discovery_cb.setToolTip("Broadcast LAN beacon so clients can auto-detect this server")
        self._autostart_cb = QtWidgets.QCheckBox("Auto-start")
        self._autostart_cb.setChecked(bool(self._settings.value("autostart", False, type=bool)))
        self._start_minimized_cb = QtWidgets.QCheckBox("Start minimized")
        self._start_minimized_cb.setChecked(bool(self._settings.value("start_minimized", False, type=bool)))

        self._return_enabled = QtWidgets.QCheckBox("Return")
        self._return_enabled.setChecked(False)
        self._return_input_device = QtWidgets.QComboBox()
        patch_combo(self._return_input_device)
        self._return_vu = VuMeter()
        self._regie_vu = VuMeter()
        self._show_all_devices = QtWidgets.QCheckBox("Show all devices")
        self._refresh_devices_btn = QtWidgets.QToolButton()
        self._refresh_devices_btn.setText("\u21bb")
        self._refresh_devices_btn.setFixedSize(28, 28)
        self._refresh_devices_btn.setToolTip("Refresh audio devices")
        self._device_status = QtWidgets.QLabel("")

        self._start_btn = QtWidgets.QPushButton("▶  Start")
        self._start_btn.setProperty("class", "success")
        self._start_btn.setFixedWidth(120)
        self._stop_btn = QtWidgets.QPushButton("■  Stop")
        self._stop_btn.setProperty("class", "danger")
        self._stop_btn.setFixedWidth(120)
        self._stop_btn.setEnabled(False)

        # Left column: Name only
        left_col = QtWidgets.QVBoxLayout()
        left_col.setSpacing(6)

        name_lay = QtWidgets.QHBoxLayout()
        name_lay.setSpacing(8)
        name_lay.addWidget(QtWidgets.QLabel("Name"))
        name_lay.addWidget(self._server_name)
        left_col.addLayout(name_lay)
        left_col.addStretch(1)

        # Middle column: Start / Stop stacked
        mid_col = QtWidgets.QVBoxLayout()
        mid_col.setSpacing(6)
        mid_col.addWidget(self._start_btn)
        mid_col.addWidget(self._stop_btn)
        mid_col.addStretch(1)

        # Right column: options stacked
        right_col = QtWidgets.QVBoxLayout()
        right_col.setSpacing(4)
        right_col.addWidget(self._discovery_cb)
        right_col.addWidget(self._autostart_cb)
        right_col.addWidget(self._start_minimized_cb)
        right_col.addStretch(1)

        # Combine into one row
        row_lay = QtWidgets.QHBoxLayout()
        row_lay.setSpacing(16)
        row_lay.addLayout(left_col, 1)
        row_lay.addLayout(mid_col, 0)
        row_lay.addLayout(right_col, 0)
        config_lay.addLayout(row_lay, 0, 0, 1, 6)

        # -- Return bus + Régie group (combined) --
        return_box = QtWidgets.QGroupBox("Return bus")
        return_lay = QtWidgets.QVBoxLayout(return_box)
        return_lay.setContentsMargins(10, 8, 10, 8)
        return_lay.setSpacing(6)

        # Row 0 — Enable + Input device
        ret_input_lay = QtWidgets.QHBoxLayout()
        ret_input_lay.setSpacing(8)
        ret_input_lay.addWidget(self._return_enabled)
        ret_input_lay.addWidget(self._return_input_device, 1)
        return_lay.addLayout(ret_input_lay)

        # Row 1 — VU meters side by side
        vu_lay = QtWidgets.QHBoxLayout()
        vu_lay.setSpacing(12)
        lbl_ret = QtWidgets.QLabel("Return")
        lbl_ret.setFixedWidth(50)
        vu_lay.addWidget(lbl_ret)
        vu_lay.addWidget(self._return_vu, 1)
        lbl_regie = QtWidgets.QLabel("Régie")
        lbl_regie.setFixedWidth(50)
        vu_lay.addWidget(lbl_regie)
        vu_lay.addWidget(self._regie_vu, 1)
        return_lay.addLayout(vu_lay)

        # -- Regie group (kept as placeholder, hidden) --
        regie_box = QtWidgets.QWidget()  # invisible spacer, regie VU moved to return_box

        # -- Clients group --
        clients_box = QtWidgets.QGroupBox("Clients")
        clients_lay = QtWidgets.QVBoxLayout(clients_box)
        clients_lay.setContentsMargins(6, 6, 6, 6)
        clients_lay.setSpacing(6)

        # Base columns: 0=ClientID(hidden), 1=Name, 2=Addr(hidden), 3=Status, 4=VU
        self._clients = QtWidgets.QTableWidget(0, 5)
        self._clients.setHorizontalHeaderLabels(
            ["Client ID", "Name", "Addr", "", "VU"]
        )
        self._clients.setColumnHidden(0, True)
        self._clients.setColumnHidden(2, True)   # Addr hidden, shown in Info dialog
        hdr = self._clients.horizontalHeader()
        hdr.setDefaultSectionSize(60)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)   # Name
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)   # Status
        self._clients.setColumnWidth(3, 30)
        hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)   # VU
        self._clients.verticalHeader().setDefaultSectionSize(32)
        self._clients.verticalHeader().setVisible(False)
        self._clients.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._clients.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._clients.setAlternatingRowColors(True)

        self._client_row_ids: list[int] = []
        self._client_uuid_by_id: Dict[int, str] = {}
        self._vu_widgets: Dict[int, VuMeter] = {}
        self._status_widgets: Dict[int, StatusIndicator] = {}

        self._forget_client_btn = QtWidgets.QPushButton("Remove client")
        self._forget_client_btn.setProperty("class", "danger")
        self._forget_client_btn.setEnabled(False)
        self._forget_client_btn.clicked.connect(self._forget_selected_client)

        clients_bottom = QtWidgets.QHBoxLayout()
        clients_bottom.addWidget(self._forget_client_btn)
        clients_bottom.addStretch(1)

        clients_lay.addWidget(self._clients)
        clients_lay.addLayout(clients_bottom)

        # -- Outputs group --
        self._output_row_ids: list[int] = []
        self._output_device_widgets: Dict[int, QtWidgets.QComboBox] = {}
        self._output_bus_widgets: Dict[int, QtWidgets.QComboBox] = {}
        self._output_vu_widgets: Dict[int, VuMeter] = {}
        self._output_devices_cache: list[tuple[str, int]] = []
        self._bus_selector = QtWidgets.QComboBox()
        patch_combo(self._bus_selector)
        self._bus_selector_ids: list[int] = []
        self._bus_row_ids: list[int] = []
        self._bus_feed_widgets: Dict[int, QtWidgets.QCheckBox] = {}

        self._buses = QtWidgets.QTableWidget(0, 3)
        self._buses.setHorizontalHeaderLabels(["ID", "Name", "Retour Regie"])
        self._buses.verticalHeader().setVisible(False)
        self._buses.verticalHeader().setDefaultSectionSize(32)
        self._buses.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._buses.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._buses.setAlternatingRowColors(True)
        self._buses.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self._buses.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._buses.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        self._add_output_device = QtWidgets.QComboBox()
        patch_combo(self._add_output_device)
        self._add_output_device.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._add_output_device.setMinimumContentsLength(40)
        self._add_output_btn = QtWidgets.QPushButton("Add output")
        self._add_output_btn.setProperty("class", "success")
        self._remove_output_btn = QtWidgets.QPushButton("Remove output")
        self._remove_output_btn.setProperty("class", "danger")
        self._remove_output_btn.setEnabled(False)

        self._outputs = QtWidgets.QTableWidget(0, 4)
        self._outputs.setHorizontalHeaderLabels(["ID", "Device", "Bus", "VU"])
        out_hdr = self._outputs.horizontalHeader()
        out_hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self._outputs.setColumnWidth(0, 30)
        out_hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        out_hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self._outputs.setColumnWidth(2, 75)
        out_hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._outputs.verticalHeader().setDefaultSectionSize(32)
        self._outputs.verticalHeader().setVisible(False)
        self._outputs.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._outputs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._outputs.setAlternatingRowColors(True)
        self._outputs.itemSelectionChanged.connect(self._on_output_selection_changed)

        outputs_box = QtWidgets.QGroupBox("Outputs")
        outputs_layout = QtWidgets.QVBoxLayout(outputs_box)
        outputs_layout.setContentsMargins(6, 6, 6, 6)
        outputs_layout.setSpacing(6)

        outputs_layout.addWidget(self._buses)

        outputs_top = QtWidgets.QGridLayout()
        outputs_top.addWidget(QtWidgets.QLabel("Device"), 0, 0)
        outputs_top.addWidget(self._add_output_device, 0, 1, 1, 2)
        outputs_top.addWidget(QtWidgets.QLabel("Bus"), 0, 3)
        outputs_top.addWidget(self._bus_selector, 0, 4)
        outputs_top.addWidget(self._add_output_btn, 0, 5)
        outputs_top.addWidget(self._remove_output_btn, 0, 6)
        outputs_layout.addLayout(outputs_top)
        outputs_layout.addWidget(self._outputs)

        # -- Info button --
        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("ℹ")
        self._info_btn.setFixedSize(26, 26)
        self._info_btn.setToolTip("Info / Debug")
        self._info_btn.clicked.connect(self._show_geek_info)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self._show_all_devices)
        bottom.addWidget(self._refresh_devices_btn)
        bottom.addWidget(self._device_status, 1)
        bottom.addWidget(self._info_btn)

        # -- Splitter for clients / outputs --
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        splitter.addWidget(clients_box)
        splitter.addWidget(outputs_box)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)

        # -- Main layout --
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(config_box)
        layout.addWidget(return_box)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(lambda _: self._start_device_refresh())
        self._start_btn.clicked.connect(self._start_server)
        self._stop_btn.clicked.connect(self._stop_server)
        self._add_output_btn.clicked.connect(self._on_add_output)
        self._remove_output_btn.clicked.connect(self._on_remove_output)
        self._return_enabled.stateChanged.connect(self._on_return_enabled_changed)
        self._return_input_device.currentIndexChanged.connect(self._on_return_input_device_changed)
        self._autostart_cb.stateChanged.connect(self._on_autostart_changed)
        self._start_minimized_cb.stateChanged.connect(self._on_start_minimized_changed)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._refresh_clients)

        self._device_thread: Optional[QtCore.QThread] = None
        self._device_worker: Optional[DeviceWorker] = None
        self._device_timeout: Optional[QtCore.QTimer] = None
        self._last_device_error: str = ""
        self._last_device_count: int = 0
        self._return_input_devices_cache: list[tuple[str, int]] = []

        self._update_controls_for_server_state()

        QtCore.QTimer.singleShot(0, self._start_device_refresh)
        QtCore.QTimer.singleShot(0, self._load_preset_preview)
        QtCore.QTimer.singleShot(0, self._maybe_autostart)

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
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("py-intercom.server")
            except Exception:
                pass
        except Exception:
            pass

    def _update_controls_for_server_state(self) -> None:
        running = self._server is not None

        self._add_output_device.setEnabled(bool(running))
        self._bus_selector.setEnabled(bool(running))
        can_add_output = bool(running) and self._add_output_device.currentData() is not None and self._bus_selector.currentData() is not None
        self._add_output_btn.setEnabled(bool(can_add_output))
        self._on_output_selection_changed()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_server()
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

    def _preset_paths(self) -> Path:
        return Path.home() / "py-intercom" / "server_preset.json"

    def _load_preset_preview(self) -> None:
        if self._server is not None:
            return

        server_preset = self._preset_paths()
        outputs: list[dict] = []
        buses_raw: dict = {}
        clients_raw: dict = {}
        data = read_json_file(server_preset)
        if isinstance(data, dict) and isinstance(data.get("outputs"), list):
            outputs = list(data.get("outputs") or [])
        if isinstance(data, dict) and isinstance(data.get("buses"), dict):
            buses_raw = dict(data.get("buses") or {})
        if isinstance(data, dict) and isinstance(data.get("clients"), dict):
            clients_raw = dict(data.get("clients") or {})
        return_enabled = bool(data.get("return_enabled", False)) if isinstance(data, dict) else False
        return_input_device = None
        if isinstance(data, dict):
            return_input_device = data.get("return_input_device")
            try:
                return_input_device = int(return_input_device) if return_input_device is not None else None
            except Exception:
                return_input_device = None
        return_input_device_name = str(data.get("return_input_device_name") or "") if isinstance(data, dict) else ""
        return_input_device_hostapi = str(data.get("return_input_device_hostapi") or "") if isinstance(data, dict) else ""

        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=True, validate=False)
        except Exception:
            devices = []

        return_input_device = resolve_device(
            return_input_device_name,
            return_input_device_hostapi,
            return_input_device,
            devices,
        )

        uuid_to_id: Dict[str, int] = {}
        for u in clients_raw.keys():
            try:
                uuid_to_id[str(u)] = int(client_id_from_uuid(str(u)))
            except Exception:
                continue

        feed_by_id = {1: False, 2: False}
        for bus_id_str, b in buses_raw.items():
            try:
                bid = int(bus_id_str)
            except Exception:
                continue
            if bid not in (1, 2) or not isinstance(b, dict):
                continue
            feed_by_id[int(bid)] = bool(b.get("feed_to_regie", False))

        buses = {
            0: {"bus_id": 0, "name": "Regie", "feed_to_regie": False},
            1: {"bus_id": 1, "name": "Plateau", "feed_to_regie": bool(feed_by_id[1])},
            2: {"bus_id": 2, "name": "VMix", "feed_to_regie": bool(feed_by_id[2])},
        }

        snap: Dict[int, dict] = {}
        for u, c in clients_raw.items():
            cid = uuid_to_id.get(str(u))
            if cid is None:
                continue
            name = ""
            muted = False
            if isinstance(c, dict):
                name = str(c.get("name") or "")
            snap[int(cid)] = {
                "client_id": int(cid),
                "name": name,
                "client_uuid": str(u),
                "addr": None,
                "age_s": None,
                "vu_dbfs": -60.0,
                "last_timestamp_ms": 0,
                "last_sequence_number": 0,
                "control_connected": False,
                "control_age_s": None,
            }

        outs: Dict[int, dict] = {}
        for oid, o in enumerate(outputs):
            if not isinstance(o, dict):
                continue
            outs[int(oid)] = {
                "output_id": int(oid),
                "device": o.get("device"),
                "bus_id": o.get("bus_id", 0),
                "samplerate": "",
                "queued_ms": "",
                "underflows": "",
                "vu_dbfs": "",
            }

        self._refresh_bus_selectors(buses)
        self._refresh_buses_table(buses)
        self._refresh_outputs_table(outs, buses)
        self._set_clients_table(snap, buses)
        self._return_enabled.setChecked(bool(return_enabled))
        if return_input_device is not None:
            idx = self._return_input_device.findData(int(return_input_device))
            if idx >= 0:
                self._return_input_device.setCurrentIndex(idx)

    def _reset_total_config(self) -> None:
        if self._server is not None:
            QtWidgets.QMessageBox.warning(self, "Running", "Stop the server first")
            return

        if (
            QtWidgets.QMessageBox.question(
                self,
                "Reset config",
                "Delete server preset file and reset server configuration?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        server_preset = self._preset_paths()
        try:
            tmp = server_preset.with_suffix(server_preset.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

        try:
            if server_preset.exists():
                server_preset.unlink()
        except Exception:
            pass

        self._outputs.setRowCount(0)
        self._buses.setRowCount(0)
        self._output_row_ids = []
        self._bus_row_ids = []
        self._output_device_widgets.clear()
        self._output_bus_widgets.clear()
        self._bus_feed_widgets.clear()
        self._load_preset_preview()

    def _reopen_outputs_after_start(self) -> None:
        if self._server is None:
            return
        try:
            self._server.reopen_outputs(force=True)
        except Exception:
            pass

    def _reopen_return_after_start(self) -> None:
        if self._server is None:
            return
        try:
            self._server.reopen_return_input()
        except Exception:
            pass

    def _refresh_bus_selectors(self, buses: Dict[int, dict]) -> None:
        sorted_buses = [
            (int(bid), b)
            for bid, b in sorted(buses.items(), key=lambda kv: int(kv[0]))
            if int(bid) in (0, 1, 2)
        ]
        bus_ids = [int(bid) for bid, _ in sorted_buses]

        if bus_ids == self._bus_selector_ids and self._bus_selector.count() > 0:
            return

        self._bus_selector.blockSignals(True)
        try:
            current = self._bus_selector.currentData()
            self._bus_selector.clear()
            for bid, b in sorted_buses:
                self._bus_selector.addItem(str(b.get("name") or f"Bus {int(bid)}"), int(bid))
            if current is not None:
                i = self._bus_selector.findData(current)
                if i >= 0:
                    self._bus_selector.setCurrentIndex(i)
            self._bus_selector_ids = list(bus_ids)
        finally:
            self._bus_selector.blockSignals(False)

    def _refresh_buses_table(self, buses: Dict[int, dict]) -> None:
        bus_ids = [int(bid) for bid in sorted(buses.keys())]
        rebuild = bus_ids != self._bus_row_ids
        if rebuild:
            self._buses.setRowCount(len(bus_ids))
            self._bus_row_ids = list(bus_ids)
            self._bus_feed_widgets.clear()

        for row, bid in enumerate(bus_ids):
            b = buses.get(int(bid), {})

            def _set_item(col: int, text: str) -> None:
                it = self._buses.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem(text)
                    self._buses.setItem(row, col, it)
                else:
                    it.setText(text)

            _set_item(0, str(int(bid)))
            _set_item(1, str(b.get("name") or f"Bus {int(bid)}"))
            if rebuild:
                cb = QtWidgets.QCheckBox()
                cb.stateChanged.connect(lambda state, bus_id=int(bid): self._on_bus_feed_to_regie_changed(bus_id, state))
                self._buses.setCellWidget(row, 2, centered_checkbox(cb))
                self._bus_feed_widgets[int(bid)] = cb

            cb = self._bus_feed_widgets.get(int(bid))
            if cb is not None:
                cb.blockSignals(True)
                try:
                    cb.setEnabled(self._server is not None and int(bid) in (1, 2))
                    if int(bid) == 0:
                        cb.setChecked(True)
                    else:
                        cb.setChecked(bool(b.get("feed_to_regie", False)) if int(bid) in (1, 2) else False)
                finally:
                    cb.blockSignals(False)
    def _on_bus_feed_to_regie_changed(self, bus_id: int, state: int) -> None:
        if self._server is None:
            return
        self._server.set_bus_feed_to_regie(int(bus_id), is_checked(state))

    def _refresh_outputs_table(self, outs: Dict[int, dict], buses: Dict[int, dict]) -> None:
        output_ids = [oid for oid in sorted(outs.keys())]
        rebuild = output_ids != self._output_row_ids
        if rebuild:
            self._outputs.setRowCount(len(output_ids))
            self._output_row_ids = list(output_ids)
            self._output_device_widgets.clear()
            self._output_bus_widgets.clear()
            self._output_vu_widgets.clear()

        bus_names = {
            int(bid): str(buses.get(int(bid), {}).get("name", bid))
            for bid in buses.keys()
            if int(bid) in (0, 1, 2)
        }
        sorted_bus_ids = [int(bid) for bid in sorted(bus_names.keys())]

        for row, oid in enumerate(output_ids):
            st = outs.get(int(oid), {})

            def _set_item(col: int, text: str) -> None:
                it = self._outputs.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem(text)
                    self._outputs.setItem(row, col, it)
                else:
                    it.setText(text)

            _set_item(0, str(oid))

            if rebuild:
                dev_cb = QtWidgets.QComboBox()
                patch_combo(dev_cb)
                dev_cb.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                dev_cb.setMinimumContentsLength(40)
                dev_cb.blockSignals(True)
                for label, idx in self._output_devices_cache:
                    dev_cb.addItem(label, idx)
                dev_cb.blockSignals(False)
                dev_cb.currentIndexChanged.connect(lambda _i, out_id=int(oid): self._on_output_device_changed(out_id))
                self._outputs.setCellWidget(row, 1, dev_cb)
                self._output_device_widgets[int(oid)] = dev_cb

                bus_cb = QtWidgets.QComboBox()
                patch_combo(bus_cb)
                bus_cb.blockSignals(True)
                for bid in sorted_bus_ids:
                    bus_cb.addItem(str(bus_names[bid]), int(bid))
                bus_cb.blockSignals(False)
                bus_cb.currentIndexChanged.connect(lambda _i, out_id=int(oid): self._on_output_bus_changed(out_id))
                self._outputs.setCellWidget(row, 2, bus_cb)
                self._output_bus_widgets[int(oid)] = bus_cb

            dev_cb = self._output_device_widgets.get(int(oid))
            if dev_cb is not None:
                dev_cb.blockSignals(True)
                try:
                    cur_dev = st.get("device")
                    if cur_dev is not None:
                        i = dev_cb.findData(int(cur_dev))
                        if i >= 0:
                            dev_cb.setCurrentIndex(i)
                    dev_cb.setEnabled(self._server is not None)
                finally:
                    dev_cb.blockSignals(False)

            bus_cb = self._output_bus_widgets.get(int(oid))
            if bus_cb is not None:
                bus_cb.blockSignals(True)
                try:
                    cur_bus = st.get("bus_id")
                    if cur_bus is not None:
                        i = bus_cb.findData(int(cur_bus))
                        if i >= 0:
                            bus_cb.setCurrentIndex(i)
                    bus_cb.setEnabled(self._server is not None)
                finally:
                    bus_cb.blockSignals(False)

            if rebuild:
                vu = VuMeter()
                self._outputs.setCellWidget(row, 3, cell_vu(vu))
                self._output_vu_widgets[int(oid)] = vu
            vu = self._output_vu_widgets.get(int(oid))
            if vu is not None:
                try:
                    vu.set_level(float(st.get("vu_dbfs", -60.0)))
                except Exception:
                    pass

    def _on_output_selection_changed(self) -> None:
        self._remove_output_btn.setEnabled(self._server is not None and len(self._outputs.selectedItems()) > 0)

    def _on_add_output(self) -> None:
        if self._server is None:
            return
        dev = self._add_output_device.currentData()
        if dev is None:
            return
        bus_data = self._bus_selector.currentData()
        if bus_data is None:
            return
        bus_id = int(bus_data)
        try:
            self._server.create_output(int(dev), int(bus_id))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Output", str(e))

    def _on_remove_output(self) -> None:
        if self._server is None:
            return
        row = self._outputs.currentRow()
        if row < 0:
            return
        item = self._outputs.item(row, 0)
        if item is None:
            return
        try:
            oid = int(item.text())
        except Exception:
            return
        self._server.remove_output(int(oid))

    def _on_output_device_changed(self, output_id: int) -> None:
        if self._server is None:
            return
        cb = self._output_device_widgets.get(int(output_id))
        if cb is None:
            return
        dev = cb.currentData()
        if dev is None:
            return
        self._server.set_output_device(int(output_id), int(dev))

    def _on_output_bus_changed(self, output_id: int) -> None:
        if self._server is None:
            return
        cb = self._output_bus_widgets.get(int(output_id))
        if cb is None:
            return
        bid = cb.currentData()
        if bid is None:
            return
        self._server.set_output_bus(int(output_id), int(bid))

    def _on_devices_refreshed(self, devs, error: str) -> None:
        if error:
            self._last_device_error = error
            self._device_status.setText(f"Device refresh failed: {error}")
            QtWidgets.QMessageBox.warning(self, "Device refresh failed", error)
            return

        self._last_device_count = int(len(devs))
        self._device_status.setText(f"Devices loaded: {self._last_device_count}")

        self._add_output_device.blockSignals(True)
        try:
            current = self._add_output_device.currentData()
            self._add_output_device.clear()
            self._output_devices_cache = []
            for d in devs:
                if d.max_output_channels <= 0:
                    continue
                label = f"{d.index}-{d.name}"
                self._add_output_device.addItem(label, d.index)
                self._output_devices_cache.append((label, int(d.index)))
            if current is not None:
                i = self._add_output_device.findData(current)
                if i >= 0:
                    self._add_output_device.setCurrentIndex(i)
        finally:
            self._add_output_device.blockSignals(False)

        outs_snap = self._server.get_outputs_snapshot() if self._server is not None else {}
        for oid, dev_cb in self._output_device_widgets.items():
            try:
                selected = dev_cb.currentData()
                if selected is None and self._server is not None:
                    st = outs_snap.get(int(oid), {})
                    cur_dev = st.get("device")
                    if cur_dev is not None:
                        selected = int(cur_dev)
                dev_cb.blockSignals(True)
                dev_cb.clear()
                for label, idx in self._output_devices_cache:
                    dev_cb.addItem(label, idx)
                if selected is not None:
                    i = dev_cb.findData(selected)
                    if i >= 0:
                        dev_cb.setCurrentIndex(i)
            except Exception:
                pass
            finally:
                try:
                    dev_cb.blockSignals(False)
                except Exception:
                    pass

        self._return_input_device.blockSignals(True)
        try:
            current_in = self._return_input_device.currentData()
            self._return_input_device.clear()
            self._return_input_devices_cache = []
            self._return_input_device.addItem("(none)", None)
            for d in devs:
                if d.max_input_channels <= 0:
                    continue
                label = f"{d.index}-{d.name}"
                self._return_input_device.addItem(label, d.index)
                self._return_input_devices_cache.append((label, int(d.index)))
            target_in = current_in
            if target_in is None:
                data = read_json_file(self._preset_paths())
                if isinstance(data, dict):
                    saved_in = data.get("return_input_device")
                    try:
                        saved_in = int(saved_in) if saved_in is not None else None
                    except Exception:
                        saved_in = None
                    saved_name = str(data.get("return_input_device_name") or "")
                    saved_hostapi = str(data.get("return_input_device_hostapi") or "")

                    target_in = resolve_device(saved_name, saved_hostapi, saved_in, devs)

            if target_in is not None:
                i = self._return_input_device.findData(int(target_in))
                if i >= 0:
                    self._return_input_device.setCurrentIndex(i)
        finally:
            self._return_input_device.blockSignals(False)

        if self._server is None:
            self._output_row_ids = []
            self._load_preset_preview()

    def _on_return_enabled_changed(self, state: int) -> None:
        if self._server is None:
            return
        self._server.set_return_enabled(is_checked(state))

    def _on_return_input_device_changed(self, _idx: int) -> None:
        dev = self._return_input_device.currentData()
        if self._server is not None:
            self._server.set_return_input_device(int(dev) if dev is not None else None)
            return

        server_preset = self._preset_paths()
        data = read_json_file(server_preset)
        if not isinstance(data, dict):
            data = {}
        try:
            device_idx = int(dev) if dev is not None else None
        except Exception:
            device_idx = None

        device_name = ""
        device_hostapi = ""
        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=False, validate=False)
        except Exception:
            devices = []
        if device_idx is not None:
            for d in devices:
                if int(d.index) == int(device_idx):
                    device_name = str(d.name)
                    device_hostapi = str(d.hostapi)
                    break

        data["return_input_device"] = int(device_idx) if device_idx is not None else None
        data["return_input_device_name"] = str(device_name)
        data["return_input_device_hostapi"] = str(device_hostapi)
        try:
            atomic_write_json(server_preset, data)
        except Exception:
            pass

    def _forget_selected_client(self) -> None:
        try:
            items = self._clients.selectedItems()
            if not items:
                return
            row = int(items[0].row())
            if row < 0 or row >= len(self._client_row_ids):
                return
            client_id = int(self._client_row_ids[row])
        except Exception:
            return

        if self._server is not None:
            self._server.forget_client(int(client_id))
            return

        client_uuid = str(self._client_uuid_by_id.get(int(client_id), "") or "")
        if not client_uuid:
            return

        server_preset = self._preset_paths()
        try:
            data = read_json_file(server_preset)
            if not isinstance(data, dict):
                data = {}

            clients = data.get("clients") if isinstance(data.get("clients"), dict) else {}
            clients.pop(str(client_uuid), None)
            data["clients"] = clients

            buses = data.get("buses") if isinstance(data.get("buses"), dict) else {}
            for _bid, b in buses.items():
                if not isinstance(b, dict):
                    continue
                uuids = b.get("source_uuids")
                if isinstance(uuids, list):
                    b["source_uuids"] = [str(u) for u in uuids if str(u) and str(u) != str(client_uuid)]
            data["buses"] = buses

            atomic_write_json(server_preset, data)
        except Exception:
            pass

        self._load_preset_preview()

    def _start_server(self) -> None:
        if self._server is not None:
            return

        # reset cached widgets (they may refer to deleted Qt objects after a previous stop/start)
        self._client_row_ids = []
        self._vu_widgets.clear()
        self._status_widgets.clear()

        self._output_row_ids = []
        self._bus_row_ids = []
        self._output_device_widgets.clear()
        self._output_bus_widgets.clear()
        self._output_vu_widgets.clear()
        self._bus_feed_widgets.clear()

        self._server = IntercomServer(
            bind_ip="0.0.0.0",
            port=int(AUDIO_UDP_PORT),
            output_device=None,
            server_name=self._server_name.text().strip() or "py-intercom",
            discovery_enabled=self._discovery_cb.isChecked(),
            return_input_device=self._return_input_device.currentData(),
            return_enabled=self._return_enabled.isChecked(),
        )

        try:
            self._server.load_preset()
            # Sync GUI back from the preset-loaded server state so the checkbox
            # reflects reality before start() opens the return stream.
            self._return_enabled.blockSignals(True)
            self._return_enabled.setChecked(bool(self._server._return_enabled))
            self._return_enabled.blockSignals(False)
            if self._server._return_input_device is not None:
                idx = self._return_input_device.findData(int(self._server._return_input_device))
                if idx >= 0:
                    self._return_input_device.blockSignals(True)
                    self._return_input_device.setCurrentIndex(idx)
                    self._return_input_device.blockSignals(False)
            self._server.save_preset()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Preset", f"Preset load failed: {e}")

        try:
            self._server.start()
        except Exception as e:
            logger.exception("server start failed")
            self._server = None
            QtWidgets.QMessageBox.critical(self, "Start failed", str(e))
            return

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._update_controls_for_server_state()

        try:
            snap = self._server.get_clients_snapshot()
            buses = self._server.get_buses_snapshot()
            outs = self._server.get_outputs_snapshot()
            self._refresh_bus_selectors(buses)
            self._refresh_buses_table(buses)
            self._set_clients_table(snap, buses)
            self._refresh_outputs_table(outs, buses)
        except Exception:
            pass

        self._timer.start()
        self._status_label.setText(f"Running on port {int(AUDIO_UDP_PORT)}")
        QtCore.QTimer.singleShot(1200, self._reopen_outputs_after_start)
        QtCore.QTimer.singleShot(2500, self._reopen_return_after_start)

    def _stop_server(self) -> None:
        if self._server is None:
            return
        try:
            self._timer.stop()
            self._server.stop()
        finally:
            self._server = None
            self._start_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
            self._clients.setRowCount(0)
            self._outputs.setRowCount(0)
            self._remove_output_btn.setEnabled(False)
            self._forget_client_btn.setEnabled(False)
            self._status_label.setText("Stopped")
            self._return_vu.set_level(-60.0)
            self._regie_vu.set_level(-60.0)

            self._client_row_ids = []
            self._vu_widgets.clear()
            self._status_widgets.clear()

            self._output_row_ids = []
            self._output_device_widgets.clear()
            self._output_bus_widgets.clear()
            self._output_vu_widgets.clear()
            self._buses.setRowCount(0)
            self._bus_row_ids = []
            self._bus_feed_widgets.clear()

            self._update_controls_for_server_state()
            self._load_preset_preview()

    def _refresh_clients(self) -> None:
        if self._server is None:
            return
        snap = self._server.get_clients_snapshot()
        buses = self._server.get_buses_snapshot()
        outs = self._server.get_outputs_snapshot()
        stats = self._server.get_stats_snapshot()
        self._refresh_bus_selectors(buses)
        self._refresh_buses_table(buses)
        self._set_clients_table(snap, buses)
        self._refresh_outputs_table(outs, buses)
        try:
            self._return_vu.set_level(float(stats.get("return_vu_dbfs", -60.0)))
        except Exception:
            self._return_vu.set_level(-60.0)

        try:
            self._regie_vu.set_level(float(stats.get("regie_vu_dbfs", -60.0)))
        except Exception:
            self._regie_vu.set_level(-60.0)

        try:
            items = self._clients.selectedItems()
            have_sel = bool(items)
        except Exception:
            have_sel = False
        self._forget_client_btn.setEnabled(bool(have_sel))
        self._update_controls_for_server_state()

    def _show_geek_info(self) -> None:
        port = int(AUDIO_UDP_PORT)
        ctrl_port = port + 1
        running = self._server is not None
        snap = self._server.get_clients_snapshot() if self._server is not None else {}
        stats = self._server.get_stats_snapshot() if self._server is not None else {}

        selected = None
        try:
            items = self._clients.selectedItems()
            if items:
                row = int(items[0].row())
                if 0 <= row < len(self._client_row_ids):
                    selected = int(self._client_row_ids[row])
        except Exception:
            selected = None

        lines = [
            f"Audio UDP port: {port}",
            f"Control TCP port: {ctrl_port}",
            f"Running: {running}",
            f"Clients: {len(snap)}",
            f"Outputs: {stats.get('outputs', '-')}",
            f"Mix queue: {stats.get('mix_q', '-')}",
            f"Underflows: {stats.get('underflows', '-')}",
            f"RX datagrams: {stats.get('rx_datagrams', '-')}",
            f"RX bytes: {stats.get('rx_bytes', '-')}",
            f"RX sock err: {stats.get('rx_socket_errors', '-')}",
            f"RX pkts: {stats.get('rx_packets', '-')}",
            f"RX dec err: {stats.get('rx_decode_errors', '-')}",
            f"TX pkts: {stats.get('tx_packets', '-')}",
            f"TX sock err: {stats.get('tx_socket_errors', '-')}",
            f"Opus OK: {stats.get('opus_ok', '-')}",
            f"Opuslib ver: {stats.get('opuslib_version', '-')}",
        ]

        if snap:
            lines.append("")
            lines.append("--- Connected clients ---")
            for cid, st in sorted(snap.items()):
                addr = st.get("addr")
                addr_str = f"{addr[0]}:{addr[1]}" if addr else "-"
                ctrl = "✓" if st.get("control_connected") else "✗"
                lines.append(f"  [{cid}] {st.get('name', '?')} — {addr_str} (ctrl: {ctrl})")

        if selected is not None and selected in snap:
            st = snap.get(int(selected), {})
            addr = st.get("addr")
            lines.extend(
                [
                    "",
                    f"Selected client_id: {selected}",
                    f"Name: {st.get('name', '')}",
                    f"UUID: {st.get('client_uuid') or '-'}",
                    f"Addr: {addr[0]}:{addr[1]}" if addr else "Addr: -",
                    f"Ctrl: {st.get('control_connected', False)}",
                ]
            )

        server_preset = ""
        if self._server is not None:
            try:
                server_preset = str(self._server.get_preset_paths_snapshot().get("server_preset") or "")
            except Exception:
                server_preset = ""
        if not server_preset:
            server_preset = str(self._preset_paths())

        exists = False
        try:
            exists = Path(server_preset).exists()
        except Exception:
            exists = False

        lines.extend(["", f"Server preset: {server_preset}", f"Preset exists: {exists}"])

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Info")
        box.setText("\n".join(lines))
        reset_btn = box.addButton("Reset config", QtWidgets.QMessageBox.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is reset_btn:
            self._reset_total_config()

    def _set_clients_table(self, snap: Dict[int, dict], buses: Dict[int, dict]) -> None:
        client_ids = [client_id for client_id, _st in sorted(snap.items(), key=lambda kv: kv[0])]
        rebuild = client_ids != self._client_row_ids
        if rebuild:
            self._clients.setRowCount(len(client_ids))
            self._client_row_ids = list(client_ids)
            self._vu_widgets.clear()
            self._status_widgets.clear()

        for row, client_id in enumerate(client_ids):
            st = snap.get(int(client_id), {})
            try:
                self._client_uuid_by_id[int(client_id)] = str(st.get("client_uuid") or "")
            except Exception:
                pass

            def _set_item(col: int, text: str) -> None:
                it = self._clients.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem(text)
                    self._clients.setItem(row, col, it)
                else:
                    it.setText(text)

            _set_item(0, str(client_id))
            _set_item(1, str(st.get("name", "")))
            addr = st.get("addr")
            _set_item(2, f"{addr[0]}:{addr[1]}" if addr else "")

            # col 3 — Status indicator
            disconnected = not bool(st.get("control_connected", False))
            if rebuild:
                si = StatusIndicator()
                self._clients.setCellWidget(row, 3, centered_checkbox(si))  # reuse centering helper
                self._status_widgets[int(client_id)] = si
            si = self._status_widgets.get(int(client_id))
            if si is not None:
                si.set_online(not disconnected)

            # col 4 — VU meter
            if rebuild:
                vu = VuMeter()
                self._clients.setCellWidget(row, 4, cell_vu(vu))
                self._vu_widgets[int(client_id)] = vu
            vu = self._vu_widgets.get(int(client_id))
            if vu is not None:
                try:
                    vu.set_level(float(st.get("vu_dbfs", -60.0)))
                except Exception:
                    pass

            # Row styling: dimmed for offline, normal for online
            fg = QtGui.QColor(100, 100, 100) if disconnected else QtGui.QColor(220, 220, 220)
            for col in range(self._clients.columnCount()):
                it = self._clients.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem("")
                    self._clients.setItem(row, col, it)
                it.setForeground(QtGui.QBrush(fg))

    def _on_autostart_changed(self, state: int) -> None:
        self._settings.setValue("autostart", bool(is_checked(state)))

    def _on_start_minimized_changed(self, state: int) -> None:
        self._settings.setValue("start_minimized", bool(is_checked(state)))

    def _maybe_autostart(self) -> None:
        if self._server is None and bool(self._autostart_cb.isChecked()):
            self._start_server()


def run_gui(port: int, minimized: bool = False) -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    win = ServerWindow()
    if minimized or bool(win._start_minimized_cb.isChecked()):
        win.showMinimized()
    else:
        win.show()
    return int(app.exec())
