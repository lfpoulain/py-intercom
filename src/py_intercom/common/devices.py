from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, List, Optional

import sounddevice as sd


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int


def list_devices(
    hostapi_substring: Optional[str] = "WASAPI",
    *,
    hard_refresh: bool = False,
    validate: bool = False,
) -> List[DeviceInfo]:
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

        if validate:
            try:
                max_in = int(dev.get("max_input_channels", 0))
                max_out = int(dev.get("max_output_channels", 0))
                sr = float(dev.get("default_samplerate", 0.0) or 0.0)
                ok = False

                if max_out > 0 and sr > 0:
                    try:
                        sd.check_output_settings(device=idx, channels=min(1, max_out), samplerate=sr)
                        ok = True
                    except Exception:
                        pass

                if max_in > 0 and sr > 0:
                    try:
                        sd.check_input_settings(device=idx, channels=min(1, max_in), samplerate=sr)
                        ok = True
                    except Exception:
                        pass

                if not ok:
                    continue
            except Exception:
                continue

        out.append(
            DeviceInfo(
                index=idx,
                name=str(dev.get("name", idx)),
                hostapi=hostapi_name,
                max_input_channels=int(dev.get("max_input_channels", 0)),
                max_output_channels=int(dev.get("max_output_channels", 0)),
            )
        )

    return out


def format_devices(devices: Iterable[DeviceInfo]) -> str:
    lines = []
    for d in devices:
        lines.append(
            f"{d.index:>3} | {d.hostapi} | in:{d.max_input_channels} out:{d.max_output_channels} | {d.name}"
        )
    return "\n".join(lines)
