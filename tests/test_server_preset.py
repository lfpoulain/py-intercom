import json

import numpy as np
import pytest

import py_intercom.common.opus_codec as opus_codec_mod
import py_intercom.server.models as models_mod
import py_intercom.server.presets as presets_mod
import py_intercom.server.server as server_mod
from py_intercom.common.buses import PLATEAU_BUS_ID, VMIX_BUS_ID
from py_intercom.common.constants import FRAME_SAMPLES
from py_intercom.common.control_protocol import CONTROL_PROTOCOL_VERSION, encode_control_message
from py_intercom.common.devices import DeviceInfo
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
        assert snapshot["identity_collision_uuids"] == sorted([first_uuid, second_uuid])
        assert srv.get_stats_snapshot()["identity_collision_count"] == 1
    finally:
        srv.stop()


def _output_device(index: int, name: str) -> DeviceInfo:
    return DeviceInfo(
        index=index,
        name=name,
        hostapi="Windows WASAPI",
        max_input_channels=0,
        max_output_channels=2,
    )


def test_preset_gains_are_finite_and_bounded(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    # Python's json accepts NaN: a hand-edited preset can carry it.
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "buses": {"1": {"gain_db": 60.0}, "2": {"gain_db": float("nan")}},
                "outputs": [
                    {"device": 3, "device_name": "Scene", "bus_id": 2, "gain_db": -1000.0},
                    {"device": 3, "device_name": "Scene", "bus_id": 7},
                ],
            }
        ),
        encoding="utf-8",
    )
    srv = _make_server(monkeypatch, preset_path)
    monkeypatch.setattr(presets_mod, "list_devices", lambda *args, **kwargs: [_output_device(3, "Scene")])

    try:
        assert srv.load_preset() is True
        buses = srv.get_buses_snapshot()
        outputs = srv.get_outputs_snapshot()
    finally:
        srv.stop()

    assert buses[1]["gain_db"] == 20.0
    assert buses[2]["gain_db"] == 0.0
    assert [(o["device"], o["bus_id"], o["gain_db"]) for o in outputs.values()] == [
        (3, VMIX_BUS_ID, -60.0),
        (3, PLATEAU_BUS_ID, 0.0),
    ]


def test_preset_output_on_an_unknown_bus_survives_the_next_save(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "outputs": [
                    # Older presets could route an output to a custom bus.
                    {"device": 3, "device_name": "Scene", "bus_id": 5, "gain_db": -6.0},
                    {"device": 3, "device_name": "Scene", "bus_id": "vmix"},
                ],
            }
        ),
        encoding="utf-8",
    )
    srv = _make_server(monkeypatch, preset_path)
    monkeypatch.setattr(presets_mod, "list_devices", lambda *args, **kwargs: [_output_device(3, "Scene")])

    try:
        assert srv.load_preset() is True
        outputs = srv.get_outputs_snapshot()
        saved = srv._build_preset_data()["outputs"]
    finally:
        srv.stop()

    # Kept on Plateau rather than dropped, so saving cannot erase the physical
    # output; only an unreadable bus is skipped.
    assert [(o["device"], o["bus_id"], o["gain_db"]) for o in outputs.values()] == [(3, PLATEAU_BUS_ID, -6.0)]
    assert [(o["device"], o["bus_id"], o["gain_db"]) for o in saved] == [(3, PLATEAU_BUS_ID, -6.0)]


