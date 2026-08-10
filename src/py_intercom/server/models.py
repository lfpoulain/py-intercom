from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.constants import MAX_GAIN_DB


def _profile_gain(value: Any) -> float:
    try:
        gain = float(value)
    except (TypeError, ValueError):
        gain = 0.0
    return max(-60.0, min(float(MAX_GAIN_DB), gain))


@dataclass
class ClientProfile:
    """Persistent client identity and operator-controlled preferences.

    Audio codecs, jitter buffers, sockets and liveness belong to ``ClientState``
    and are deliberately absent here.  Loading a preset can therefore restore
    known operators without allocating any live audio resources.
    """

    client_uuid: str
    name: str = ""
    input_gain_db: float = 0.0

    @classmethod
    def from_preset(cls, client_uuid: str, value: Any) -> "ClientProfile":
        raw = value if isinstance(value, dict) else {}
        return cls(
            client_uuid=str(client_uuid),
            name=str(raw.get("name") or ""),
            input_gain_db=_profile_gain(raw.get("input_gain_db", 0.0)),
        )

    def to_preset(self) -> dict:
        return {
            "name": str(self.name or ""),
            "input_gain_db": _profile_gain(self.input_gain_db),
        }

    def copy(self) -> "ClientProfile":
        return ClientProfile(
            client_uuid=str(self.client_uuid),
            name=str(self.name or ""),
            input_gain_db=_profile_gain(self.input_gain_db),
        )
