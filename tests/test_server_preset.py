import json

import numpy as np
import pytest

import py_intercom.common.opus_codec as opus_codec_mod
import py_intercom.server.models as models_mod
import py_intercom.server.presets as presets_mod
import py_intercom.server.server as server_mod
from py_intercom.common.constants import FRAME_SAMPLES
from py_intercom.common.control_protocol import CONTROL_PROTOCOL_VERSION, encode_control_message
from py_intercom.common.identity import client_id_from_uuid
from py_intercom.common.preset_schema import PRESET_SCHEMA_VERSION


class _FakeEncoder:
    def encode(self, frame):
        return b"opus"


class _FakeDecoder:
    def decode(self, payload):
        return np.zeros((FRAME_SAMPLES,), dtype=np.float32)


class _ControlConnection:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []

    def settimeout(self, _timeout):
        return None

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(bytes(data))

    def close(self):
        return None


def _make_server(monkeypatch, preset_path):
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", lambda *args, **kwargs: _FakeEncoder())
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", lambda *args, **kwargs: _FakeDecoder())
    monkeypatch.setattr(presets_mod, "list_devices", lambda *args, **kwargs: [])
    return server_mod.IntercomServer(
        port=0,
        preset_path=str(preset_path),
        discovery_enabled=False,
    )


def test_save_preset_does_not_hold_server_lock_while_listing_devices(monkeypatch):
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", lambda *args, **kwargs: _FakeEncoder())
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", lambda *args, **kwargs: _FakeDecoder())

    srv = server_mod.IntercomServer(
        port=0,
        preset_path="server_preset.json",
        discovery_enabled=False,
    )
    acquired_lock = []
    written = []

    def fake_list_devices(*args, **kwargs):
        acquired = srv._lock.acquire(blocking=False)
        acquired_lock.append(bool(acquired))
        if acquired:
            srv._lock.release()
        return []

    def fake_atomic_write_json(path, data):
        written.append((path, data))

    monkeypatch.setattr(presets_mod, "list_devices", fake_list_devices)
    monkeypatch.setattr(server_mod, "atomic_write_json", fake_atomic_write_json)

    try:
        srv.save_preset()
    finally:
        srv.stop()

    assert acquired_lock == [True]
    assert len(written) == 1
    assert written[0][1]["preset_type"] == "server"
    assert written[0][1]["schema_version"] == PRESET_SCHEMA_VERSION
    assert "version" not in written[0][1]


def test_loading_profiles_does_not_create_runtime_audio_clients(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    client_uuid = "72c315cb-b21f-4a3f-a498-fc0b951b7fd7"
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "outputs": [],
                "buses": {},
                "clients": {
                    client_uuid: {"name": "Camera 1", "input_gain_db": 7.0},
                },
            }
        ),
        encoding="utf-8",
    )
    srv = _make_server(monkeypatch, preset_path)

    try:
        assert srv.load_preset() is True
        assert srv._clients == {}
        assert list(srv._client_profiles) == [client_uuid]
        assert srv._preset_loaded_source_version == 1

        snapshot = srv.get_clients_snapshot()
        remembered = snapshot[client_id_from_uuid(client_uuid)]
        assert remembered["name"] == "Camera 1"
        assert remembered["input_gain_db"] == 7.0
        assert remembered["control_connected"] is False
        assert remembered["addr"] is None
    finally:
        srv.stop()


def test_hello_applies_persisted_profile_without_reusing_fake_session(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    client_uuid = "b076734f-eebc-4e67-8ebd-e988040cb4cc"
    client_id = client_id_from_uuid(client_uuid)
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "outputs": [],
                "buses": {},
                "clients": {
                    client_uuid: {"name": "Old name", "input_gain_db": 5.0},
                },
            }
        ),
        encoding="utf-8",
    )
    srv = _make_server(monkeypatch, preset_path)
    hello = encode_control_message(
        {
            "type": "hello",
            "version": CONTROL_PROTOCOL_VERSION,
            "client_id": client_id,
            "client_uuid": client_uuid,
            "name": "Camera renamed",
            "udp_port": 50123,
        }
    )
    conn = _ControlConnection([hello, b""])

    try:
        srv.load_preset()
        srv._ctrl_session_loop(conn, ("127.0.0.1", 42000))

        state = srv._clients[client_id]
        assert state.name == "Camera renamed"
        assert state.input_gain_db == 5.0
        assert srv._client_profiles[client_uuid].name == "Camera renamed"
        assert conn.sent
    finally:
        srv.stop()


def test_corrupt_preset_blocks_all_automatic_overwrites(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    corrupted = "{ definitely not json"
    preset_path.write_text(corrupted, encoding="utf-8")
    srv = _make_server(monkeypatch, preset_path)

    try:
        with pytest.raises(models_mod.PresetLoadError) as raised:
            srv.load_preset()
        assert raised.value.path == preset_path
        assert raised.value.backup_path == preset_path.with_suffix(".json.bak")

        srv._autosave_preset()
        with pytest.raises(models_mod.PresetLoadError):
            srv.save_preset()
    finally:
        srv.stop()

    assert preset_path.read_text(encoding="utf-8") == corrupted


@pytest.mark.parametrize(
    "incompatible",
    [
        {"preset_type": "server", "schema_version": PRESET_SCHEMA_VERSION + 1},
        {"preset_type": "client", "schema_version": PRESET_SCHEMA_VERSION},
    ],
)
def test_incompatible_server_preset_is_never_downgraded_or_overwritten(
    monkeypatch,
    tmp_path,
    incompatible,
):
    preset_path = tmp_path / "server-preset.json"
    original = json.dumps(incompatible)
    preset_path.write_text(original, encoding="utf-8")
    srv = _make_server(monkeypatch, preset_path)

    try:
        with pytest.raises(models_mod.PresetLoadError) as raised:
            srv.load_preset()
        assert raised.value.status is models_mod.JsonFileStatus.INVALID
        srv._autosave_preset()
        with pytest.raises(models_mod.PresetLoadError):
            srv.save_preset()
    finally:
        srv.stop()

    assert preset_path.read_text(encoding="utf-8") == original


def test_profile_crc_collision_is_preserved_but_exposed_as_unsafe(monkeypatch, tmp_path):
    first_uuid = "CbqlANX5SvlhQR9k"
    second_uuid = "S2gCFl89L6spbARQ"
    client_id = client_id_from_uuid(first_uuid)
    assert client_id == client_id_from_uuid(second_uuid)
    preset_path = tmp_path / "server-preset.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "clients": {
                    first_uuid: {"name": "First"},
                    second_uuid: {"name": "Second"},
                },
            }
        ),
        encoding="utf-8",
    )
    srv = _make_server(monkeypatch, preset_path)

    try:
        assert srv.load_preset() is True
        assert set(srv._client_profiles) == {first_uuid, second_uuid}
        snapshot = srv.get_clients_snapshot()[client_id]
        assert snapshot["identity_collision"] is True
        assert snapshot["identity_collision_uuids"] == sorted(
            [first_uuid, second_uuid]
        )
        assert srv.get_stats_snapshot()["identity_collision_count"] == 1
    finally:
        srv.stop()
