"""Sound card side of the client: PortAudio streams and their realtime callbacks."""

from __future__ import annotations

import math

import numpy as np
import sounddevice as sd
from loguru import logger

from ..common.audio_errors import (
    friendly_audio_open_error,
    shared_stream_extra_settings,
)
from ..common.buses import FIXED_BUS_IDS
from ..common.constants import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
)


class AudioIOMixin:
    @staticmethod
    def _rms_dbfs_realtime(frame: np.ndarray, floor_db: float = -60.0) -> float:
        if int(frame.size) == 0:
            return float(floor_db)
        mean_square = float(np.dot(frame, frame)) / float(frame.size)
        if not math.isfinite(mean_square) or mean_square <= 0.0:
            return float(floor_db)
        db = 10.0 * math.log10(mean_square)
        return max(float(floor_db), min(0.0, float(db)))

    @staticmethod
    def _query_audio_device(device: int | None) -> dict | None:
        if device is None:
            return None
        try:
            info = sd.query_devices(int(device))
        except Exception:
            return None
        return info if isinstance(info, dict) else None

    def _start_audio_stream(self, stream, *, kind: str, device: int | None) -> None:
        try:
            stream.start()
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind=kind,
                dev_info=self._query_audio_device(device),
                device=device,
                sr=int(SAMPLE_RATE),
                ch=1,
            )
            raise RuntimeError(msg) from e

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

    def _open_input_stream(self) -> sd.InputStream:
        dev_info = self._query_audio_device(self.config.input_device)

        if dev_info is not None:
            logger.debug(
                "input device: name={} hostapi={} max_in={} default_sr={}",
                dev_info.get("name"),
                dev_info.get("hostapi"),
                dev_info.get("max_input_channels"),
                dev_info.get("default_samplerate"),
            )

        sr = int(SAMPLE_RATE)
        ch = 1
        blocksize = int(FRAME_SAMPLES)
        extra_settings = shared_stream_extra_settings(dev_info)

        try:
            sd.check_input_settings(
                device=self.config.input_device,
                channels=ch,
                dtype="float32",
                samplerate=sr,
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=self.config.input_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        try:
            logger.debug(
                "opening InputStream: dev={} ch={} blocksize={} latency=low sr={}",
                str(self.config.input_device),
                ch,
                blocksize,
                sr,
            )
            st = sd.InputStream(
                samplerate=sr,
                channels=ch,
                dtype="float32",
                blocksize=blocksize,
                device=self.config.input_device,
                latency="low",
                extra_settings=extra_settings,
                callback=self._in_callback,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="input",
                dev_info=dev_info,
                device=self.config.input_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        with self._state_lock:
            self._in_channels = ch
            self._in_samplerate = int(sr)
        return st

    def _open_output_stream(self) -> sd.OutputStream:
        dev_info = self._query_audio_device(self.config.output_device)

        if dev_info is not None:
            logger.debug(
                "output device: name={} hostapi={} max_out={} default_sr={}",
                dev_info.get("name"),
                dev_info.get("hostapi"),
                dev_info.get("max_output_channels"),
                dev_info.get("default_samplerate"),
            )

        sr = int(SAMPLE_RATE)
        ch = 1
        blocksize = int(FRAME_SAMPLES)
        extra_settings = shared_stream_extra_settings(dev_info)

        try:
            sd.check_output_settings(
                device=self.config.output_device,
                channels=ch,
                dtype="float32",
                samplerate=sr,
                extra_settings=extra_settings,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev_info,
                device=self.config.output_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        try:
            logger.debug(
                "opening OutputStream: dev={} ch={} blocksize={} latency=low sr={}",
                str(self.config.output_device),
                ch,
                blocksize,
                sr,
            )
            st = sd.OutputStream(
                samplerate=sr,
                channels=ch,
                dtype="float32",
                blocksize=blocksize,
                device=self.config.output_device,
                latency="low",
                extra_settings=extra_settings,
                callback=self._out_callback,
            )
        except Exception as e:
            msg = friendly_audio_open_error(
                e,
                kind="output",
                dev_info=dev_info,
                device=self.config.output_device,
                sr=sr,
                ch=ch,
            )
            raise RuntimeError(msg) from e

        with self._state_lock:
            self._out_channels = ch
            self._out_samplerate = sr
        return st

    def _in_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self._input_callback_statuses += 1

        if self._stop.is_set() or not self._udp_ready.is_set():
            with self._state_lock:
                self._in_vu_dbfs = -60.0
            return

        with self._state_lock:
            gain_db = float(self._input_gain_db)
            bus_mask = self._ptt_bus_mask(self._ptt_buses)

        if bus_mask == 0:
            with self._state_lock:
                self._in_vu_dbfs = -60.0
                for bus_id in FIXED_BUS_IDS:
                    bid = int(bus_id)
                    self._bus_vu_dbfs[bid] = max(
                        -60.0,
                        float(self._bus_vu_dbfs.get(bid, -60.0)) - 3.0,
                    )
            return

        if int(frames) != int(FRAME_SAMPLES):
            self._input_frame_mismatches += 1
            return

        if indata.ndim == 1 and int(indata.shape[0]) == int(FRAME_SAMPLES):
            source = indata
        elif (
            indata.ndim == 2
            and int(indata.shape[0]) == int(FRAME_SAMPLES)
            and int(indata.shape[1]) == 1
        ):
            source = indata[:, 0]
        else:
            self._input_frame_mismatches += 1
            return

        with self._tx_lock:
            session_token = int(self._session_token)
            session_epoch = int(self._audio_session_epoch)
        if session_token == 0:
            return

        np.copyto(self._capture_work, source, casting="same_kind")
        gain_linear = math.pow(10.0, gain_db / 20.0)
        np.multiply(self._capture_work, gain_linear, out=self._capture_work)
        np.absolute(self._capture_work, out=self._capture_abs_work)
        peak = float(np.max(self._capture_abs_work))
        if not math.isfinite(peak):
            self._capture_work.fill(0.0)
        elif peak > 1.0:
            np.multiply(self._capture_work, 0.99 / peak, out=self._capture_work)
        np.clip(self._capture_work, -1.0, 1.0, out=self._capture_work)
        vu = self._rms_dbfs_realtime(self._capture_work)

        with self._state_lock:
            self._in_vu_dbfs = vu
            for bus_id in FIXED_BUS_IDS:
                bid = int(bus_id)
                if bus_mask & (1 << bid):
                    self._bus_vu_dbfs[bid] = vu
                else:
                    self._bus_vu_dbfs[bid] = max(
                        -60.0,
                        float(self._bus_vu_dbfs.get(bid, -60.0)) - 3.0,
                    )

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

    def _out_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self._output_callback_statuses += 1

        if self._stop.is_set() or not self._udp_ready.is_set():
            outdata.fill(0.0)
            return

        with self._state_lock:
            out_sr = int(self._out_samplerate)
            out_gain_db = float(self._output_gain_db)

        if out_sr != int(SAMPLE_RATE) or int(frames) != int(FRAME_SAMPLES):
            self._output_frame_mismatches += 1
            outdata.fill(0.0)
            return

        metadata = self._playout_ring.pop_into(self._output_work)
        if metadata is None:
            self._playout_underflows += 1
            outdata.fill(0.0)
            with self._state_lock:
                self._out_vu_dbfs = -60.0
            return

        gain_linear = math.pow(10.0, out_gain_db / 20.0)
        np.multiply(self._output_work, gain_linear, out=self._output_work)
        np.clip(self._output_work, -1.0, 1.0, out=self._output_work)
        vu = self._rms_dbfs_realtime(self._output_work)

        with self._state_lock:
            self._out_vu_dbfs = vu

        if outdata.ndim == 1:
            np.copyto(outdata, self._output_work, casting="same_kind")
        elif outdata.ndim == 2 and int(outdata.shape[0]) == int(FRAME_SAMPLES):
            for channel in range(int(outdata.shape[1])):
                np.copyto(
                    outdata[:, channel],
                    self._output_work,
                    casting="same_kind",
                )
        else:
            self._output_frame_mismatches += 1
            outdata.fill(0.0)
