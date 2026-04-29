import json
import time

from py_intercom.common.discovery import (
    DiscoveredServer,
    DiscoveryListener,
    _BEACON_TYPE,
    _BEACON_VERSION,
)
from py_intercom.common.constants import AUDIO_UDP_PORT, CONTROL_PORT_OFFSET


def _make_beacon(server_name="srv", audio_port=AUDIO_UDP_PORT, version=_BEACON_VERSION) -> bytes:
    return json.dumps(
        {
            "type": _BEACON_TYPE,
            "server_name": server_name,
            "audio_port": audio_port,
            "control_port": audio_port + CONTROL_PORT_OFFSET,
            "version": version,
        }
    ).encode("utf-8")


def test_handle_valid_beacon_adds_server():
    listener = DiscoveryListener()
    listener._handle_beacon(_make_beacon("alpha"), "10.0.0.5")
    servers = listener.get_servers()
    key = f"10.0.0.5:{AUDIO_UDP_PORT}"
    assert key in servers
    s = servers[key]
    assert isinstance(s, DiscoveredServer)
    assert s.server_name == "alpha"
    assert s.audio_port == AUDIO_UDP_PORT
    assert s.control_port == AUDIO_UDP_PORT + CONTROL_PORT_OFFSET


def test_handle_invalid_json_ignored():
    listener = DiscoveryListener()
    listener._handle_beacon(b"\xff\xff not json", "10.0.0.6")
    assert listener.get_servers() == {}


def test_handle_wrong_type_ignored():
    listener = DiscoveryListener()
    bad = json.dumps({"type": "something-else", "server_name": "x"}).encode("utf-8")
    listener._handle_beacon(bad, "10.0.0.7")
    assert listener.get_servers() == {}


def test_rename_triggers_update_callback():
    received = []
    listener = DiscoveryListener(on_update=lambda servers: received.append(dict(servers)))
    listener._handle_beacon(_make_beacon("alpha"), "10.0.0.5")
    listener._handle_beacon(_make_beacon("alpha"), "10.0.0.5")  # same -> no callback
    listener._handle_beacon(_make_beacon("beta"), "10.0.0.5")  # rename -> callback
    assert len(received) == 2  # initial add + rename


def test_expiry_drops_stale_entries():
    listener = DiscoveryListener(expiry_s=0.01)
    listener._handle_beacon(_make_beacon("alpha"), "10.0.0.5")
    assert len(listener.get_servers()) == 1
    time.sleep(0.05)
    assert listener.get_servers() == {}
