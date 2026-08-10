from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal


PRESET_SCHEMA_VERSION = 2
PresetType = Literal["client", "server"]


class PresetSchemaError(ValueError):
    """Raised when a preset cannot safely be interpreted by this release."""


@dataclass(frozen=True, slots=True)
class PresetMigrationResult:
    data: dict[str, Any]
    source_version: int
    migrated: bool


def _schema_version(data: dict[str, Any], preset_type: PresetType) -> int:
    raw = data.get("schema_version")
    if raw is None:
        # Historical server presets carried ``version: 1`` while client
        # presets had no marker at all.  Both layouts are the V1 schema.
        if preset_type == "server" and "version" in data:
            raw = data.get("version")
        else:
            return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PresetSchemaError("schema_version must be an integer")
    if raw < 1:
        raise PresetSchemaError("schema_version must be positive")
    return int(raw)


def migrate_preset(data: object, *, preset_type: PresetType) -> PresetMigrationResult:
    """Validate and upgrade a client or server preset without mutating it.

    Unknown keys are deliberately preserved so an older optional preference
    is never discarded merely because the current GUI does not expose it.
    Presets written by a newer application are rejected rather than
    downgraded and overwritten.
    """

    if preset_type not in ("client", "server"):
        raise ValueError("preset_type must be 'client' or 'server'")
    if not isinstance(data, dict):
        raise PresetSchemaError("JSON root must be an object")

    source_version = _schema_version(data, preset_type)
    if source_version > PRESET_SCHEMA_VERSION:
        raise PresetSchemaError(
            "preset schema is newer than this application "
            f"({source_version} > {PRESET_SCHEMA_VERSION})"
        )

    declared_type = data.get("preset_type")
    if declared_type is not None and declared_type != preset_type:
        raise PresetSchemaError(
            f"preset_type is {declared_type!r}; expected {preset_type!r}"
        )

    migrated = copy.deepcopy(data)
    if preset_type == "server":
        # Remove the ambiguous historical marker.  Protocol and application
        # versions must not be confused with the persistence schema.
        migrated.pop("version", None)
    migrated["preset_type"] = preset_type
    migrated["schema_version"] = int(PRESET_SCHEMA_VERSION)

    return PresetMigrationResult(
        data=migrated,
        source_version=int(source_version),
        migrated=(
            source_version != PRESET_SCHEMA_VERSION
            or declared_type != preset_type
            or migrated != data
        ),
    )


def stamp_preset(data: object, *, preset_type: PresetType) -> dict[str, Any]:
    """Return a write-ready snapshot using the current schema."""

    return migrate_preset(data, preset_type=preset_type).data
