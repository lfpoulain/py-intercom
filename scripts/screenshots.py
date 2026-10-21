"""Regenerate the README screenshots in docs/images/.

Runs a real server and real clients over loopback; only the sound cards are
simulated (microphones play a tone, headphones consume silence). The user's
presets and Qt settings are never touched: HOME and QSettings point to a
temporary directory.

    .venv\\Scripts\\python scripts\\screenshots.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
SANDBOX = Path(tempfile.mkdtemp(prefix="py-intercom-shots-"))
os.environ["USERPROFILE"] = os.environ["HOME"] = str(SANDBOX)
os.environ["LOCALAPPDATA"] = str(SANDBOX)
sys.path.insert(0, str(ROOT / "src"))

from py_intercom.common.bootstrap import prepare_windows_runtime  # noqa: E402

prepare_windows_runtime(ROOT)

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from py_intercom.client.client import ClientConfig, IntercomClient  # noqa: E402
from py_intercom.client.gui import ClientWindow  # noqa: E402
from py_intercom.common.buses import PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID  # noqa: E402
from py_intercom.common.constants import FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402
from py_intercom.common.identity import client_id_from_uuid  # noqa: E402
from py_intercom.common.theme import apply_theme  # noqa: E402
from py_intercom.server.gui import ServerWindow  # noqa: E402


class _FakeStream:
    """Drives a PortAudio-style callback every 10 ms from a thread."""

    def __init__(self, *, callback, **_kwargs) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.active = False
        self._phase = 0

    def _buffer(self) -> np.ndarray:
        raise NotImplementedError

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            self._callback(self._buffer(), FRAME_SAMPLES, None, None)
            next_tick += FRAME_SAMPLES / SAMPLE_RATE
            time.sleep(max(0.0, next_tick - time.monotonic()))

    def start(self) -> None:
        self.active = True
        self._thread.start()

    def stop(self) -> None:
        self.active = False
        self._stop.set()

    abort = stop

    def close(self) -> None:
        self.stop()


class _FakeMicrophone(_FakeStream):
    def _buffer(self) -> np.ndarray:
        t = (np.arange(FRAME_SAMPLES) + self._phase) / SAMPLE_RATE
        self._phase += FRAME_SAMPLES
        envelope = 0.25 + 0.2 * np.sin(2 * np.pi * 1.3 * t)  # speech-like level movement
        return (envelope * np.sin(2 * np.pi * 220 * t)).astype(np.float32).reshape(-1, 1)


class _FakeHeadphones(_FakeStream):
    def _buffer(self) -> np.ndarray:
        return np.zeros((FRAME_SAMPLES, 1), dtype=np.float32)


_HOSTAPIS = [{"name": "Windows WASAPI"}]
_DEVICES = [
    {"name": "Micro casque (USB Audio)", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "Casque (USB Audio)", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Sortie Plateau (Focusrite USB)", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Sortie VMix (Focusrite USB)", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Programme (VB-Audio Cable)", "max_input_channels": 2, "max_output_channels": 0},
]
for _index, _device in enumerate(_DEVICES):
    _device.update(index=_index, hostapi=0, default_samplerate=48000.0)


def _query_devices(device=None, kind=None):
    return dict(_DEVICES[int(device)]) if device is not None else [dict(d) for d in _DEVICES]


sd.query_hostapis = lambda *args, **kwargs: list(_HOSTAPIS)
sd.query_devices = _query_devices
sd.InputStream = _FakeMicrophone
sd.OutputStream = _FakeHeadphones
sd.check_input_settings = lambda **_kwargs: None
sd.check_output_settings = lambda **_kwargs: None


def pump(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("screenshot scene did not become ready")
        pump(0.05)


def headless_client(name: str) -> IntercomClient:
    client_uuid = str(uuid.uuid4())
    client = IntercomClient(
        client_id=client_id_from_uuid(client_uuid),
        config=ClientConfig(server_ip="127.0.0.1", name=name, client_uuid=client_uuid, input_device=0, output_device=0),
    )
    client.start()
    wait_for(lambda: client.get_stats_snapshot()["control_ready"])
    return client


def save(window: QtWidgets.QWidget, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    window.grab().save(str(path))
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, str(SANDBOX))
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)

    server_window = ServerWindow()
    server_window._server_name.setText("Régie principale")
    server_window._discovery_cb.setChecked(False)
    server_window.show()
    wait_for(lambda: server_window._device_thread is None and server_window._return_input_device.count() > 0)
    server_window._start_server()
    server = server_window._server
    assert server is not None, "server did not start"
    server.create_output(2, PLATEAU_BUS_ID)
    server.create_output(3, VMIX_BUS_ID)

    clients = [headless_client(name) for name in ("Cadreur 1", "Cadreur 2", "Plateau")]
    clients[0].set_ptt_bus(REGIE_BUS_ID, True)
    clients[2].set_ptt_bus(PLATEAU_BUS_ID, True)

    client_window = ClientWindow()
    client_window._name.setText("Réalisation")
    client_window._server_ip.setText("127.0.0.1")
    for bus_id, key in ((REGIE_BUS_ID, "F1"), (PLATEAU_BUS_ID, "F2"), (VMIX_BUS_ID, "F3")):
        client_window._ptt_bus_keys[bus_id].setKeySequence(QtGui.QKeySequence(key))
    client_window.show()
    combos = (client_window._input_device, client_window._output_device)

    def first_device(combo: QtWidgets.QComboBox) -> int | None:
        return next((i for i in range(combo.count()) if combo.itemData(i) is not None), None)

    wait_for(lambda: client_window._device_thread is None and all(first_device(c) is not None for c in combos))
    for combo in combos:
        combo.setCurrentIndex(first_device(combo))
    client_window._connect()
    wait_for(lambda: client_window._client is not None and client_window._client.get_stats_snapshot()["control_ready"])

    pump(3.0)
    save(server_window, "server.png")
    logo = QtGui.QImage(str(ROOT / "src" / "py_intercom" / "img" / "logo.png"))
    logo.scaledToWidth(240, QtCore.Qt.TransformationMode.SmoothTransformation).save(str(OUT / "logo.png"))
    save(client_window, "client.png")

    client_window.close()
    for client in clients:
        client.stop()
    server_window.close()
    pump(0.5)


if __name__ == "__main__":
    main()
