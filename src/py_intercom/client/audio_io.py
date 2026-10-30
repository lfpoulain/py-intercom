"""Sound card side of the client: PortAudio streams and their realtime callbacks.

The callbacks never take ``_state_lock``: the gains, the PTT routes and the
meters are single attribute or dict item accesses, atomic under the GIL, and
a lock shared with the GUI could make a callback miss its deadline.
"""

from __future__ import annotations

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio import db_to_linear, limit_peak_inplace, rms_dbfs
from ..common.audio_errors import (
    friendly_audio_open_error,
    shared_stream_extra_settings,
)
from ..common.buses import FIXED_BUS_IDS
from ..common.constants import (
    CHANNELS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
)

# A callback that fails this many frames in a row (0.5 s) gives its stream up.
# The stream then reads inactive: the operator status turns degraded and cuts
# PTT, and the next connection rebuilds the streams. Shorter bursts are ridden out.
_CALLBACK_ABORT_AFTER_ERRORS = 50


class AudioIOMixin:
    @staticmethod
    def _query_audio_device(device: int | None) -> dict | None:
        if device is None:
            return None
        try:
            info = sd.query_devices(int(device))
        except Exception:
            return None
        return info if isinstance(info, dict) else None

    def _streams_latency_ms(self) -> float | None:
        """Input plus output latency reported by PortAudio, in ms."""
        total = 0.0
        for stream in (self._in_stream, self._out_stream):
            if stream is None:
                return None
            try:
                total += float(stream.latency)
            except sd.PortAudioError:
                return None
        return total * 1000.0

    @staticmethod
    def _stream_is_active(stream) -> bool:
        if stream is None:
            return False
        try:
            return bool(stream.active)
        except sd.PortAudioError:
            # A stream whose device vanished can no longer be queried: it is
            # reported inactive, which the operator status shows.
            return False

    def audio_streams_active(self) -> bool:
        """Whether both the capture and the playback streams are running."""
        return self._stream_is_active(self._in_stream) and self._stream_is_active(self._out_stream)

    def _audio_device(self, kind: str) -> int | None:
        return self.config.input_device if kind == "input" else self.config.output_device

    def _audio_open_error(self, exc: Exception, *, kind: str, dev_info: dict | None) -> RuntimeError:
        return RuntimeError(
            friendly_audio_open_error(
                exc,
                kind=kind,
                dev_info=dev_info,
                device=self._audio_device(kind),
                sr=int(SAMPLE_RATE),
                ch=int(CHANNELS),
            )
        )

    def _open_audio_stream(self, kind: str) -> sd.InputStream | sd.OutputStream:
        """Create, without starting it, the ``input`` or ``output`` stream at the engine format."""
        device = self._audio_device(kind)
        dev_info = self._query_audio_device(device)
        if dev_info is not None:
            logger.debug(
                "{} device: name={} hostapi={} max_in={} max_out={} default_sr={}",
                kind,
                dev_info.get("name"),
                dev_info.get("hostapi"),
                dev_info.get("max_input_channels"),
                dev_info.get("max_output_channels"),
                dev_info.get("default_samplerate"),
            )

        is_input = kind == "input"
        settings = {
            "device": device,
            "channels": int(CHANNELS),
            "dtype": "float32",
            "samplerate": int(SAMPLE_RATE),
            "extra_settings": shared_stream_extra_settings(dev_info),
        }
        try:
            if is_input:
                sd.check_input_settings(**settings)
            else:
                sd.check_output_settings(**settings)
            logger.debug(
                "opening {} stream: dev={} ch={} blocksize={} latency=low sr={}",
                kind,
                str(device),
                CHANNELS,
                FRAME_SAMPLES,
                SAMPLE_RATE,
            )
            stream_class = sd.InputStream if is_input else sd.OutputStream
            return stream_class(
                **settings,
                blocksize=int(FRAME_SAMPLES),
                latency="low",
                callback=self._in_callback if is_input else self._out_callback,
            )
        except Exception as e:
            raise self._audio_open_error(e, kind=kind, dev_info=dev_info) from e

    def _open_input_stream(self) -> sd.InputStream:
        return self._open_audio_stream("input")

    def _open_output_stream(self) -> sd.OutputStream:
        return self._open_audio_stream("output")

    def _start_audio_stream(self, stream, *, kind: str) -> None:
        try:
            stream.start()
        except Exception as e:
            dev_info = self._query_audio_device(self._audio_device(kind))
            raise self._audio_open_error(e, kind=kind, dev_info=dev_info) from e

    def _start_audio_streams(self) -> None:
        # New streams do not inherit the error streaks of the ones they replace.
        self._input_callback_error_streak = 0
        self._output_callback_error_streak = 0
        # Opening the microphone first is significant for Bluetooth headsets:
        # Windows establishes the bidirectional HFP profile when capture
        # starts. Playback is opened only after that transition, so it cannot
        # be an already-running A2DP stream invalidated by the profile switch.
        self._in_stream = self._open_input_stream()
        self._start_audio_stream(self._in_stream, kind="input")
        self._out_stream = self._open_output_stream()
        self._start_audio_stream(self._out_stream, kind="output")

    def _close_audio_streams(self) -> None:
        # Reverse startup order. In particular, keep Bluetooth capture/HFP
        # active until its matching playback endpoint has been closed.
        out_stream, self._out_stream = self._out_stream, None
        in_stream, self._in_stream = self._in_stream, None
        self._close_audio_stream(out_stream)
        self._close_audio_stream(in_stream)

    @staticmethod
    def _close_audio_stream(stream) -> None:
        if stream is None:
            return
        # PortAudio backends differ in which operations remain valid after a
        # failed start.  Each step is isolated so a stop/abort failure can
        # never prevent the final close.
        for operation in ("abort", "stop", "close"):
            method = getattr(stream, operation, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception as e:
                logger.debug("audio stream {} during cleanup failed: {}", operation, e)

    def _in_callback(self, indata, frames, time_info, status) -> None:
        try:
            self._capture_frame(indata, frames, status)
        except Exception as e:
            self._input_callback_errors += 1
            self._input_callback_error_streak += 1
            self._abort_on_persistent_error("input", self._input_callback_error_streak, e)
        else:
            self._input_callback_error_streak = 0

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        try:
            self._play_frame(outdata, frames, status)
        except Exception as e:
            outdata.fill(0.0)
            self._output_callback_errors += 1
            self._output_callback_error_streak += 1
            self._abort_on_persistent_error("output", self._output_callback_error_streak, e)
        else:
            self._output_callback_error_streak = 0

    @staticmethod
    def _abort_on_persistent_error(kind: str, streak: int, exc: Exception) -> None:
        """Ride out a passing callback failure, give the stream up on a lasting one.

        An exception escaping the callback would abort the stream at the first
        failure. Below the threshold the error is only counted, since logging
        could make the callback miss its deadline; the abort itself is logged.
        """
        if streak < _CALLBACK_ABORT_AFTER_ERRORS:
            return
        logger.error(
            "{} audio callback failed {} frames in a row, stopping the stream: {}: {}",
            kind,
            streak,
            type(exc).__name__,
            exc,
        )
        raise sd.CallbackAbort from exc

    def _update_bus_vu(self, bus_mask: int, vu_dbfs: float) -> None:
        """Show the capture level on the buses talked to; the others decay."""
        for bus_id in FIXED_BUS_IDS:
            bid = int(bus_id)
            if bus_mask & (1 << bid):
                self._bus_vu_dbfs[bid] = vu_dbfs
            else:
                self._bus_vu_dbfs[bid] = max(-60.0, self._bus_vu_dbfs[bid] - 3.0)

    def _capture_frame(self, indata, frames, status) -> None:
        if status:
            self._input_callback_statuses += 1

        if self._stop.is_set():
            self._in_vu_dbfs = -60.0
            self._in_transmitting = False
            return

        if int(frames) != int(FRAME_SAMPLES):
            self._input_frame_mismatches += 1
            return

        # Measured even with no talk key held, so the operator can check the
        # microphone and its gain before speaking (a few microseconds).
        np.copyto(self._capture_work, indata[:, 0], casting="same_kind")
        np.multiply(self._capture_work, db_to_linear(self._input_gain_db), out=self._capture_work)
        limit_peak_inplace(self._capture_work)
        vu = rms_dbfs(self._capture_work)
        self._in_vu_dbfs = vu

        bus_mask = self._ptt_bus_mask(self._ptt_buses) if self._udp_ready.is_set() else 0
        with self._tx_lock:
            session_token = int(self._session_token)
            session_epoch = int(self._audio_session_epoch)
        if bus_mask == 0 or session_token == 0:
            self._in_transmitting = False
            self._update_bus_vu(0, -60.0)
            return
        self._in_transmitting = True
        self._update_bus_vu(bus_mask, vu)

        metadata = self._pack_tx_metadata(session_epoch, bus_mask)
        with self._tx_lock:
            if (
                int(self._session_token) != session_token
                or int(self._audio_session_epoch) != session_epoch
                or self._stop.is_set()
                or not self._udp_ready.is_set()
            ):
                return
            self._tx_ring.push(self._capture_work, metadata=metadata)

    def _play_frame(self, outdata, frames, status) -> None:
        if status:
            self._output_callback_statuses += 1

        if self._stop.is_set() or not self._udp_ready.is_set():
            outdata.fill(0.0)
            return

        if int(frames) != int(FRAME_SAMPLES):
            self._output_frame_mismatches += 1
            outdata.fill(0.0)
            return

        if self._playout_ring.pop_into(self._output_work) is None:
            outdata.fill(0.0)
            self._out_vu_dbfs = -60.0
            return

        np.multiply(self._output_work, db_to_linear(self._output_gain_db), out=self._output_work)
        np.clip(self._output_work, -1.0, 1.0, out=self._output_work)
        self._out_vu_dbfs = rms_dbfs(self._output_work)
        np.copyto(outdata[:, 0], self._output_work, casting="same_kind")
