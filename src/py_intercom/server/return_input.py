"""Return bus: a local server input mixed into each subscribed client's feed."""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import limit_peak_inplace, rms_dbfs
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
        with self._lock:
            enabled = bool(self._return_enabled)
            device = self._return_input_device
        if not enabled or device is None:
            self._close_return_input_stream()
            return
        try:
            self._open_return_input_stream(int(device))
            logger.debug("reopen_return_input: stream reopened on device {}", device)
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

        if not bool(enabled):
            with self._return_lock:
                self._return_vu_dbfs = -60.0

        # A running server opens or closes the stream to match the new state.
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
        replaced_stream = None
        try:
            with self._lock:
                if self._return_input_device != old_device:
                    raise RuntimeError("l'entrée Programme a changé pendant l'ouverture du nouveau périphérique")
                wants_live_stream = (
                    self._running and not self._stop.is_set() and self._return_enabled and dev_val is not None
                )
                if wants_live_stream and candidate_stream is not None and candidate_gate is not None:
                    replaced_stream = self._commit_return_input_stream(candidate_stream, candidate_gate)
                    candidate_adopted = True
                self._return_input_device = dev_val
                # The operator's choice replaces a card absent since loading.
                self._unresolved_return = None
                selection_committed = True
        finally:
            if not candidate_adopted and candidate_stream is not None:
                if candidate_gate is not None:
                    candidate_gate.clear()
                self._close_stream(candidate_stream)

        # PortAudio stop/close can take long on some drivers: it happens outside
        # every engine lock so RX, mix and broadcast never wait on it.
        self._close_stream(replaced_stream)

        if selection_committed and self._running and self._return_enabled and dev_val is None:
            self._close_return_input_stream()

        self._autosave_preset()

    def _close_return_input_stream(self, *, only: sd.InputStream | None = None) -> bool:
        """Close the live Return stream, or only ``only`` if it is still live.

        Returns False when ``only`` was already replaced: the stream committed
        meanwhile is healthy and keeps playing.
        """
        with self._return_lock:
            stream = self._return_stream
            if only is not None and stream is not only:
                return False
            gate = self._return_stream_gate
            if gate is not None:
                gate.clear()
            self._return_stream = None
            self._return_stream_gate = None
            self._return_stream_error = ""
            self._return_last_callback_monotonic = 0.0
        self._close_stream(stream)
        with self._return_lock:
            self._return_vu_dbfs = -60.0
            self._return_input_scratch.fill(0.0)
            self._return_frames.clear()
        return True

    def _create_return_input_stream(self, device: int) -> tuple[sd.InputStream, threading.Event]:
        in_sr = int(SAMPLE_RATE)
        blocksize_frame = int(FRAME_SAMPLES)
        gate = threading.Event()

        # Pre-check the device against the requested format. This surfaces a
        # clear error before allocating the stream and gives a friendly message.
        dev_info = None
        try:
            dev_info = sd.query_devices(int(device))
        except Exception as e:
            logger.warning("failed to query return input device {}: {}", int(device), e)
        extra_settings = shared_stream_extra_settings(dev_info)
        # A programme feed is stereo: both sides are summed to mono, so a
        # source panned or recorded on one side only is never lost.
        channels = 2 if dev_info is not None and int(dev_info.get("max_input_channels", 1)) >= 2 else 1
        try:
            sd.check_input_settings(
                device=int(device),
                channels=channels,
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
                ch=channels,
            )
            raise RuntimeError(msg) from e

        st: sd.InputStream | None = None
        try:
            st = sd.InputStream(
                samplerate=float(in_sr),
                channels=channels,
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
                ch=channels,
            )
            raise RuntimeError(msg) from e
        return st, gate

    def _commit_return_input_stream(
        self,
        stream: sd.InputStream,
        gate: threading.Event,
    ) -> sd.InputStream | None:
        """Make ``stream`` the live Return input and return the one it replaces.

        The replaced stream is already gated off; the caller closes it once it
        holds no engine lock.
        """
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
            # A fresh stream gets one full stall window for its first callback.
            self._return_last_callback_monotonic = time.monotonic()
            self._return_vu_dbfs = -60.0
            self._return_input_scratch.fill(0.0)
            self._return_frames.clear()
            gate.set()
        return old_stream

    def _open_return_input_stream(self, device: int) -> None:
        stream = None
        gate = None
        replaced_stream = None
        try:
            stream, gate = self._create_return_input_stream(int(device))
            with self._lock:
                # The selection may have moved on while the stream was opening;
                # committing it anyway would play the wrong device for good.
                if self._return_input_device != int(device):
                    raise RuntimeError("le périphérique de l'entrée Programme a changé pendant l'ouverture")
                replaced_stream = self._commit_return_input_stream(stream, gate)
        except Exception as exc:
            if gate is not None:
                gate.clear()
            self._close_stream(stream)
            with self._return_lock:
                if self._return_stream is None and self._running and not self._stop.is_set() and self._return_enabled:
                    self._return_stream_error = str(exc)
            raise
        self._close_stream(replaced_stream)

    def _return_stream_needs_reopen(self, *, now: float) -> bool:
        with self._lock:
            wanted = bool(self._return_enabled) and self._return_input_device is not None
        if not wanted:
            return False
        with self._return_lock:
            stream = self._return_stream
            last_callback = float(self._return_last_callback_monotonic)
        # PortAudio is queried outside the lock the audio callback takes.
        return not self._stream_is_active(stream) or self._stream_stalled(last_callback, now=now)

    def _reopen_return_in_place(self) -> bool:
        """Close a dead Return stream, then reopen it on the selected device.

        Returns whether a stream was actually reopened. Only the stream seen
        dead is closed: one an operator action committed meanwhile keeps
        playing, and a Return disabled meanwhile is left closed.
        """
        with self._return_lock:
            seen = self._return_stream
        if not self._return_stream_needs_reopen(now=time.monotonic()):
            return False
        with self._lock:
            device = self._return_input_device
        if device is None:
            return False
        if seen is not None and not self._close_return_input_stream(only=seen):
            return False
        self._open_return_input_stream(int(device))
        return True

    def _return_in_callback(
        self,
        indata,
        frames,
        time_info,
        status,
        *,
        active_gate: threading.Event | None = None,
    ) -> None:
        # PortAudio thread: no logging and no allocation, problems are counted.
        if active_gate is not None and not active_gate.is_set():
            return
        self._return_last_callback_monotonic = time.monotonic()
        if status:
            self._return_callback_statuses += 1

        if self._stop.is_set() or not bool(self._return_enabled):
            return

        if int(frames) != int(FRAME_SAMPLES):
            self._return_frame_mismatches += 1
            return

        with self._return_lock:
            # A previous stream may have entered the callback just before its
            # gate was cleared during a device swap. Re-check after acquiring
            # the shared scratch lock so it cannot contaminate the new stream's
            # ring after the atomic commit.
            if active_gate is not None and not active_gate.is_set():
                return
            scratch = self._return_input_scratch
            if indata.shape[1] >= 2:
                # Equal-weight sum (-6 dB per side): identical sides keep
                # their level and a full-scale programme never clips.
                np.add(indata[:, 0], indata[:, 1], out=scratch, casting="unsafe")
                np.multiply(scratch, 0.5, out=scratch)
            else:
                np.copyto(scratch, indata[:, 0], casting="unsafe")
            limit_peak_inplace(scratch)
            self._return_vu_dbfs = rms_dbfs(scratch)
            self._return_frames.push(scratch)
