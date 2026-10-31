from __future__ import annotations

from py_intercom.server.health import ServerHealthState, derive_server_operational_health


def _stats(control_connected_clients: int) -> dict:
    return {
        "running": True,
        "worker_alive": {"udp-rx": True, "udp-tx": True, "mix": True, "ctrl-accept": True},
        "worker_errors": {},
        "control_connected_clients": control_connected_clients,
        "return_enabled": False,
    }


def _idle_client(port: int) -> dict:
    return {
        "control_connected": True,
        "addr": ("192.168.1.20", port),
        "age_s": 30.0,
        "ptt_buses": {},
    }


def test_client_connecting_between_two_snapshots_is_not_a_udp_alert() -> None:
    # The GUI takes the clients snapshot first and the stats a moment later:
    # a client that connects in between is counted only in the stats.
    health = derive_server_operational_health(
        _stats(control_connected_clients=2),
        {1: _idle_client(41000)},
        {0: {"stream_active": True}},
    )

    assert health.udp.state is ServerHealthState.HEALTHY
    assert health.overall.state is ServerHealthState.HEALTHY


def test_udp_label_counts_the_clients_whose_audio_is_fine() -> None:
    speaking_without_audio = {**_idle_client(41001), "ptt_buses": {0: True}}
    clients = {1: _idle_client(41000), 2: speaking_without_audio}

    health = derive_server_operational_health(_stats(2), clients, {0: {"stream_active": True}})

    assert health.udp.state is ServerHealthState.DEGRADED
    assert health.udp.label == "Voix reçue : 1/2 postes"
    assert "1 poste(s) en parole" in health.udp.detail


def test_no_output_is_a_valid_setup() -> None:
    health = derive_server_operational_health(_stats(0), {}, {})

    assert health.audio.state is ServerHealthState.HEALTHY
    assert health.overall.state is ServerHealthState.HEALTHY


def test_failed_return_is_reported_even_without_outputs() -> None:
    stats = {**_stats(0), "return_enabled": True, "return_stream_active": False}

    health = derive_server_operational_health(stats, {}, {})

    assert health.audio.state is ServerHealthState.ERROR
    assert health.audio.label == "Programme en erreur"


def test_output_numbers_match_the_strips() -> None:
    health = derive_server_operational_health(_stats(0), {}, {0: {"stream_active": False}})

    assert health.audio.detail == "sorties arrêtées : 1"
