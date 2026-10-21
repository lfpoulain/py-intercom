"""Return bus: a local server input mixed into each subscribed client's feed."""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import rms_dbfs
from ..common.audio_errors import (
    friendly_audio_open_error,
    shared_stream_extra_settings,
)
from ..common.constants import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
)
from ..common.logging import best_effort


class ReturnInputMixin:
    def reopen_return_input(self) -> None:
        if not bool(self._running) or self._stop.is_set():
            return
        if not bool(self._return_enabled) or self._return_input_device is None:
            self._close_return_input_stream()
            return
        try:
            self._open_return_input_stream(int(self._return_input_device))
            logger.debug("reopen_return_input: stream reopened on device {}", self._return_input_device)
        except Exception as e:
            logger.warning("reopen_return_input failed: {}", e)

    def set_return_enabled(self, enabled: bool) -> None:
        changed = False
        with self._lock:
            if self._return_enabled != bool(enabled):
                self._return_enabled = bool(enabled)
                changed = True
        
        if not changed:
            return

        # Remise à zéro du VU mètre si désactivé
        if not bool(enabled):
            with self._return_lock:
                self._return_vu_dbfs = -60.0

        # Si le serveur tourne, on rouvre (ou ferme) le stream
        if self._running:
            self.reopen_return_input()

        self._autosave_preset()

    def set_return_input_device(self, device: int | None) -> None:
        dev_val = int(device) if device is not None else None
        with self._lock:
            old_device = self._return_input_device
        if old_device == dev_val:
            return

        candidate_stream = None
        candidate_gate = None
        if self._running and not self._stop.is_set() and self._return_enabled and dev_val is not None:
            # Candidate starts gated and cannot enter the Return ring before the
            # device selection and stream are committed together.
            candidate_stream, candidate_gate = self._create_return_input_stream(dev_val)

        candidate_adopted = False
        selection_committed = False
        try:
            with self._lock:
                if self._return_input_device != old_device:
                    raise RuntimeError("return input changed while opening replacement device")
                wants_live_stream = (
                    self._running
                    and not self._stop.is_set()
                    and self._return_enabled
                    and dev_val is not None
                )
                if (
                    wants_live_stream
                    and candidate_stream is not None
                    and candidate_gate is not None
                ):
                    self._commit_return_input_stream(candidate_stream, candidate_gate)
                    candidate_adopted = True
                self._return_input_device = dev_val
                selection_committed = True
        finally:
            if not candidate_adopted and candidate_stream is not None:
                if candidate_gate is not None:
                    candidate_gate.clear()
                self._close_stream(candidate_stream)

        if (
            selection_committed
            and self._running
            and self._return_enabled
            and dev_val is None
        ):
            self._close_return_input_stream()

        self._autosave_preset()

    def _close_return_input_stream(self) -> None:
        with self._return_lock:
            stream = self._return_stream
            gate = self._return_stream_gate
            if gate is not None:
                gate.clear()
            self._return_stream = None
            self._return_stream_gate = None
            self._return_stream_error = ""
        self._close_stream(stream)
        with self._return_lock:
            self._return_vu_dbfs = -60.0
            self._return_input_scratch.fill(0.0)
            self._return_frames.clear()

    def _create_return_input_stream(self, device: int) -> tuple[sd.InputStream, threading.Event]:
        in_sr = int(SAMPLE_RATE)
        blocksize_frame = int(FRAME_SAMPLES)
        gate = threading.Event()

        # Pre-check the device against the requested format. This surfaces a
        # clear error before allocating the stream and gives a friendly message.
        dev_info = None
        try:
            dev_info = sd.query_devices(int(device))
        except Exception:
            dev_info = None
        extra_settings = shared_stream_extra_settings(dev_info)
        try:
            sd.check_input_settings(
                device=int(device),
                channels=1,
                dtype="float32",
                samplerate=int(in_sr),
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=int(device),
                sr=int(in_sr),
                ch=1,
            )
            raise RuntimeError(msg) from e

        st: sd.InputStream | None = None
        try:
            st = sd.InputStream(
                samplerate=float(in_sr),
                channels=1,
                dtype="float32",
                blocksize=blocksize_frame,
                device=int(device),
                latency="low",
                extra_settings=extra_settings,
                callback=lambda indata, frames, time_info, status: self._return_in_callback(
                    indata,
                    frames,
                    time_info,
                    status,
                    active_gate=gate,
                ),
            )
            st.start()
        except Exception as e:
            with best_effort("close failed return stream"):
                if st is not None:
                    st.stop()
                    st.close()
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=int(device),
                sr=int(in_sr),
                ch=1,
            )
            raise RuntimeError(msg) from e
        return st, gate

    def _commit_return_input_stream(
        self,
        stream: sd.InputStream,
        gate: threading.Event,
    ) -> None:
        with self._return_lock:
            # stop() sets its event before detaching streams. If it wins this
            # lock, a late candidate must never be committed after shutdown;
            # if this commit wins, stop() will subsequently close the new one.
            if self._stop.is_set() or not self._running or not self._return_enabled:
                raise RuntimeError("return input stream commit cancelled")
            old_stream = self._return_stream
            old_gate = self._return_stream_gate
            if old_gate is not None:
                old_gate.clear()
            self._return_stream = stream
            self._return_stream_gate = gate
            self._return_stream_error = ""
            self._return_in_samplerate = int(SAMPLE_RATE)
            self._return_vu_dbfs = -60.0
            self._return_input_scratch.fill(0.0)
            self._return_frames.clear()
            gate.set()
        self._close_stream(old_stream)

    def _open_return_input_stream(self, device: int) -> None:
        stream = None
        gate = None
        try:
            stream, gate = self._create_return_input_stream(int(device))
            self._commit_return_input_stream(stream, gate)
        except Exception as exc:
            if gate is not None:
                gate.clear()
            if stream is not None:
                with self._return_lock:
                    is_current = self._return_stream is stream
                if not is_current:
                    self._close_stream(stream)
            with self._return_lock:
                if (
                    self._return_stream is None
                    and self._running
                    and not self._stop.is_set()
                    and self._return_enabled
                ):
                    self._return_stream_error = str(exc)
            raise

    def _return_in_callback(
        self,
        indata,
        frames,
        time_info,
        status,
        *,
        active_gate: threading.Event | None = None,
    ) -> None:
        if active_gate is not None and not active_gate.is_set():
            return
        if status:
            logger.debug("return in status: {}", status)

        if self._stop.is_set() or not bool(self._return_enabled):
            return

        if int(frames) != int(FRAME_SAMPLES):
            logger.debug("return in frame mismatch: got={} expected={}", int(frames), int(FRAME_SAMPLES))
            return

        with self._return_lock:
            # A previous stream may have entered the callback just before its
            # gate was cleared during a device swap. Re-check after acquiring
            # the shared scratch lock so it cannot contaminate the new stream's
            # ring after the atomic commit.
            if active_gate is not None and not active_gate.is_set():
                return
            scratch = self._return_input_scratch
            try:
                if indata.ndim == 1:
                    np.copyto(scratch, indata, casting="unsafe")
                elif indata.shape[1] == 1:
                    np.copyto(scratch, indata[:, 0], casting="unsafe")
                else:
                    np.mean(indata, axis=1, dtype=np.float32, out=scratch)
            except Exception:
                scratch.fill(0.0)
                self._return_vu_dbfs = -60.0
                return

            peak = max(float(np.max(scratch)), -float(np.min(scratch)))
            if peak > 1.0:
                scratch *= 0.99 / peak
            np.clip(scratch, -1.0, 1.0, out=scratch)
            try:
                self._return_vu_dbfs = float(rms_dbfs(scratch))
            except Exception:
                self._return_vu_dbfs = -60.0
            self._return_frames.push(scratch)
