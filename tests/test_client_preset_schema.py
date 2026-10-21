from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from PySide6 import QtGui

import py_intercom.common.theme as theme_mod
from py_intercom.client.gui import ClientWindow, new_client_preset
from py_intercom.common.jsonio import JsonFileStatus
from py_intercom.common.preset_schema import PRESET_SCHEMA_VERSION


def _load_fake(path):
    fake = SimpleNamespace(
        _preset_path=lambda: path,
        _preset_write_blocked=False,
        _preset_load_problem=None,
    )
    loaded = ClientWindow._load_preset(fake)
    return fake, loaded


def test_client_legacy_preset_is_migrated_only_in_memory(tmp_path) -> None:
    path = tmp_path / "client.json"
    legacy = {
        "client_uuid": "stable-client",
        "name": "Alice",
        "plugin_extension": {"enabled": True},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    fake, loaded = _load_fake(path)

    assert fake._preset_write_blocked is False
    assert loaded["preset_type"] == "client"
    assert loaded["schema_version"] == PRESET_SCHEMA_VERSION
    assert loaded["plugin_extension"] == {"enabled": True}
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


@pytest.mark.parametrize(
    "incompatible",
    [
        {
            "preset_type": "client",
            "schema_version": PRESET_SCHEMA_VERSION + 1,
            "name": "future",
        },
        {
            "preset_type": "server",
            "schema_version": PRESET_SCHEMA_VERSION,
            "name": "wrong type",
        },
    ],
)
def test_incompatible_client_preset_is_blocked_without_overwrite(
    tmp_path, incompatible
) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps(incompatible), encoding="utf-8")
    original = path.read_bytes()

    fake, loaded = _load_fake(path)
    fake._preset = {"client_uuid": "replacement"}
    fake._preset_writer = SimpleNamespace(
        request=lambda _data: pytest.fail("blocked preset must not be queued"),
        flush=lambda **_kwargs: pytest.fail("blocked preset must not be flushed"),
    )

    assert loaded == {}
    assert fake._preset_write_blocked is True
    assert fake._preset_load_problem[0] is JsonFileStatus.INVALID
    assert ClientWindow._preset_save(fake, flush=True) is False
    assert path.read_bytes() == original


def test_every_client_preset_write_is_stamped_and_preserves_unknown_keys(
    tmp_path,
) -> None:
    requested = []

    class Writer:
        def request(self, data) -> None:
            requested.append(copy.deepcopy(data))

        def flush(self, *, raise_on_error: bool) -> None:
            assert raise_on_error is True

    fake = SimpleNamespace(
        _preset_path=lambda: tmp_path / "client.json",
        _preset_write_blocked=False,
        _preset={"client_uuid": "abc", "plugin_extension": {"x": 1}},
        _preset_writer=Writer(),
    )

    assert ClientWindow._preset_save(fake, flush=True) is True
    assert requested == [fake._preset]
    assert requested[0]["preset_type"] == "client"
    assert requested[0]["schema_version"] == PRESET_SCHEMA_VERSION
    assert requested[0]["plugin_extension"] == {"x": 1}


def test_client_reset_preset_uses_current_schema() -> None:
    preset = new_client_preset("reset-uuid")

    assert preset["client_uuid"] == "reset-uuid"
    assert preset["preset_type"] == "client"
    assert preset["schema_version"] == PRESET_SCHEMA_VERSION


def test_unchanged_client_preference_does_not_schedule_an_automatic_rewrite() -> None:
    saves = []
    fake = SimpleNamespace(
        _preset={"input_device": 4},
        _preset_save=lambda: saves.append(True),
    )

    ClientWindow._preset_set(fake, "input_device", 4)

    assert saves == []


def test_normal_font_database_does_not_register_windows_fonts(monkeypatch) -> None:
    added = []
    monkeypatch.setattr(
        theme_mod.QtGui.QFontDatabase,
        "families",
        staticmethod(lambda: ["Arial"]),
    )
    monkeypatch.setattr(
        theme_mod.QtGui.QFontDatabase,
        "addApplicationFont",
        staticmethod(lambda path: added.append(path) or 0),
    )
    fake_app = SimpleNamespace(
        font=lambda: QtGui.QFont("Arial", 10),
        setFont=lambda _font: None,
    )

    assert theme_mod._ensure_ui_fonts(fake_app) == "Arial"
    assert added == []
