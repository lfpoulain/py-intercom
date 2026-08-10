from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from py_intercom.common.jsonio import JsonFileStatus
from py_intercom.common.preset_schema import PRESET_SCHEMA_VERSION
from py_intercom.server.gui import ServerWindow


def _read_fake(path):
    fake = SimpleNamespace(
        _preset_paths=lambda: path,
        _preset_load_problem=None,
        _preset_problem_notified=None,
        _show_server_preset_problem=lambda: None,
    )
    loaded = ServerWindow._read_server_preset(fake)
    return fake, loaded


def test_missing_server_gui_preset_uses_current_schema(tmp_path) -> None:
    fake, loaded = _read_fake(tmp_path / "missing.json")

    assert fake._preset_load_problem is None
    assert loaded == {
        "preset_type": "server",
        "schema_version": PRESET_SCHEMA_VERSION,
    }


def test_server_gui_legacy_preview_is_migrated_only_in_memory(tmp_path) -> None:
    path = tmp_path / "server.json"
    legacy = {
        "version": 1,
        "return_enabled": True,
        "plugin_extension": {"enabled": True},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    fake, loaded = _read_fake(path)

    assert fake._preset_load_problem is None
    assert loaded["preset_type"] == "server"
    assert loaded["schema_version"] == PRESET_SCHEMA_VERSION
    assert loaded["plugin_extension"] == {"enabled": True}
    assert "version" not in loaded
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


@pytest.mark.parametrize(
    "incompatible",
    [
        {
            "preset_type": "server",
            "schema_version": PRESET_SCHEMA_VERSION + 1,
        },
        {
            "preset_type": "client",
            "schema_version": PRESET_SCHEMA_VERSION,
        },
    ],
)
def test_server_gui_blocks_incompatible_preview_without_overwrite(
    tmp_path,
    incompatible,
) -> None:
    path = tmp_path / "server.json"
    path.write_text(json.dumps(incompatible), encoding="utf-8")
    original = path.read_bytes()

    fake, loaded = _read_fake(path)

    assert loaded is None
    assert fake._preset_load_problem[0] is JsonFileStatus.INVALID
    assert path.read_bytes() == original
