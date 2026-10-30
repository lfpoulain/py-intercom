from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import socket
import time

import numpy as np
import pytest
from PySide6 import QtCore, QtTest, QtWidgets

import py_intercom.client.preflight as preflight_mod
from py_intercom.client.preflight import (
    TONE_AMPLITUDE,
    TONE_CYCLE_SAMPLES,
    PreflightDialog,
    TonePlayer,
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


def test_shortcuts_check_rejects_a_chord_contained_in_another() -> None:
    nested = check_shortcuts({REGIE_BUS_ID: "A", PLATEAU_BUS_ID: "Ctrl+A"})
    assert nested.verdict is Verdict.ERROR
    assert "Ctrl+A" in nested.detail

    assert check_shortcuts({REGIE_BUS_ID: "Ctrl+A", PLATEAU_BUS_ID: "Shift+A"}).verdict is Verdict.OK


def test_shortcuts_check_reports_an_unparsable_key_on_its_own() -> None:
    result = check_shortcuts({REGIE_BUS_ID: "F1, F2", PLATEAU_BUS_ID: "F3"})

    assert result.verdict is Verdict.ERROR
    assert "non pris en charge" in result.detail and "= F1, F2" in result.detail
    assert "plusieurs bus" not in result.detail


def test_tone_callback_loops_a_precomputed_cycle(monkeypatch) -> None:
    streams: list[object] = []

    class FakeStream:
        def __init__(self, **kwargs) -> None:
            self.callback = kwargs["callback"]
            streams.append(self)

        def start(self) -> None:
            return

    monkeypatch.setattr(preflight_mod.sd, "query_devices", lambda _device: {})
    monkeypatch.setattr(preflight_mod.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(preflight_mod, "shared_stream_extra_settings", lambda _info: None)
    expected = [tone_frame(i * FRAME_SAMPLES) for i in range(TONE_CYCLE_SAMPLES // FRAME_SAMPLES)]
    TonePlayer(4)

    def no_synthesis(_phase: int):
        raise AssertionError("the callback must not synthesize the tone")

    monkeypatch.setattr(preflight_mod, "tone_frame", no_synthesis)
    out = np.zeros((FRAME_SAMPLES, 1), dtype=np.float32)
    for block in [*expected, expected[0]]:
        streams[0].callback(out, FRAME_SAMPLES, None, None)
        np.testing.assert_array_equal(out[:, 0], block)


def _wait_for(qt_app, condition) -> None:
    deadline = time.monotonic() + 5.0
    while not condition() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert condition()


def _dialog(microphones: list[_FakeMicrophone]) -> PreflightDialog:
    def microphone_factory(device: int) -> _FakeMicrophone:
        microphones.append(_FakeMicrophone(device, peak_dbfs=-20.0))
        return microphones[-1]

    return PreflightDialog(
        None,
        server_host="127.0.0.1",
        control_port=_closed_port(),
        input_device=3,
        output_device=4,
        shortcuts={REGIE_BUS_ID: "F1"},
        microphone_factory=microphone_factory,
        tone_factory=_FakeTone,
    )


def test_escape_during_the_microphone_check_releases_it_and_stops(qt_app) -> None:
    microphones: list[_FakeMicrophone] = []
    tones_before = len(_FakeTone.instances)
    dialog = _dialog(microphones)
    dialog.show()
    dialog.start()
    _wait_for(qt_app, lambda: bool(microphones))

    QtTest.QTest.keyClick(dialog, QtCore.Qt.Key.Key_Escape)

    assert not dialog.isVisible()
    assert microphones[0].closed and dialog._microphone is None
    assert not dialog._timer.isActive()
    assert len(_FakeTone.instances) == tones_before  # the beep can no longer start later


def test_closing_during_the_beep_stops_it(qt_app, monkeypatch) -> None:
    monkeypatch.setattr("py_intercom.client.preflight.MIC_LISTEN_S", 0.1)
    dialog = _dialog([])
    dialog.show()
    dialog.start()
    _wait_for(qt_app, lambda: dialog._tone is not None)
    tone = dialog._tone

    dialog.reject()

    assert tone.closed and dialog._tone is None


def test_shortcuts_check_catches_keys_that_share_a_physical_key(monkeypatch) -> None:
    # "=" and "+" on a layout where both sit on the same key (vk:187).
    groups = {"=": [frozenset({"char:=", "vk:187"})], "+": [frozenset({"char:+", "vk:187", "vk:107"})]}
    monkeypatch.setattr(preflight_mod, "_parse_qt_shortcut", lambda key: groups[key])

    result = check_shortcuts({REGIE_BUS_ID: "=", PLATEAU_BUS_ID: "+"})

    assert result.verdict is Verdict.ERROR
    assert "plusieurs bus" in result.detail
