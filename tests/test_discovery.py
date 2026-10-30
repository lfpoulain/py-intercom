import json
import socket
import time

import pytest

from py_intercom.common import discovery
from py_intercom.common.constants import AUDIO_UDP_PORT, CONTROL_PORT_OFFSET
from py_intercom.common.discovery import (
    _BEACON_TYPE,
    _BEACON_VERSION,
    DiscoveredServer,
    DiscoveryBeacon,
    DiscoveryListener,
)


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


def test_server_seen_through_several_interfaces_is_listed_once():
    beacon = DiscoveryBeacon(server_name="Régie")._build_payload()
    listener = DiscoveryListener(expiry_s=0.05)

    listener._handle_beacon(beacon, "10.0.0.10")
    listener._handle_beacon(beacon, "172.28.176.1")  # Hyper-V adapter of the same PC
    servers = listener.get_servers()

    assert [server.ip for server in servers.values()] == ["10.0.0.10"]

    time.sleep(0.1)  # 10.0.0.10 gone: the other interface takes over
    listener._handle_beacon(beacon, "172.28.176.1")
    assert [server.ip for server in listener.get_servers().values()] == ["172.28.176.1"]
    assert listener.get_servers().keys() == servers.keys()


def test_distinct_servers_on_one_address_are_kept_apart():
    listener = DiscoveryListener()
    first = json.loads(_make_beacon("Régie"))
    other = {**first, "server_name": "Plateau", "audio_port": AUDIO_UDP_PORT + 10}

    listener._handle_beacon(json.dumps({**first, "instance": "a"}).encode(), "10.0.0.5")
    listener._handle_beacon(json.dumps({**first, "instance": "b"}).encode(), "10.0.0.6")
    listener._handle_beacon(json.dumps({**other, "instance": "a"}).encode(), "10.0.0.5")

    assert len(listener.get_servers()) == 3


def test_handle_invalid_json_ignored():
    listener = DiscoveryListener()
    listener._handle_beacon(b"\xff\xff not json", "10.0.0.6")
    assert listener.get_servers() == {}


def test_handle_wrong_type_ignored():
    listener = DiscoveryListener()
    bad = json.dumps({"type": "something-else", "server_name": "x"}).encode("utf-8")
    listener._handle_beacon(bad, "10.0.0.7")
    assert listener.get_servers() == {}


def test_handle_malformed_beacon_fields_ignored():
    listener = DiscoveryListener()
    bad = json.dumps(
        {
            "type": _BEACON_TYPE,
            "server_name": "broken",
            "audio_port": "not-a-port",
            "control_port": AUDIO_UDP_PORT + CONTROL_PORT_OFFSET,
            "version": _BEACON_VERSION,
        }
    ).encode("utf-8")
    listener._handle_beacon(bad, "10.0.0.8")
    assert listener.get_servers() == {}


def test_handle_out_of_range_beacon_ports_ignored():
    listener = DiscoveryListener()
    bad = json.dumps(
        {
            "type": _BEACON_TYPE,
            "server_name": "broken",
            "audio_port": 70000,
            "control_port": AUDIO_UDP_PORT + CONTROL_PORT_OFFSET,
            "version": _BEACON_VERSION,
        }
    ).encode("utf-8")
    listener._handle_beacon(bad, "10.0.0.9")
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


def test_malformed_beacons_are_counted_not_raised():
    listener = DiscoveryListener()
    out_of_range = json.loads(_make_beacon(audio_port=70000))

    listener._handle_beacon(b"\xff\xff not json", "10.0.0.6")
    listener._handle_beacon(b"[" * 4000, "10.0.0.6")  # deeper than the JSON parser allows
    listener._handle_beacon(json.dumps({**out_of_range, "version": float("inf")}).encode(), "10.0.0.6")
    listener._handle_beacon(json.dumps(out_of_range).encode(), "10.0.0.6")
    listener._handle_beacon(json.dumps({"type": "another-app"}).encode(), "10.0.0.6")

    assert listener.invalid_beacons == 4  # another application's packet is not an error
    assert listener.get_servers() == {}


def test_update_callback_errors_do_not_stop_discovery():
    def broken(_servers):
        raise RuntimeError("GUI gone")

    listener = DiscoveryListener(on_update=broken)
    listener._handle_beacon(_make_beacon("alpha"), "10.0.0.5")

    assert len(listener.get_servers()) == 1


# --- Beacon sender ------------------------------------------------------------


@pytest.mark.parametrize("bind_ip", ["192.168.10.2", " 192.168.10.2 "])
def test_beacon_sends_only_from_the_bound_interface(monkeypatch, bind_ip):
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["10.0.0.2", "192.168.10.2"])

    assert DiscoveryBeacon(bind_ip=bind_ip)._source_addresses() == ["192.168.10.2"]


@pytest.mark.parametrize("bind_ip", ["", "0.0.0.0"])
def test_unbound_beacon_sends_from_every_interface(monkeypatch, bind_ip):
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ["10.0.0.2", "192.168.10.2"])

    assert DiscoveryBeacon(bind_ip=bind_ip)._source_addresses() == ["10.0.0.2", "192.168.10.2"]


def test_beacon_falls_back_to_an_unbound_socket_without_known_interfaces(monkeypatch):
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: [])

    assert DiscoveryBeacon()._source_addresses() == [""]


def test_beacon_sockets_follow_interface_changes(monkeypatch):
    addresses = ["127.0.0.1"]
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: list(addresses))
    beacon = DiscoveryBeacon()
    sockets = {}
    try:
        beacon._sync_sockets(sockets)
        first = sockets["127.0.0.1"]
        assert first.getsockname()[0] == "127.0.0.1"

        addresses.clear()  # interface gone
        beacon._sync_sockets(sockets)

        assert list(sockets) == [""]
        assert first.fileno() == -1  # closed
    finally:
        for sock in sockets.values():
            sock.close()


def test_local_ipv4_addresses_are_filtered_deduplicated_and_sorted(monkeypatch):
    def fake_getaddrinfo(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", (address, 0))
            for address in ("192.168.1.4", "127.0.0.1", "10.0.0.2", "192.168.1.4")
        ]

    monkeypatch.setattr(discovery.socket, "getaddrinfo", fake_getaddrinfo)

    assert discovery.local_ipv4_addresses() == ["10.0.0.2", "192.168.1.4"]


def test_local_ipv4_addresses_lookup_failure_returns_nothing(monkeypatch):
    def failing_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("host not found")

    monkeypatch.setattr(discovery.socket, "getaddrinfo", failing_getaddrinfo)

    assert discovery.local_ipv4_addresses() == []
