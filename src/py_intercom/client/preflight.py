"""Operator preflight: check network, shortcuts, microphone and headphones.

Run before connecting. Each check is independent and reports a plain
verdict the operator can act on. Audio probes open the selected devices
with the engine's exact format (48 kHz mono, 10 ms blocks), so a device that
passes here will also open when connecting.
"""

from __future__ import annotations

import math
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

import numpy as np
import sounddevice as sd
from PySide6 import QtCore, QtWidgets

from ..common.audio_errors import friendly_audio_open_error, shared_stream_extra_settings
from ..common.buses import BUS_BY_ID
from ..common.constants import FRAME_SAMPLES, SAMPLE_RATE
from ..common.logging import best_effort
from ..common.theme import StatusIndicator, VuMeter

MIC_LISTEN_S = 4.0
MIC_MIN_PEAK_DBFS = -45.0
NETWORK_TIMEOUT_S = 1.5
TONE_FREQUENCY_HZ = 1000.0
TONE_AMPLITUDE = 0.125  # -18 dBFS


class Verdict(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    verdict: Verdict
    detail: str


# --- Pure checks -------------------------------------------------------------


def check_network(host: str, port: int, *, timeout_s: float = NETWORK_TIMEOUT_S) -> CheckResult:
    """Open the control TCP port: proves routing, firewall and a running server."""
    host = str(host or "").strip()
    if not host:
        return CheckResult(Verdict.ERROR, "Aucune adresse de serveur saisie.")
    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            elapsed_ms = (time.perf_counter() - started) * 1000.0
    except OSError as exc:
        return CheckResult(
            Verdict.ERROR,
            f"Serveur injoignable sur {host}:{port} ({exc.__class__.__name__}). "
            "Vérifiez qu'il est démarré et que le pare-feu autorise TCP 5001 et UDP 5000.",
        )
    if elapsed_ms > 50.0:
        return CheckResult(Verdict.WARNING, f"Serveur joignable, mais lent à répondre ({elapsed_ms:.0f} ms). Wi-Fi ?")
    return CheckResult(Verdict.OK, f"Serveur joignable en {elapsed_ms:.0f} ms.")


def check_shortcuts(shortcuts: Mapping[int, str]) -> CheckResult:
    """Every bus may have a key; two buses must never share one."""
    assigned = {int(bus_id): str(key).strip() for bus_id, key in shortcuts.items() if str(key or "").strip()}
    if not assigned:
        return CheckResult(Verdict.WARNING, "Aucun raccourci PTT : seuls les boutons à l'écran permettront de parler.")
    by_key: dict[str, list[str]] = {}
    for bus_id, key in assigned.items():
        by_key.setdefault(key.lower(), []).append(BUS_BY_ID[bus_id].name if bus_id in BUS_BY_ID else str(bus_id))
    clashes = [f"{key.upper()} ({', '.join(buses)})" for key, buses in by_key.items() if len(buses) > 1]
    if clashes:
        return CheckResult(Verdict.ERROR, "Même touche sur plusieurs bus : " + "; ".join(clashes) + ".")
    summary = ", ".join(
        f"{BUS_BY_ID[bus_id].name if bus_id in BUS_BY_ID else bus_id} = {key}" for bus_id, key in sorted(assigned.items())
    )
    return CheckResult(Verdict.OK, summary)


def microphone_verdict(peak_dbfs: float) -> CheckResult:
    if peak_dbfs >= -1.0:
        return CheckResult(Verdict.WARNING, f"Le micro sature ({peak_dbfs:.0f} dBFS) : baissez le gain micro.")
    if peak_dbfs >= MIC_MIN_PEAK_DBFS:
        return CheckResult(Verdict.OK, f"Voix captée (crête {peak_dbfs:.0f} dBFS).")
    return CheckResult(
        Verdict.ERROR,
        "Aucun son capté. Vérifiez le micro choisi, qu'il n'est pas coupé et que Windows l'autorise.",
    )


def tone_frame(phase: int) -> np.ndarray:
    """One 10 ms frame of a beep pattern: 300 ms tone, 300 ms silence."""
    t = (np.arange(FRAME_SAMPLES) + phase) / SAMPLE_RATE
    gate = (np.floor(t / 0.3) % 2 == 0).astype(np.float32)
    return (TONE_AMPLITUDE * gate * np.sin(2.0 * np.pi * TONE_FREQUENCY_HZ * t)).astype(np.float32)


# --- Audio probes ------------------------------------------------------------


def _open_error(exc: Exception, *, kind: str, device: int) -> RuntimeError:
    dev_info = None
    with best_effort("query preflight device"):
        dev_info = sd.query_devices(int(device))
    return RuntimeError(
        friendly_audio_open_error(exc, kind=kind, dev_info=dev_info, device=device, sr=SAMPLE_RATE, ch=1)
    )


class MicrophoneProbe:
    """Records the peak level of the selected microphone."""

    def __init__(self, device: int) -> None:
        self._scratch = np.zeros(FRAME_SAMPLES, dtype=np.float32)
        self._peak = 0.0
        self._last = 0.0
        try:
            dev_info = sd.query_devices(int(device))
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=FRAME_SAMPLES,
                device=int(device),
                extra_settings=shared_stream_extra_settings(dev_info),
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            raise _open_error(exc, kind="input", device=device) from exc

    def _callback(self, indata, frames, time_info, status) -> None:
        if int(frames) != FRAME_SAMPLES:
            return
        np.abs(indata[:, 0], out=self._scratch)
        self._last = float(self._scratch.max())
        self._peak = max(self._peak, self._last)

    @staticmethod
    def _dbfs(value: float) -> float:
        return 20.0 * math.log10(value) if value > 1e-6 else -120.0

    @property
    def level_dbfs(self) -> float:
        return self._dbfs(self._last)

    @property
    def peak_dbfs(self) -> float:
        return self._dbfs(self._peak)

    def close(self) -> None:
        with best_effort("close preflight microphone"):
            self._stream.stop()
            self._stream.close()


class TonePlayer:
    """Plays the beep pattern on the selected headphones."""

    def __init__(self, device: int) -> None:
        self._phase = 0
        try:
            dev_info = sd.query_devices(int(device))
            self._stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=FRAME_SAMPLES,
                device=int(device),
                extra_settings=shared_stream_extra_settings(dev_info),
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            raise _open_error(exc, kind="output", device=device) from exc

    def _callback(self, outdata, frames, time_info, status) -> None:
        if int(frames) != FRAME_SAMPLES:
            outdata.fill(0.0)
            return
        outdata[:, 0] = tone_frame(self._phase)
        self._phase += FRAME_SAMPLES

    def close(self) -> None:
        with best_effort("close preflight tone"):
            self._stream.stop()
            self._stream.close()


# --- Dialog ------------------------------------------------------------------

_INDICATOR_STATE = {
    Verdict.PENDING: "unknown",
    Verdict.RUNNING: "connecting",
    Verdict.OK: "healthy",
    Verdict.WARNING: "degraded",
    Verdict.ERROR: "error",
}


class _CheckRow(QtWidgets.QWidget):
    def __init__(self, title: str, waiting_text: str) -> None:
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(12)
        self.indicator = StatusIndicator()
        lay.addWidget(self.indicator, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        title_label = QtWidgets.QLabel(title)
        title_label.setStyleSheet("font-weight: 600;")
        self.detail = QtWidgets.QLabel(waiting_text)
        self.detail.setWordWrap(True)
        self.detail.setProperty("role", "muted")
        for label in (title_label, self.detail):
            label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        text.addWidget(title_label)
        text.addWidget(self.detail)
        # Optional live controls (VU-meter, answer buttons), left-aligned.
        self.extra = QtWidgets.QHBoxLayout()
        self.extra.addStretch(1)
        text.addLayout(self.extra)
        lay.addLayout(text, 1)
        self.waiting_text = waiting_text
        self.reset()

    def reset(self) -> None:
        self.result: CheckResult | None = None
        self.set(CheckResult(Verdict.PENDING, self.waiting_text))

    def set(self, result: CheckResult) -> None:
        if result.verdict not in (Verdict.PENDING, Verdict.RUNNING):
            self.result = result
        self.indicator.set_state(_INDICATOR_STATE[result.verdict], description=result.detail)
        self.detail.setText(result.detail)


class PreflightDialog(QtWidgets.QDialog):
    """Runs the four checks in sequence: network, shortcuts, microphone, headphones."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        *,
        server_host: str,
        control_port: int,
        input_device: int,
        output_device: int,
        shortcuts: Mapping[int, str],
        microphone_factory: Callable[[int], MicrophoneProbe] = MicrophoneProbe,
        tone_factory: Callable[[int], TonePlayer] = TonePlayer,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Préflight — test avant le live")
        self.setMinimumWidth(560)
        self._server_host = server_host
        self._control_port = int(control_port)
        self._input_device = int(input_device)
        self._output_device = int(output_device)
        self._shortcuts = dict(shortcuts)
        self._microphone_factory = microphone_factory
        self._tone_factory = tone_factory
        self._microphone: MicrophoneProbe | None = None
        self._tone: TonePlayer | None = None
        self._network_result: CheckResult | None = None
        self._mic_deadline = 0.0

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        intro = QtWidgets.QLabel("Vérifie la chaîne complète avant de vous connecter. Durée : environ 15 secondes.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.network_row = _CheckRow("Réseau", "Connexion au serveur.")
        self.shortcuts_row = _CheckRow("Raccourcis PTT", "Touches attribuées aux bus.")
        self.microphone_row = _CheckRow("Microphone", "Parlez normalement pendant 4 secondes.")
        self.headphones_row = _CheckRow("Casque", "Un bip va être joué dans le casque.")
        self.rows = (self.network_row, self.shortcuts_row, self.microphone_row, self.headphones_row)
        for row in self.rows:
            lay.addWidget(row)

        self._mic_meter = VuMeter()
        self._mic_meter.setMinimumWidth(220)
        self._mic_meter.setVisible(False)
        self.microphone_row.extra.insertWidget(0, self._mic_meter)

        self._heard_btn = QtWidgets.QPushButton("J'entends le bip")
        self._heard_btn.setProperty("class", "success")
        self._not_heard_btn = QtWidgets.QPushButton("Je n'entends rien")
        self._not_heard_btn.setProperty("class", "danger")
        for index, button in enumerate((self._heard_btn, self._not_heard_btn)):
            button.setVisible(False)
            self.headphones_row.extra.insertWidget(index, button)
        self._heard_btn.clicked.connect(lambda: self._finish_headphones(True))
        self._not_heard_btn.clicked.connect(lambda: self._finish_headphones(False))

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-weight: 600; padding-top: 6px;")
        lay.addWidget(self.summary)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.start_btn = QtWidgets.QPushButton("Lancer le test")
        self.start_btn.setProperty("class", "success")
        self.close_btn = QtWidgets.QPushButton("Fermer")
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.close_btn)
        lay.addLayout(buttons)
        self.start_btn.clicked.connect(self.start)
        self.close_btn.clicked.connect(self.close)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._step = "idle"

    # Sequence: network (background thread) -> shortcuts -> microphone -> headphones.

    def start(self) -> None:
        self._release_audio()
        self.summary.setText("")
        self.start_btn.setEnabled(False)
        for row in self.rows:
            row.reset()
        self.network_row.set(CheckResult(Verdict.RUNNING, "Connexion au serveur…"))
        self._network_result = None
        threading.Thread(target=self._run_network_check, name="preflight-network", daemon=True).start()
        self._step = "network"
        self._timer.start()

    def _run_network_check(self) -> None:
        self._network_result = check_network(self._server_host, self._control_port)

    def _tick(self) -> None:
        if self._step == "network" and self._network_result is not None:
            self.network_row.set(self._network_result)
            self.shortcuts_row.set(check_shortcuts(self._shortcuts))
            self._start_microphone()
        elif self._step == "microphone" and self._microphone is not None:
            self._mic_meter.set_level(self._microphone.level_dbfs)
            if time.monotonic() >= self._mic_deadline:
                peak = self._microphone.peak_dbfs
                self._release_audio()
                self._mic_meter.setVisible(False)
                self.microphone_row.set(microphone_verdict(peak))
                self._start_headphones()

    def _start_microphone(self) -> None:
        self._step = "microphone"
        try:
            self._microphone = self._microphone_factory(self._input_device)
        except Exception as exc:
            self.microphone_row.set(CheckResult(Verdict.ERROR, str(exc)))
            self._start_headphones()
            return
        self._mic_deadline = time.monotonic() + MIC_LISTEN_S
        self._mic_meter.setVisible(True)
        self.microphone_row.set(CheckResult(Verdict.RUNNING, "Parlez maintenant…"))

    def _start_headphones(self) -> None:
        self._step = "headphones"
        self._timer.stop()
        try:
            self._tone = self._tone_factory(self._output_device)
        except Exception as exc:
            self.headphones_row.set(CheckResult(Verdict.ERROR, str(exc)))
            self._finish()
            return
        self.headphones_row.set(CheckResult(Verdict.RUNNING, "Un bip est joué dans le casque. L'entendez-vous ?"))
        self._heard_btn.setVisible(True)
        self._not_heard_btn.setVisible(True)

    def _finish_headphones(self, heard: bool) -> None:
        self._release_audio()
        self._heard_btn.setVisible(False)
        self._not_heard_btn.setVisible(False)
        if heard:
            self.headphones_row.set(CheckResult(Verdict.OK, "Le casque fonctionne."))
        else:
            self.headphones_row.set(
                CheckResult(
                    Verdict.ERROR,
                    "Aucun son entendu. Vérifiez le casque choisi, son volume et qu'il n'est pas en Bluetooth A2DP.",
                )
            )
        self._finish()

    def _finish(self) -> None:
        self._step = "done"
        self._timer.stop()
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Relancer le test")
        results = [row.result for row in self.rows if row.result is not None]
        errors = sum(1 for r in results if r.verdict is Verdict.ERROR)
        warnings = sum(1 for r in results if r.verdict is Verdict.WARNING)
        if errors:
            self.summary.setText(f"✗ {errors} point(s) à corriger avant le live.")
        elif warnings:
            self.summary.setText(f"⚠ Prêt, avec {warnings} remarque(s).")
        else:
            self.summary.setText("✓ Tout est prêt. Vous pouvez vous connecter.")

    def _release_audio(self) -> None:
        for probe in (self._microphone, self._tone):
            if probe is not None:
                probe.close()
        self._microphone = None
        self._tone = None

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self._release_audio()
        super().closeEvent(event)
