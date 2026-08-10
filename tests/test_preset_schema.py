from __future__ import annotations

import pytest

from py_intercom.common.preset_schema import (
    PRESET_SCHEMA_VERSION,
    PresetSchemaError,
    migrate_preset,
    stamp_preset,
)


def test_legacy_client_preset_is_copied_stamped_and_preserved() -> None:
    original = {"client_uuid": "abc", "future_optional_key": {"x": 1}}

    result = migrate_preset(original, preset_type="client")

    assert result.source_version == 1
    assert result.migrated is True
    assert result.data["preset_type"] == "client"
    assert result.data["schema_version"] == PRESET_SCHEMA_VERSION
    assert result.data["future_optional_key"] == {"x": 1}
    assert "schema_version" not in original
    result.data["future_optional_key"]["x"] = 2
    assert original["future_optional_key"] == {"x": 1}


def test_legacy_server_version_is_migrated_without_ambiguous_marker() -> None:
    result = migrate_preset(
        {"version": 1, "outputs": [], "clients": {}},
        preset_type="server",
    )

    assert result.source_version == 1
    assert "version" not in result.data
    assert result.data["preset_type"] == "server"
    assert result.data["schema_version"] == PRESET_SCHEMA_VERSION


def test_current_schema_is_stable() -> None:
    current = {
        "preset_type": "client",
        "schema_version": PRESET_SCHEMA_VERSION,
        "name": "Alice",
    }
    result = migrate_preset(current, preset_type="client")

    assert result.data == current
    assert result.migrated is False


@pytest.mark.parametrize("root", [None, [], "config"])
def test_non_object_root_is_rejected(root: object) -> None:
    with pytest.raises(PresetSchemaError, match="root"):
        migrate_preset(root, preset_type="client")


@pytest.mark.parametrize("version", [True, 0, -1, "2", 2.5])
def test_invalid_schema_version_is_rejected(version: object) -> None:
    with pytest.raises(PresetSchemaError, match="schema_version"):
        migrate_preset(
            {"preset_type": "client", "schema_version": version},
            preset_type="client",
        )


def test_newer_schema_is_rejected_instead_of_downgraded() -> None:
    with pytest.raises(PresetSchemaError, match="newer"):
        stamp_preset(
            {
                "preset_type": "server",
                "schema_version": PRESET_SCHEMA_VERSION + 1,
            },
            preset_type="server",
        )


def test_wrong_preset_type_is_rejected() -> None:
    with pytest.raises(PresetSchemaError, match="expected 'client'"):
        migrate_preset(
            {
                "preset_type": "server",
                "schema_version": PRESET_SCHEMA_VERSION,
            },
            preset_type="client",
        )


def test_unknown_requested_preset_type_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="preset_type"):
        migrate_preset({}, preset_type="other")  # type: ignore[arg-type]
