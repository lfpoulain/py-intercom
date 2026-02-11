from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional
import zlib

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .server import IntercomServer
from ..common.devices import list_devices
from ..common.jsonio import atomic_write_json, read_json_file


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


class ServerWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Py-Intercom Server")

        self._server: Optional[IntercomServer] = None

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        self._port = QtWidgets.QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(5000)
        self._show_all_devices = QtWidgets.QCheckBox("Show all devices")
        self._refresh_devices_btn = QtWidgets.QPushButton("Refresh devices")
        self._device_status = QtWidgets.QLabel("")

        self._start_btn = QtWidgets.QPushButton("Start")
        self._stop_btn = QtWidgets.QPushButton("Stop")
        self._stop_btn.setEnabled(False)

        top = QtWidgets.QGridLayout()
        top.addWidget(QtWidgets.QLabel("Port"), 0, 0)
        top.addWidget(self._port, 0, 1)
        top.addWidget(self._show_all_devices, 0, 2)
        top.addWidget(self._refresh_devices_btn, 0, 3)
        top.addWidget(self._start_btn, 0, 4)
        top.addWidget(self._stop_btn, 0, 5)

        top.addWidget(self._device_status, 1, 0, 1, 6)

        self._clients = QtWidgets.QTableWidget(0, 13)
        self._clients.setHorizontalHeaderLabels(
            [
                "Client ID",
                "Name",
                "Mode",
                "PTT",
                "Addr",
                "Age (s)",
                "VU (dBFS)",
                "Ctrl",
                "Muted",
                "Gain (dB)",
                "Regie",
                "Plateau",
                "VMix",
            ]
        )
        self._clients.setColumnHidden(0, True)
        self._clients.setColumnHidden(5, True)
        self._clients.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._clients.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._clients.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)

        self._client_row_ids: list[int] = []
        self._client_uuid_by_id: Dict[int, str] = {}
        self._muted_widgets: Dict[int, QtWidgets.QCheckBox] = {}
        self._gain_widgets: Dict[int, QtWidgets.QSlider] = {}
        self._route_widgets: Dict[tuple[int, int], QtWidgets.QCheckBox] = {}

        self._output_row_ids: list[int] = []
        self._output_device_widgets: Dict[int, QtWidgets.QComboBox] = {}
        self._output_bus_widgets: Dict[int, QtWidgets.QComboBox] = {}
        self._output_devices_cache: list[tuple[str, int]] = []

        self._bus_selector = QtWidgets.QComboBox()
        self._bus_selector.addItem("Regie", 0)
        self._bus_selector.addItem("Plateau", 1)
        self._bus_selector.addItem("VMix", 2)

        self._add_output_device = QtWidgets.QComboBox()
        self._add_output_device.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._add_output_device.setMinimumContentsLength(40)
        self._add_output_btn = QtWidgets.QPushButton("Add output")
        self._remove_output_btn = QtWidgets.QPushButton("Remove selected output")
        self._remove_output_btn.setEnabled(False)

        self._outputs = QtWidgets.QTableWidget(0, 7)
        self._outputs.setHorizontalHeaderLabels(["Output ID", "Device", "Bus", "SR", "Queued (ms)", "Underflows", "VU (dBFS)"])
        out_hdr = self._outputs.horizontalHeader()
        out_hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        out_hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        out_hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        out_hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        out_hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        out_hdr.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        out_hdr.setSectionResizeMode(6, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self._outputs.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._outputs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._outputs.itemSelectionChanged.connect(self._on_output_selection_changed)

        outputs_box = QtWidgets.QGroupBox("Outputs")
        outputs_layout = QtWidgets.QVBoxLayout(outputs_box)
        outputs_top = QtWidgets.QGridLayout()
        outputs_top.addWidget(QtWidgets.QLabel("Device"), 0, 0)
        outputs_top.addWidget(self._add_output_device, 0, 1)
        outputs_top.addWidget(QtWidgets.QLabel("Bus"), 0, 2)
        outputs_top.addWidget(self._bus_selector, 0, 3)
        outputs_top.addWidget(self._add_output_btn, 0, 4)
        outputs_top.addWidget(self._remove_output_btn, 0, 5)
        outputs_layout.addLayout(outputs_top)
        outputs_layout.addWidget(self._outputs)

        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("i")
        self._info_btn.clicked.connect(self._show_geek_info)

        self._forget_client_btn = QtWidgets.QPushButton("Remove client")
        self._forget_client_btn.setEnabled(False)
        self._forget_client_btn.clicked.connect(self._forget_selected_client)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self._forget_client_btn)
        bottom.addStretch(1)
        bottom.addWidget(self._info_btn)

        layout = QtWidgets.QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(self._clients)
        layout.addWidget(outputs_box)
        layout.addLayout(bottom)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(lambda _: self._start_device_refresh())
        self._start_btn.clicked.connect(self._start_server)
        self._stop_btn.clicked.connect(self._stop_server)
        self._add_output_btn.clicked.connect(self._on_add_output)
        self._remove_output_btn.clicked.connect(self._on_remove_output)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._refresh_clients)

        self._device_thread: Optional[QtCore.QThread] = None
        self._device_worker: Optional[_DeviceWorker] = None
        self._device_timeout: Optional[QtCore.QTimer] = None
        self._last_device_error: str = ""
        self._last_device_count: int = 0

        QtCore.QTimer.singleShot(0, self._start_device_refresh)
        QtCore.QTimer.singleShot(0, self._load_preset_preview)

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

        uuid_to_id: Dict[str, int] = {}
        for u in clients_raw.keys():
            try:
                uuid_to_id[str(u)] = int(zlib.crc32(str(u).encode("utf-8")) & 0xFFFFFFFF)
            except Exception:
                continue

        buses = {
            0: {"name": "Regie", "default_all_sources": True, "source_ids": []},
            1: {"name": "Plateau", "default_all_sources": False, "source_ids": []},
            2: {"name": "VMix", "default_all_sources": False, "source_ids": []},
        }
        for bus_id_str, b in buses_raw.items():
            try:
                bid = int(bus_id_str)
            except Exception:
                continue
            if bid not in buses or not isinstance(b, dict):
                continue
            buses[bid]["default_all_sources"] = bool(b.get("default_all_sources", buses[bid]["default_all_sources"]))
            uuids = b.get("source_uuids")
            if isinstance(uuids, list):
                ids = []
                for u in uuids:
                    cid = uuid_to_id.get(str(u))
                    if cid is not None:
                        ids.append(int(cid))
                buses[bid]["source_ids"] = ids

        snap: Dict[int, dict] = {}
        for u, c in clients_raw.items():
            cid = uuid_to_id.get(str(u))
            if cid is None:
                continue
            name = ""
            muted = False
            gain_db = 0.0
            if isinstance(c, dict):
                name = str(c.get("name") or "")
                muted = bool(c.get("muted", False))
                try:
                    gain_db = float(c.get("gain_db", 0.0))
                except Exception:
                    gain_db = 0.0
            snap[int(cid)] = {
                "client_id": int(cid),
                "name": name,
                "mode": "",
                "ptt_key": "",
                "client_uuid": str(u),
                "addr": None,
                "age_s": None,
                "muted": muted,
                "gain_db": float(gain_db),
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

        self._refresh_outputs_table(outs, buses)
        self._set_clients_table(snap, buses)

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
        self._output_row_ids = []
        self._output_device_widgets.clear()
        self._output_bus_widgets.clear()
        self._load_preset_preview()

    def _refresh_outputs_table(self, outs: Dict[int, dict], buses: Dict[int, dict]) -> None:
        output_ids = [oid for oid in sorted(outs.keys())]
        rebuild = output_ids != self._output_row_ids
        if rebuild:
            self._outputs.setRowCount(len(output_ids))
            self._output_row_ids = list(output_ids)
            self._output_device_widgets.clear()
            self._output_bus_widgets.clear()

        bus_names = {int(bid): str(b.get("name", bid)) for bid, b in buses.items()}

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
                dev_cb.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                dev_cb.setMinimumContentsLength(40)
                for label, idx in self._output_devices_cache:
                    dev_cb.addItem(label, idx)
                dev_cb.currentIndexChanged.connect(lambda _i, out_id=int(oid): self._on_output_device_changed(out_id))
                self._outputs.setCellWidget(row, 1, dev_cb)
                self._output_device_widgets[int(oid)] = dev_cb

                bus_cb = QtWidgets.QComboBox()
                for bid in sorted(bus_names.keys()):
                    bus_cb.addItem(str(bus_names[bid]), int(bid))
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
                finally:
                    bus_cb.blockSignals(False)

            _set_item(3, str(st.get("samplerate", "")))
            try:
                _set_item(4, f"{float(st.get('queued_ms', 0.0)):.0f}")
            except Exception:
                _set_item(4, "")
            _set_item(5, str(st.get("underflows", "")))
            try:
                _set_item(6, f"{float(st.get('vu_dbfs', -60.0)):.1f}")
            except Exception:
                _set_item(6, "")

    def _on_output_selection_changed(self) -> None:
        self._remove_output_btn.setEnabled(self._server is not None and len(self._outputs.selectedItems()) > 0)

    def _on_add_output(self) -> None:
        if self._server is None:
            return
        dev = self._add_output_device.currentData()
        if dev is None:
            return
        bus_id = int(self._bus_selector.currentData())
        self._server.create_output(int(dev), int(bus_id))

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

        if self._server is None:
            self._output_row_ids = []
            self._load_preset_preview()

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
        self._muted_widgets.clear()
        self._gain_widgets.clear()
        self._route_widgets.clear()

        self._output_row_ids = []
        self._output_device_widgets.clear()
        self._output_bus_widgets.clear()

        self._server = IntercomServer(
            bind_ip="0.0.0.0",
            port=int(self._port.value()),
            output_device=None,
        )

        try:
            self._server.load_preset()
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
        self._timer.start()

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

            self._client_row_ids = []
            self._muted_widgets.clear()
            self._gain_widgets.clear()
            self._route_widgets.clear()

            self._output_row_ids = []
            self._output_device_widgets.clear()
            self._output_bus_widgets.clear()

            self._load_preset_preview()

    def _refresh_clients(self) -> None:
        if self._server is None:
            return
        snap = self._server.get_clients_snapshot()
        buses = self._server.get_buses_snapshot()
        outs = self._server.get_outputs_snapshot()
        self._set_clients_table(snap, buses)
        self._refresh_outputs_table(outs, buses)

        try:
            items = self._clients.selectedItems()
            have_sel = bool(items)
        except Exception:
            have_sel = False
        self._forget_client_btn.setEnabled(bool(have_sel))

    def _show_geek_info(self) -> None:
        port = int(self._port.value())
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

        if selected is not None and selected in snap:
            st = snap.get(int(selected), {})
            addr = st.get("addr")
            lines.extend(
                [
                    "",
                    f"Selected client_id: {selected}",
                    f"Name: {st.get('name', '')}",
                    f"Mode: {st.get('mode', '')}",
                    f"PTT: {st.get('ptt_key') or '-'}",
                    f"UUID: {st.get('client_uuid') or '-'}",
                    f"Addr: {addr[0]}:{addr[1]}" if addr else "Addr: -",
                    f"Muted: {st.get('muted', False)}",
                    f"Gain: {st.get('gain_db', 0.0)}",
                    f"Ctrl: {st.get('control_connected', False)}",
                ]
            )

        server_preset = None
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
            self._muted_widgets.clear()
            self._gain_widgets.clear()
            self._route_widgets.clear()
        else:
            # If Qt has deleted widgets (e.g. after setRowCount(0) on stop/start), force a rebuild.
            for row, client_id in enumerate(client_ids):
                w = self._clients.cellWidget(row, 8)
                if w is None or int(client_id) not in self._muted_widgets:
                    rebuild = True
                    break
            if rebuild:
                self._clients.setRowCount(len(client_ids))
                self._client_row_ids = list(client_ids)
                self._muted_widgets.clear()
                self._gain_widgets.clear()
                self._route_widgets.clear()

        def route_checked(bus_id: int, client_id: int) -> bool:
            b = buses.get(int(bus_id))
            if not b:
                return False
            src_ids = set(b.get("source_ids") or [])
            if bool(b.get("default_all_sources")) and len(src_ids) == 0:
                return True
            return int(client_id) in src_ids

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
            _set_item(2, str(st.get("mode", "")))
            _set_item(3, str(st.get("ptt_key") or ""))
            addr = st.get("addr")
            _set_item(4, f"{addr[0]}:{addr[1]}" if addr else "")
            _set_item(6, f"{st.get('vu_dbfs', -60.0):.1f}")

            ctrl_ok = bool(st.get("control_connected", False))
            ctrl_age = st.get("control_age_s")
            if ctrl_ok and ctrl_age is not None:
                _set_item(7, f"OK {float(ctrl_age):.1f}s")
            elif ctrl_ok:
                _set_item(7, "OK")
            else:
                _set_item(7, "-")

            disconnected = not bool(st.get("control_connected", False))
            bg = QtGui.QColor(255, 220, 220) if disconnected else QtGui.QColor(255, 255, 255)
            fg = QtGui.QColor(80, 0, 0) if disconnected else QtGui.QColor(0, 0, 0)
            for col in range(self._clients.columnCount()):
                it = self._clients.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem("")
                    self._clients.setItem(row, col, it)
                it.setBackground(QtGui.QBrush(bg))
                it.setForeground(QtGui.QBrush(fg))

            if rebuild:
                muted = QtWidgets.QCheckBox()
                muted.stateChanged.connect(lambda state, cid=client_id: self._on_muted_changed(cid, state))
                self._clients.setCellWidget(row, 8, muted)
                self._muted_widgets[int(client_id)] = muted
            muted = self._muted_widgets.get(int(client_id))
            if muted is not None:
                try:
                    muted.blockSignals(True)
                    try:
                        muted.setChecked(bool(st.get("muted", False)))
                    finally:
                        muted.blockSignals(False)
                except RuntimeError:
                    # widget was deleted by Qt; rebuild on next refresh
                    self._client_row_ids = []
                    return
                muted.setStyleSheet("background-color: rgb(255, 220, 220);" if disconnected else "")

            if rebuild:
                gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
                gain.setMinimum(-60)
                gain.setMaximum(12)
                gain.valueChanged.connect(lambda val, cid=client_id: self._on_gain_changed(cid, val))
                self._clients.setCellWidget(row, 9, gain)
                self._gain_widgets[int(client_id)] = gain
            gain = self._gain_widgets.get(int(client_id))
            if gain is not None and not gain.isSliderDown():
                try:
                    gain.blockSignals(True)
                    try:
                        gain.setValue(int(round(float(st.get("gain_db", 0.0)))))
                    finally:
                        gain.blockSignals(False)
                except RuntimeError:
                    self._client_row_ids = []
                    return
                gain.setStyleSheet("background-color: rgb(255, 220, 220);" if disconnected else "")

            for col, bus_id in enumerate((0, 1, 2), start=10):
                key = (int(client_id), int(bus_id))
                if rebuild:
                    cb = QtWidgets.QCheckBox()
                    cb.stateChanged.connect(
                        lambda state, cid=client_id, bid=bus_id: self._on_route_changed(cid, bid, state)
                    )
                    self._clients.setCellWidget(row, col, cb)
                    self._route_widgets[key] = cb
                cb = self._route_widgets.get(key)
                if cb is not None:
                    try:
                        cb.blockSignals(True)
                        try:
                            cb.setChecked(route_checked(bus_id, client_id))
                        finally:
                            cb.blockSignals(False)
                    except RuntimeError:
                        self._client_row_ids = []
                        return
                    cb.setStyleSheet("background-color: rgb(255, 220, 220);" if disconnected else "")

    def _on_route_changed(self, client_id: int, bus_id: int, state: int) -> None:
        if self._server is None:
            return
        self._server.set_route(int(client_id), int(bus_id), _is_checked(state))

    def _on_muted_changed(self, client_id: int, state: int) -> None:
        if self._server is None:
            return
        self._server.set_client_muted(client_id, _is_checked(state))

    def _on_gain_changed(self, client_id: int, gain_db: int) -> None:
        if self._server is None:
            return
        self._server.set_client_gain_db(client_id, float(gain_db))


def run_gui(port: int) -> int:
    app = QtWidgets.QApplication(sys.argv)
    win = ServerWindow()
    win._port.setValue(int(port))

    win.show()
    return int(app.exec())
