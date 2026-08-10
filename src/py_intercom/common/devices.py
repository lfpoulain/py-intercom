from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Iterable, List, Literal, Optional

import sounddevice as sd

from .audio_errors import shared_stream_extra_settings
from .constants import CHANNELS, SAMPLE_RATE


_DEVICE_ENUMERATION_LOCK = threading.RLock()


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: int = 0
    input_compatible: Optional[bool] = None
    output_compatible: Optional[bool] = None

    @property
    def supports_input(self) -> bool:
        return bool(
            int(self.max_input_channels) > 0
            and self.input_compatible is not False
        )

    @property
    def supports_output(self) -> bool:
        return bool(
            int(self.max_output_channels) > 0
            and self.output_compatible is not False
        )


DeviceDirection = Literal["input", "output"]


def _supports_direction(device: DeviceInfo, direction: Optional[DeviceDirection]) -> bool:
    if direction == "input":
        return bool(device.supports_input)
    if direction == "output":
        return bool(device.supports_output)
    return True


def format_device_label(device: DeviceInfo) -> str:
    """Return an unambiguous label for GUI device selectors.

    Bluetooth and Windows audio APIs regularly expose several endpoints with
    the same display name.  The PortAudio index and host API therefore belong
    in the visible label rather than only in the saved preset.
    """

    display_name = " ".join(str(device.name).split()) or "Périphérique audio"
    display_hostapi = " ".join(str(device.hostapi).split())
    parts = [display_name]
    if display_hostapi:
        parts.append(display_hostapi)
    parts.append(f"#{int(device.index)}")
    if int(device.default_samplerate) > 0:
        rate_khz = float(device.default_samplerate) / 1000.0
        parts.append(f"{rate_khz:g} kHz")
    return " — ".join(parts)


def list_devices(
    hostapi_substring: Optional[str] = "WASAPI",
    *,
    hard_refresh: bool = False,
    validate: bool = False,
) -> List[DeviceInfo]:
    # PortAudio exposes process-global state. In particular, the private
    # terminate/initialize refresh used on Windows must never overlap another
    # enumeration in this process.
    with _DEVICE_ENUMERATION_LOCK:
        if hard_refresh and os.name == "nt":
            try:
                sd._terminate()  # type: ignore[attr-defined]
                sd._initialize()  # type: ignore[attr-defined]
            except Exception:
                pass

        hostapis = sd.query_hostapis()
        devices = sd.query_devices()

        out: List[DeviceInfo] = []
        for idx, dev in enumerate(devices):
            hostapi_idx = int(dev.get("hostapi", -1))
            hostapi_name = ""
            if 0 <= hostapi_idx < len(hostapis):
                hostapi_name = str(hostapis[hostapi_idx].get("name", ""))

            if hostapi_substring is not None:
                if hostapi_substring.upper() not in hostapi_name.upper():
                    continue

            max_in = int(dev.get("max_input_channels", 0))
            max_out = int(dev.get("max_output_channels", 0))
            input_compatible: Optional[bool] = None
            output_compatible: Optional[bool] = None
            if validate:
                input_compatible = False
                output_compatible = False
                extra_settings = shared_stream_extra_settings(dev)

                # Validate each direction independently. A duplex endpoint can
                # support the engine format on playback but not on capture
                # (a common Bluetooth/driver case), and must not leak into the
                # wrong selector merely because its other direction works.
                if max_out >= int(CHANNELS):
                    try:
                        sd.check_output_settings(
                            device=idx,
                            channels=int(CHANNELS),
                            dtype="float32",
                            samplerate=float(SAMPLE_RATE),
                            extra_settings=extra_settings,
                        )
                        output_compatible = True
                    except Exception:
                        output_compatible = False

                if max_in >= int(CHANNELS):
                    try:
                        sd.check_input_settings(
                            device=idx,
                            channels=int(CHANNELS),
                            dtype="float32",
                            samplerate=float(SAMPLE_RATE),
                            extra_settings=extra_settings,
                        )
                        input_compatible = True
                    except Exception:
                        input_compatible = False

                if not bool(input_compatible or output_compatible):
                    continue

            try:
                default_samplerate = int(round(float(dev.get("default_samplerate", 0) or 0)))
            except (TypeError, ValueError, OverflowError):
                default_samplerate = 0

            out.append(
                DeviceInfo(
                    index=idx,
                    name=str(dev.get("name", idx)),
                    hostapi=hostapi_name,
                    max_input_channels=max_in,
                    max_output_channels=max_out,
                    default_samplerate=default_samplerate,
                    input_compatible=input_compatible,
                    output_compatible=output_compatible,
                )
            )

    return out


def resolve_device(
    saved_name: str,
    saved_hostapi: str,
    fallback_idx: Optional[int],
    devices: List[DeviceInfo],
    *,
    direction: Optional[DeviceDirection] = None,
) -> Optional[int]:
    if direction not in (None, "input", "output"):
        raise ValueError("direction must be 'input', 'output' or None")

    # PortAudio indexes are not stable across reboots, but while an index is
    # still present and agrees with the saved identity it is the only reliable
    # discriminator between duplicate endpoints (Bluetooth HFP input/output
    # often have exactly the same name and host API).
    if fallback_idx is not None:
        try:
            fallback_idx = int(fallback_idx)
        except (TypeError, ValueError, OverflowError):
            fallback_idx = None
    if fallback_idx is not None:
        for d in devices:
            if int(d.index) != int(fallback_idx) or not _supports_direction(d, direction):
                continue
            identity_matches = bool(
                (not saved_name or str(d.name) == str(saved_name))
                and (not saved_hostapi or str(d.hostapi) == str(saved_hostapi))
            )
            if identity_matches:
                return int(d.index)

    if saved_name and devices:
        matches = [
            d
            for d in devices
            if _supports_direction(d, direction)
            and str(d.name) == str(saved_name)
            and (not saved_hostapi or str(d.hostapi) == str(saved_hostapi))
        ]
        if len(matches) == 1:
            return int(matches[0].index)

        # An ambiguous match is safer than silently restoring the wrong side
        # of a headset. The GUI will leave the selector empty and ask the user
        # to choose explicitly.
        return None
    return None


def format_devices(devices: Iterable[DeviceInfo]) -> str:
    """Format an inventory for diagnostics and backwards compatibility."""

    lines = []
    for device in devices:
        lines.append(
            f"{device.index:>3} | {device.hostapi} | "
            f"in:{device.max_input_channels} out:{device.max_output_channels} | "
            f"{device.name}"
        )
    return "\n".join(lines)
