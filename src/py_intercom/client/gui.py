from __future__ import annotations

import sys
import socket
import uuid
import zlib
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..common.devices import list_devices
from ..common.jsonio import atomic_write_json, read_json_file
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


class ClientWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Py-Intercom Client")

        self._client: Optional[IntercomClient] = None

        self._preset: dict = {}
        self._preset = self._load_preset()

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

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
        self._mode = QtWidgets.QComboBox()
        self._mode.addItem("Always-on", "always_on")
        self._mode.addItem("PTT", "ptt")
        self._ptt_key = QtWidgets.QLineEdit(str(self._preset_get("ptt_key", "")))
        saved_mode = str(self._preset_get("mode", "always_on"))
        i = self._mode.findData(saved_mode)
        if i >= 0:
            self._mode.setCurrentIndex(i)

        self._input_device = QtWidgets.QComboBox()
        self._output_device = QtWidgets.QComboBox()
        self._input_device.currentIndexChanged.connect(self._on_input_device_changed)
        self._output_device.currentIndexChanged.connect(self._on_output_device_changed)
        self._show_all_devices = QtWidgets.QCheckBox("Show all devices")
        self._refresh_devices_btn = QtWidgets.QPushButton("Refresh devices")
        self._device_status = QtWidgets.QLabel("")

        self._connect_btn = QtWidgets.QPushButton("Connect")
        self._disconnect_btn = QtWidgets.QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)

        self._mute = QtWidgets.QCheckBox("Mute mic")
        self._mute.setEnabled(False)

        self._sidetone = QtWidgets.QCheckBox("Sidetone (hear self locally)")
        self._sidetone.setEnabled(False)

        self._sidetone_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._sidetone_gain.setMinimum(-60)
        self._sidetone_gain.setMaximum(0)
        self._sidetone_gain.setValue(-12)
        self._sidetone_gain.setEnabled(False)

        self._mic_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._mic_gain.setMinimum(-60)
        self._mic_gain.setMaximum(12)
        self._mic_gain.setValue(0)
        self._mic_gain.setEnabled(False)

        self._hp_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._hp_gain.setMinimum(-60)
        self._hp_gain.setMaximum(12)
        self._hp_gain.setValue(0)
        self._hp_gain.setEnabled(False)

        self._in_vu = QtWidgets.QProgressBar()
        self._in_vu.setRange(0, 100)
        self._in_vu.setValue(0)

        self._out_vu = QtWidgets.QProgressBar()
        self._out_vu.setRange(0, 100)
        self._out_vu.setValue(0)

        form = QtWidgets.QGridLayout()
        form.addWidget(QtWidgets.QLabel("Server IP"), 0, 0)
        form.addWidget(self._server_ip, 0, 1)
        form.addWidget(QtWidgets.QLabel("Port"), 0, 2)
        form.addWidget(self._server_port, 0, 3)
        form.addWidget(self._client_id_label, 1, 0)
        form.addWidget(self._client_id, 1, 1, 1, 3)

        form.addWidget(QtWidgets.QLabel("Name"), 2, 0)
        form.addWidget(self._name, 2, 1)
        form.addWidget(QtWidgets.QLabel("Mode"), 2, 2)
        form.addWidget(self._mode, 2, 3)
        form.addWidget(QtWidgets.QLabel("PTT key"), 3, 0)
        form.addWidget(self._ptt_key, 3, 1, 1, 3)

        form.addWidget(QtWidgets.QLabel("Microphone"), 4, 0)
        form.addWidget(self._input_device, 4, 1, 1, 3)
        form.addWidget(QtWidgets.QLabel("Headphones"), 5, 0)
        form.addWidget(self._output_device, 5, 1, 1, 3)

        form.addWidget(self._show_all_devices, 6, 0)
        form.addWidget(self._refresh_devices_btn, 6, 1)
        form.addWidget(self._connect_btn, 6, 2)
        form.addWidget(self._disconnect_btn, 6, 3)

        form.addWidget(self._device_status, 10, 0, 1, 4)

        form.addWidget(self._mute, 7, 0)
        form.addWidget(self._sidetone, 7, 1, 1, 3)
        form.addWidget(QtWidgets.QLabel("Mic gain (dB)"), 8, 0)
        form.addWidget(self._mic_gain, 8, 1, 1, 3)
        form.addWidget(QtWidgets.QLabel("Headphones gain (dB)"), 9, 0)
        form.addWidget(self._hp_gain, 9, 1, 1, 3)
        form.addWidget(QtWidgets.QLabel("Sidetone gain (dB)"), 11, 0)
        form.addWidget(self._sidetone_gain, 11, 1, 1, 3)

        meters = QtWidgets.QGridLayout()
        meters.addWidget(QtWidgets.QLabel("Input VU"), 0, 0)
        meters.addWidget(self._in_vu, 0, 1)
        meters.addWidget(QtWidgets.QLabel("Output VU"), 1, 0)
        meters.addWidget(self._out_vu, 1, 1)

        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("i")
        self._info_btn.clicked.connect(self._show_geek_info)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self._info_btn)

        layout = QtWidgets.QVBoxLayout(central)
        layout.addLayout(form)
        layout.addLayout(meters)
        layout.addLayout(bottom)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(lambda _: self._start_device_refresh())
        self._connect_btn.clicked.connect(self._connect)
        self._disconnect_btn.clicked.connect(self._disconnect)

        self._mute.stateChanged.connect(self._on_mute_changed)
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

        QtCore.QTimer.singleShot(0, self._start_device_refresh)

    def _reset_total_config(self) -> None:
        if self._client is not None:
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
        stable_id = int(zlib.crc32(client_uuid.encode("utf-8")) & 0xFFFFFFFF)

        self._preset_save()

        self._server_ip.setText("")
        self._server_port.setValue(5000)
        self._name.setText("")

        i = self._mode.findData("always_on")
        if i >= 0:
            self._mode.setCurrentIndex(i)
        self._ptt_key.setText("")

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

    def _preset_atomic_write(self, data: dict) -> None:
        p = self._preset_path()
        atomic_write_json(p, data)

    def _preset_save(self) -> None:
        try:
            self._preset_atomic_write(self._preset)
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
        if self._client is not None:
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
        mode = str(self._mode.currentData() or "always_on")
        ptt_key = self._ptt_key.text().strip()

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
                f"Mode: {mode}",
                f"PTT key: {ptt_key or '-'}",
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
        self._disconnect()
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
        if self._client is not None:
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
        mode = str(self._mode.currentData() or "always_on")
        ptt_key = self._ptt_key.text().strip()
        if not ptt_key:
            ptt_key = None

        self._preset_set("server_ip", server_ip)
        self._preset_set("server_port", int(self._server_port.value()))
        self._preset_set("name", name)
        self._preset_set("mode", mode)
        self._preset_set("ptt_key", ptt_key or "")

        client_uuid = str(self._preset_get("client_uuid", ""))

        cfg = ClientConfig(
            server_ip=server_ip,
            server_port=int(server_port),
            control_port=None,
            name=name,
            mode=mode,
            ptt_key=ptt_key,
            client_uuid=client_uuid,
            input_device=int(input_device),
            output_device=int(output_device),
            input_gain_db=float(self._mic_gain.value()),
            output_gain_db=float(self._hp_gain.value()),
            muted=self._mute.isChecked(),
            sidetone_enabled=self._sidetone.isChecked(),
            sidetone_gain_db=float(self._sidetone_gain.value()),
        )

        try:
            self._client = IntercomClient(client_id=client_id, config=cfg)
            self._client.start()
        except Exception as e:
            self._client = None
            QtWidgets.QMessageBox.critical(self, "Connect failed", str(e))
            return

        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._mute.setEnabled(True)
        self._mic_gain.setEnabled(True)
        self._hp_gain.setEnabled(True)
        self._sidetone.setEnabled(True)
        self._sidetone_gain.setEnabled(True)
        self._timer.start()

    def _disconnect(self) -> None:
        if self._client is None:
            return
        self._kick_notified = False
        self._server_lost_notified = False
        self._was_control_connected = False
        try:
            self._timer.stop()
            self._client.stop()
        finally:
            self._client = None
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setEnabled(False)
            self._mute.setEnabled(False)
            self._mic_gain.setEnabled(False)
            self._hp_gain.setEnabled(False)
            self._sidetone.setEnabled(False)
            self._sidetone_gain.setEnabled(False)
            self._in_vu.setValue(0)
            self._out_vu.setValue(0)

    def _on_mute_changed(self, state: int) -> None:
        if self._client is None:
            return
        self._client.set_muted(_is_checked(state))

    def _on_mic_gain_changed(self, value: int) -> None:
        if self._client is None:
            return
        self._client.set_input_gain_db(float(value))

    def _on_hp_gain_changed(self, value: int) -> None:
        if self._client is None:
            return
        self._client.set_output_gain_db(float(value))

    def _on_sidetone_changed(self, state: int) -> None:
        if self._client is None:
            return
        self._client.set_sidetone_enabled(_is_checked(state))

    def _on_sidetone_gain_changed(self, value: int) -> None:
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

        self._in_vu.setValue(_db_to_progress(st.get("in_vu_dbfs", -60.0)))
        self._out_vu.setValue(_db_to_progress(st.get("out_vu_dbfs", -60.0)))

        muted = bool(st.get("muted", False))
        try:
            self._mute.blockSignals(True)
            try:
                self._mute.setChecked(muted)
            finally:
                self._mute.blockSignals(False)
        except Exception:
            pass


def run_gui(
    server_ip: str = "",
    server_port: int = 5000,
    input_device: int = -1,
    output_device: int = -1,
) -> int:
    app = QtWidgets.QApplication(sys.argv)
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
