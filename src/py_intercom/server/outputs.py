"""Physical server outputs: one PortAudio stream per (device, bus) pair."""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import clamp_gain_db, db_to_linear, limit_peak_inplace, rms_dbfs
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
    SAMPLE_RATE,
)
from ..common.logging import best_effort
from .models import OutputState

# A started stream that delivers no callback for this long is dead, even when
# PortAudio still reports it active (e.g. a WASAPI device that disappeared).
STREAM_STALL_TIMEOUT_S = 1.0
# Stream supervision: one check per tick, then a capped backoff between the
# reopen attempts of a failing stream. Attempts never stop while running.
_SUPERVISOR_TICK_S = 1.0
_REOPEN_BACKOFF_S = (1.0, 2.0, 5.0)


class OutputsMixin:
    def _add_output_locked(self, device: int, bus_id: object, gain_db: object = 0.0) -> OutputState:
        """Single factory of physical outputs: any fixed bus, bounded gain.

        Callers hold ``self._lock``. Raises ValueError for an invalid device or
        an unknown bus, before any state changes.
        """
        device_index = int(device)
        definition = require_bus(bus_id)
        output_id = int(self._next_output_id)
        self._next_output_id += 1
        out = OutputState(
            output_id=output_id,
            device=device_index,
            bus_id=int(definition.bus_id),
            gain_db=clamp_gain_db(gain_db),
        )
        self._outputs[output_id] = out
        return out

    def set_output_gain(self, output_id: int, gain_db: float) -> None:
        next_gain = clamp_gain_db(gain_db)
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

    def _stream_is_active(self, stream: sd.OutputStream | sd.InputStream | None) -> bool:
        if stream is None:
            return False
        try:
            return bool(stream.active)
        except Exception as exc:
            logger.debug("stream activity query failed: {}", exc)
            return False

    @staticmethod
    def _stream_stalled(last_callback_monotonic: float, *, now: float) -> bool:
        return float(now) - float(last_callback_monotonic) > STREAM_STALL_TIMEOUT_S

    def _output_stream_needs_reopen(self, out: OutputState, *, now: float | None = None) -> bool:
        with out.lock:
            stream = out.stream
            last_callback = float(out.last_callback_monotonic)
        current = time.monotonic() if now is None else float(now)
        # PortAudio is queried outside the lock the audio callback takes.
        return not self._stream_is_active(stream) or self._stream_stalled(last_callback, now=current)

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

    def _output_is_current(self, out: OutputState) -> bool:
        with self._lock:
            return self._outputs.get(int(out.output_id)) is out

    def _reopen_output_in_place(self, out: OutputState, *, blocking: bool = True) -> bool:
        """Close a dead output stream, then reopen it on the same device.

        Closing first matters for drivers that refuse a second handle on one
        device (ASIO, WDM-KS); a dead stream has nothing audible to preserve.
        A healthy stream is left alone, so concurrent callers cannot cut it.
        Returns whether a stream was actually reopened.
        """
        if not out.open_lock.acquire(blocking=blocking):
            return False
        try:
            # Checked after the health: a replaced output is closed only after
            # its swap, so seeing it dead implies seeing it replaced.
            if not self._output_stream_needs_reopen(out) or not self._output_is_current(out):
                return False
            self._close_output_stream(out)
            self._open_output_stream(out)
            if not self._output_is_current(out):
                # Removed or replaced meanwhile: never leave an orphan stream.
                self._close_output_stream(out)
                return False
            return True
        finally:
            out.open_lock.release()

    def _restart_output_stream(self, out: OutputState) -> None:
        if self._output_stream_needs_reopen(out):
            self._reopen_output_in_place(out)
            return
        # A healthy stream keeps playing until its replacement runs. A failed
        # replacement leaves it untouched and unflagged: the error belongs to
        # the attempt, not to an output that still works.
        self._replace_output_state(out, device=int(out.device))

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

    def _start_stream_supervisor(self) -> None:
        self._new_worker("stream-supervisor", self._supervise_streams, critical=False).start()

    def _supervise_streams(self) -> None:
        """Reopen dead output and Return streams for as long as the server runs.

        Off the audio path: one check per tick, PortAudio calls without any
        engine lock held, and a capped backoff per failing stream so a device
        that stays absent is retried forever without hammering its driver.
        """
        backoff: dict[str, tuple[int, float]] = {}
        while not self._stop.wait(_SUPERVISOR_TICK_S):
            self._supervise_streams_once(backoff)

    def _supervise_streams_once(
        self,
        backoff: dict[str, tuple[int, float]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        now = clock()
        with self._lock:
            outputs = list(self._outputs.values())
        dead: dict[str, Callable[[], bool]] = {
            f"output {int(out.output_id)}": partial(self._reopen_output_in_place, out, blocking=False)
            for out in outputs
            if self._output_stream_needs_reopen(out, now=now)
        }
        if self._return_stream_needs_reopen(now=now):
            dead["Return"] = self._reopen_return_in_place

        for key in list(backoff):
            if key not in dead:
                # Recovered, removed or disabled: a new failure starts afresh.
                del backoff[key]

        for key, reopen in dead.items():
            if self._stop.is_set():
                return
            failures, not_before = backoff.get(key, (0, 0.0))
            if now < not_before:
                continue
            try:
                reopened = reopen()
            except Exception as exc:
                delay_s = _REOPEN_BACKOFF_S[min(failures, len(_REOPEN_BACKOFF_S) - 1)]
                # Counted from the end of the attempt: a driver that takes
                # longer than the delay to fail still gets its full rest.
                failed_at = clock()
                backoff[key] = (failures + 1, failed_at + delay_s)
                # A device absent for the whole show is retried forever, but
                # logged once per window with the count of hidden attempts.
                note = self._stream_reopen_log.allow(key, now=failed_at)
                if note is not None:
                    logger.warning(
                        "{} reopen attempt {} failed, next in {:.0f} s: {}{}", key, failures + 1, delay_s, exc, note
                    )
            else:
                # False when another opener holds the stream, or it recovered
                # or was removed meanwhile: neither a failure nor a reopen.
                if reopened:
                    backoff.pop(key, None)
                    logger.info("{} stream reopened by supervision", key)

    def create_output(self, device: int, bus_id: int) -> int:
        with self._lock:
            out = self._add_output_locked(int(device), bus_id)
            # Taken before the output becomes visible (a fresh lock never
            # blocks): a supervisor tick that sees it without a stream yet
            # cannot open a second one, which nothing would ever close.
            out.open_lock.acquire()
        try:
            if self._running and not self._stop.is_set():
                self._open_output_stream(out)
        except Exception:
            with self._lock:
                self._outputs.pop(int(out.output_id), None)
            raise
        finally:
            out.open_lock.release()
        self._autosave_preset()
        return int(out.output_id)

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
                out.last_callback_monotonic = time.monotonic()
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
        # PortAudio thread: no logging and no allocation, problems are counted.
        now = time.monotonic()
        with out.lock:
            out.last_callback_monotonic = float(now)
            if status:
                out.callback_statuses += 1
            scratch = out.callback_scratch
            if int(frames) != int(FRAME_SAMPLES) or out.ring.pop_into(scratch) is None:
                out.underflows += 1
                out.vu_dbfs = -60.0
                outdata.fill(0.0)
                return

            # The ring carries the linear bus sum. The output gain comes first
            # and the limiter is always the last stage before the device.
            scratch *= db_to_linear(float(out.gain_db))
            limit_peak_inplace(scratch)
            out.vu_dbfs = rms_dbfs(scratch)
            # Streams are opened mono (CHANNELS == 1).
            np.copyto(outdata[:, 0], scratch, casting="unsafe")
