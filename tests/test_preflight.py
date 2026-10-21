from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import socket
import time

import numpy as np
import pytest
from PySide6 import QtWidgets

from py_intercom.client.preflight import (
    TONE_AMPLITUDE,
    PreflightDialog,
    Verdict,
    check_network,
    check_shortcuts,
    microphone_verdict,
    tone_frame,
)
from py_intercom.common.buses import PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID
from py_intercom.common.constants import FRAME_SAMPLES


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def listening_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield int(server.getsockname()[1])


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_network_check_reaches_a_listening_server(listening_port) -> None:
    assert check_network("127.0.0.1", listening_port).verdict is Verdict.OK


def test_network_check_reports_an_unreachable_server() -> None:
    result = check_network("127.0.0.1", _closed_port(), timeout_s=0.5)

    assert result.verdict is Verdict.ERROR
    assert "pare-feu" in result.detail


def test_network_check_requires_an_address() -> None:
    assert check_network("  ", 5001).verdict is Verdict.ERROR


def test_shortcuts_check() -> None:
    assert check_shortcuts({REGIE_BUS_ID: "F1", PLATEAU_BUS_ID: "F2"}).verdict is Verdict.OK
    assert check_shortcuts({REGIE_BUS_ID: "", PLATEAU_BUS_ID: ""}).verdict is Verdict.WARNING

    clash = check_shortcuts({REGIE_BUS_ID: "F1", VMIX_BUS_ID: "f1"})
    assert clash.verdict is Verdict.ERROR
    assert "F1" in clash.detail


def test_microphone_verdict() -> None:
    assert microphone_verdict(-20.0).verdict is Verdict.OK
    assert microphone_verdict(-0.1).verdict is Verdict.WARNING
    assert microphone_verdict(-70.0).verdict is Verdict.ERROR


def test_tone_beeps_then_pauses_at_a_safe_level() -> None:
    first_beep = np.concatenate([tone_frame(i * FRAME_SAMPLES) for i in range(30)])
    first_pause = np.concatenate([tone_frame(i * FRAME_SAMPLES) for i in range(30, 60)])

    assert 0.9 * TONE_AMPLITUDE < float(np.abs(first_beep).max()) <= TONE_AMPLITUDE
    assert float(np.abs(first_pause).max()) == 0.0


class _FakeMicrophone:
    def __init__(self, device: int, *, peak_dbfs: float) -> None:
        self.device = device
        self.level_dbfs = peak_dbfs
        self.peak_dbfs = peak_dbfs
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeTone:
    instances: list[_FakeTone] = []

    def __init__(self, device: int) -> None:
        self.device = device
        self.closed = False
        _FakeTone.instances.append(self)

    def close(self) -> None:
        self.closed = True


def _run_dialog(qt_app, monkeypatch, *, port: int, mic_peak: float, heard: bool) -> PreflightDialog:
    monkeypatch.setattr("py_intercom.client.preflight.MIC_LISTEN_S", 0.1)
    microphones: list[_FakeMicrophone] = []

    def microphone_factory(device: int) -> _FakeMicrophone:
        microphones.append(_FakeMicrophone(device, peak_dbfs=mic_peak))
        return microphones[-1]

    dialog = PreflightDialog(
        None,
        server_host="127.0.0.1",
        control_port=port,
        input_device=3,
        output_device=4,
        shortcuts={REGIE_BUS_ID: "F1"},
        microphone_factory=microphone_factory,
        tone_factory=_FakeTone,
    )
    dialog.start()
    deadline = time.monotonic() + 5.0
    while not dialog._heard_btn.isVisibleTo(dialog) and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert microphones and microphones[0].device == 3 and microphones[0].closed
    (dialog._heard_btn if heard else dialog._not_heard_btn).click()
    assert _FakeTone.instances[-1].device == 4 and _FakeTone.instances[-1].closed
    return dialog


def test_preflight_passes_when_everything_works(qt_app, monkeypatch, listening_port) -> None:
    dialog = _run_dialog(qt_app, monkeypatch, port=listening_port, mic_peak=-20.0, heard=True)

    assert [row.result.verdict for row in dialog.rows] == [Verdict.OK] * 4
    assert "Tout est prêt" in dialog.summary.text()
    assert dialog.start_btn.isEnabled()


def test_preflight_lists_what_to_fix(qt_app, monkeypatch) -> None:
    dialog = _run_dialog(qt_app, monkeypatch, port=_closed_port(), mic_peak=-90.0, heard=False)

    verdicts = [row.result.verdict for row in dialog.rows]
    assert verdicts == [Verdict.ERROR, Verdict.OK, Verdict.ERROR, Verdict.ERROR]
    assert "3 point(s) à corriger" in dialog.summary.text()
