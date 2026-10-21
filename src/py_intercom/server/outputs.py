"""Physical server outputs: one PortAudio stream per (device, bus) pair."""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import db_to_linear, rms_dbfs
from ..common.audio_errors import (
    friendly_audio_open_error,
    shared_stream_extra_settings,
)
from ..common.buses import (
    require_bus,
)
from ..common.constants import (
    CHANNELS,
    FRAME_SAMPLES,
    MAX_GAIN_DB,
    SAMPLE_RATE,
)
from ..common.logging import best_effort
from .models import OutputState


class OutputsMixin:
    def set_output_gain(self, output_id: int, gain_db: float) -> None:
        next_gain = max(-60.0, min(float(MAX_GAIN_DB), float(gain_db)))
        changed = False
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            if abs(float(out.gain_db) - float(next_gain)) >= 0.01:
                out.gain_db = float(next_gain)
                changed = True

        if not changed:
            return

        self._autosave_preset()

    def _stream_is_active(self, stream: sd.OutputStream | None) -> bool:
        if stream is None:
            return False
        try:
            return bool(stream.active)
        except Exception:
            return False

    def _output_stream_needs_reopen(self, out: OutputState, *, now: float | None = None) -> bool:
        with out.lock:
            stream = out.stream
            last_callback = float(out.last_callback_monotonic)
        if stream is None or not self._stream_is_active(stream):
            return True
        if now is None:
            now = time.monotonic()
        return last_callback <= 0.0 or (float(now) - float(last_callback)) > 1.0

    @staticmethod
    def _close_stream(stream) -> None:
        if stream is None:
            return
        with best_effort("stream.stop()"):
            stream.stop()
        with best_effort("stream.close()"):
            stream.close()

    def _close_output_stream(self, out: OutputState) -> None:
        with out.lock:
            stream = out.stream
            out.stream = None
            out.last_callback_monotonic = 0.0
            out.vu_dbfs = -60.0
        self._close_stream(stream)
        out.ring.clear()

    def _replace_output_state(self, out: OutputState, *, device: int) -> OutputState:
        candidate = OutputState(
            output_id=int(out.output_id),
            device=int(device),
            bus_id=int(out.bus_id),
            gain_db=float(out.gain_db),
        )
        self._open_output_stream(candidate)

        swapped = False
        try:
            with self._lock:
                if self._outputs.get(int(out.output_id)) is not out:
                    raise RuntimeError("output changed while opening replacement device")
                if self._stop.is_set() or not self._running:
                    raise RuntimeError("output replacement cancelled during shutdown")
                with out.lock:
                    latest_bus_id = int(out.bus_id)
                    latest_gain_db = float(out.gain_db)
                with candidate.lock:
                    candidate.bus_id = latest_bus_id
                    candidate.gain_db = latest_gain_db
                self._outputs[int(out.output_id)] = candidate
                swapped = True
        finally:
            if not swapped:
                self._close_output_stream(candidate)

        # The old stream remains fully operational until after the atomic swap.
        self._close_output_stream(out)
        return candidate

    def _restart_output_stream(self, out: OutputState) -> OutputState:
        try:
            return self._replace_output_state(out, device=int(out.device))
        except Exception as exc:
            with out.lock:
                out.stream_error = str(exc)
            raise

    def _ensure_output_retry_thread(self) -> None:
        with self._worker_lock:
            t = self._retry_thread
            if t is not None and t.is_alive():
                return
            t = threading.Thread(
                target=self._worker_entry,
                args=("output-retry", self._retry_output_streams),
                kwargs={"critical": False},
                name="output-retry",
                daemon=True,
            )
            self._retry_thread = t
            self._worker_threads["output-retry"] = t
        t.start()

    def reopen_outputs(self, *, force: bool = False) -> None:
        if not self._running or self._stop.is_set():
            return
        with self._lock:
            outputs = list(self._outputs.values())

        for out in outputs:
            if not force and not self._output_stream_needs_reopen(out):
                continue
            try:
                self._restart_output_stream(out)
            except Exception as e:
                logger.warning("output {} reopen failed: {}", int(out.output_id), e)
        if outputs:
            self._ensure_output_retry_thread()

    def _retry_output_streams(self) -> None:
        retries = 3
        delay_s = 1.0
        try:
            for attempt in range(retries):
                if self._stop.is_set():
                    return
                if self._stop.wait(delay_s):
                    return
                with self._lock:
                    outputs = list(self._outputs.values())
                now = time.monotonic()
                pending = [out for out in outputs if self._output_stream_needs_reopen(out, now=now)]
                if not pending:
                    return
                for out in pending:
                    try:
                        self._restart_output_stream(out)
                    except Exception as e:
                        logger.warning(
                            "output {} retry {}/{} failed: {}",
                            int(out.output_id),
                            attempt + 1,
                            retries,
                            e,
                        )
        finally:
            with self._worker_lock:
                if self._retry_thread is threading.current_thread():
                    self._retry_thread = None

    def create_output(self, device: int, bus_id: int) -> int:
        definition = require_bus(bus_id)
        with self._lock:
            out = OutputState(output_id=0, device=int(device), bus_id=int(definition.bus_id))
            oid = int(self._next_output_id)
            self._next_output_id += 1
            out.output_id = oid
            self._outputs[oid] = out

        if self._running and not self._stop.is_set():
            try:
                self._open_output_stream(out)
            except Exception:
                with self._lock:
                    self._outputs.pop(int(oid), None)
                raise
        self._autosave_preset()
        return int(oid)

    def remove_output(self, output_id: int) -> None:
        with self._lock:
            out = self._outputs.pop(int(output_id), None)

        if out is None:
            return
        self._close_output_stream(out)
        self._autosave_preset()

    def set_output_bus(self, output_id: int, bus_id: int) -> None:
        try:
            definition = require_bus(bus_id)
        except ValueError:
            return
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            out.bus_id = int(definition.bus_id)

        self._autosave_preset()

    def set_output_device(self, output_id: int, device: int) -> None:
        next_device = int(device)
        with self._lock:
            out = self._outputs.get(int(output_id))
            if out is None:
                return
            if int(out.device) == next_device:
                return

        if self._running and not self._stop.is_set():
            # Candidate is fully preflighted, opened and started before the
            # live state is swapped. Any exception leaves `out` untouched.
            self._replace_output_state(out, device=next_device)
        else:
            with self._lock:
                if self._outputs.get(int(output_id)) is not out:
                    return
                out.device = next_device
                out.stream_error = ""

        self._autosave_preset()

    def _open_output_stream(self, out: OutputState) -> None:
        dev = None
        try:
            dev = sd.query_devices(int(out.device))
            logger.debug(
                "output device: idx={} name={} hostapi={} max_out={} default_sr={}",
                int(out.device),
                dev.get("name"),
                dev.get("hostapi"),
                dev.get("max_output_channels"),
                dev.get("default_samplerate"),
            )
        except Exception as e:
            logger.warning("failed to query output device {}: {}", int(out.device), e)

        sr = int(SAMPLE_RATE)
        blocksize = int(FRAME_SAMPLES)
        extra_settings = shared_stream_extra_settings(dev)

        try:
            sd.check_output_settings(
                device=int(out.device),
                channels=int(CHANNELS),
                dtype="float32",
                samplerate=int(sr),
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev,
                device=int(out.device),
                sr=int(sr),
                ch=int(CHANNELS),
            )
            with out.lock:
                out.stream = None
                out.stream_error = str(msg)
            raise RuntimeError(msg) from e

        stream: sd.OutputStream | None = None
        try:
            with out.lock:
                out.samplerate = int(sr)
                out.ring.clear()
                out.callback_scratch.fill(0.0)
                out.last_callback_monotonic = 0.0

            stream = sd.OutputStream(
                samplerate=float(sr),
                channels=CHANNELS,
                dtype="float32",
                blocksize=blocksize,
                device=int(out.device),
                latency="low",
                extra_settings=extra_settings,
                callback=lambda outdata, frames, time_info, status, state=out: self._output_state_callback(
                    state, outdata, frames, time_info, status
                ),
            )
            stream.start()
            with out.lock:
                # stop() signals before it detaches output streams. Checking
                # under the same state lock prevents a stream from being
                # assigned just after shutdown already tried to close it.
                if self._stop.is_set():
                    raise RuntimeError("output stream opening cancelled during shutdown")
                out.stream = stream
                out.stream_error = ""
            logger.debug(
                "audio output stream started: out_id={} bus_id={} sr={} ch={} blocksize={} latency=low",
                int(out.output_id),
                int(out.bus_id),
                sr,
                CHANNELS,
                blocksize,
            )
        except Exception as e:
            with best_effort("close failed output stream"):
                if stream is not None:
                    stream.stop()
                    stream.close()
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev,
                device=int(out.device),
                sr=int(sr),
                ch=int(CHANNELS),
            )
            with out.lock:
                out.stream = None
                out.stream_error = str(msg)
            raise RuntimeError(msg) from e

    def _output_state_callback(self, out: OutputState, outdata, frames, time_info, status) -> None:
        if status:
            logger.debug("audio status: {}", status)

        now = time.monotonic()
        with out.lock:
            out.last_callback_monotonic = float(now)
            if int(out.samplerate) != int(SAMPLE_RATE) or int(frames) != int(FRAME_SAMPLES):
                out.underflows += 1
                out.vu_dbfs = -60.0
                outdata.fill(0.0)
                return

            scratch = out.callback_scratch
            if out.ring.pop_into(scratch) is None:
                out.underflows += 1
                out.vu_dbfs = -60.0
                outdata.fill(0.0)
                return

            np.clip(scratch, -1.0, 1.0, out=scratch)
            scratch *= db_to_linear(float(out.gain_db))
            np.clip(scratch, -1.0, 1.0, out=scratch)
            try:
                out.vu_dbfs = rms_dbfs(scratch)
            except Exception:
                out.vu_dbfs = -60.0

            if outdata.ndim == 1:
                np.copyto(outdata, scratch, casting="unsafe")
            else:
                outdata.fill(0.0)
                np.copyto(outdata[:, 0], scratch, casting="unsafe")