def test_loading_a_preset_stops_replaced_streams_outside_the_engine_lock(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    preset_path.write_text(json.dumps({"version": 1, "outputs": []}), encoding="utf-8")
    srv = _make_server(monkeypatch, preset_path)
    lock_free_during_stop = []

    class _Stream:
        closed = False

        def stop(self):
            acquired = srv._lock.acquire(blocking=False)
            if acquired:
                srv._lock.release()
            lock_free_during_stop.append(acquired)

        def close(self):
            self.closed = True

    stream = _Stream()
    replaced = models_mod.OutputState(0, 1, 1)
    replaced.stream = stream
    srv._outputs[0] = replaced

    try:
        srv.load_preset()
    finally:
        srv.stop()

    assert lock_free_during_stop == [True]
    assert stream.closed
    assert replaced.stream is None


def _absent_cards_preset(preset_path) -> None:
    preset_path.write_text(
        json.dumps(
            {
                "version": 1,
                "outputs": [
                    {"device": 3, "device_name": "Scene", "device_hostapi": "Windows WASAPI", "bus_id": 1},
                    # The USB interface of the stage return is off at boot.
                    {"device": 7, "device_name": "Retour USB", "device_hostapi": "ASIO", "bus_id": 2, "gain_db": -4},
                ],
                "return_enabled": True,
                "return_input_device": 9,
                "return_input_device_name": "Retour antenne",
                "return_input_device_hostapi": "Windows WASAPI",
            }
        ),
        encoding="utf-8",
    )


def test_outputs_and_return_of_absent_cards_survive_the_next_save(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    _absent_cards_preset(preset_path)
    srv = _make_server(monkeypatch, preset_path)
    monkeypatch.setattr(presets_mod, "list_devices", lambda *args, **kwargs: [_output_device(3, "Scene")])

    try:
        assert srv.load_preset() is True
        outputs = srv.get_outputs_snapshot()
        saved = srv._build_preset_data()
    finally:
        srv.stop()

    # Only the present card is opened, but nothing is erased from the preset.
    assert [(o["device"], o["bus_id"]) for o in outputs.values()] == [(3, PLATEAU_BUS_ID)]
    assert srv._return_input_device is None
    assert saved["outputs"] == [
        {"device": 3, "device_name": "Scene", "device_hostapi": "Windows WASAPI", "bus_id": 1, "gain_db": 0.0},
        {"device": 7, "device_name": "Retour USB", "device_hostapi": "ASIO", "bus_id": 2, "gain_db": -4.0},
    ]
    assert (
        saved["return_input_device"],
        saved["return_input_device_name"],
        saved["return_input_device_hostapi"],
    ) == (9, "Retour antenne", "Windows WASAPI")


def test_choosing_a_return_card_replaces_the_absent_one(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    _absent_cards_preset(preset_path)
    srv = _make_server(monkeypatch, preset_path)

    try:
        srv.load_preset()
        srv.set_return_input_device(4)
        saved = srv._build_preset_data()
    finally:
        srv.stop()

    assert (saved["return_input_device"], saved["return_input_device_name"]) == (4, "")


def test_preset_parsing_is_shared_and_reports_what_it_cannot_use():
    data = {
        "buses": {"x": {"gain_db": 1.0}, "9": {}, "1": {"gain_db": "loud", "feed_to_regie": True}},
        "clients": {"uuid-a": "not an object", "uuid-b": {"name": "Cam 2", "input_gain_db": 3.0}},
        "outputs": ["junk", {"device": "abc", "device_name": "Scene", "bus_id": "vmix"}, {"device": 3, "bus_id": 2}],
        "return_input_device": "nope",
    }

    parsed = presets_mod.parse_server_preset(data, [_output_device(3, "Scene")])

    assert parsed.bus_gain_db[PLATEAU_BUS_ID] == 0.0
    assert parsed.bus_feed_to_regie[PLATEAU_BUS_ID] is True
    assert {uuid: (p.name, p.input_gain_db) for uuid, p in parsed.profiles.items()} == {
        "uuid-a": ("", 0.0),
        "uuid-b": ("Cam 2", 3.0),
    }
    assert [(o.device, o.bus_id) for o in parsed.outputs] == [(3, VMIX_BUS_ID)]
    assert parsed.return_device is None
    assert not parsed.return_saved.is_set
    # Nothing is replaced by a default silently: every fallback names its field.
    assert parsed.warnings == (
        "Return input device index 'nope' ignored",
        "bus 'x' ignored: not a bus id",
        "bus '9' ignored: unknown bus or settings not an object",
        "bus 1 gain 'loud' corrected to 0.0 dB",
        "client uuid-a settings reset: not an object",
        "output 1 ignored: not an object",
        "output 2 ignored: invalid bus 'vmix'",
    )


def test_load_preset_logs_every_value_it_could_not_use(monkeypatch, tmp_path):
    preset_path = tmp_path / "server-preset.json"
    preset_path.write_text(json.dumps({"version": 1, "outputs": [{"device": "abc", "bus_id": 1}]}), encoding="utf-8")
    srv = _make_server(monkeypatch, preset_path)
    warnings = []
    monkeypatch.setattr(presets_mod.logger, "warning", lambda message, *args: warnings.append(message.format(*args)))

    try:
        srv.load_preset()
    finally:
        srv.stop()

    assert any("output 1 device index 'abc' ignored" in line for line in warnings)
