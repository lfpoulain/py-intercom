from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import py_intercom.common.opus_codec as opus_codec_mod
import py_intercom.server.server as server_mod
from py_intercom.client.gui import (
    CLIENT_STATE_REFRESH_INTERVAL_MS,
    CLIENT_VU_REFRESH_INTERVAL_MS,
    ClientWindow,
)
from py_intercom.client.status import (
    ClientOperationalState,
    ClientOperationalStatus,
    derive_client_operational_status,
)
from py_intercom.server.gui import (
    SERVER_STRUCTURE_REFRESH_INTERVAL_MS,
    SERVER_VU_REFRESH_INTERVAL_MS,
)
from py_intercom.server.health import (
    ClientTransportState,
    ServerHealthState,
    derive_client_transport_state,
    derive_server_operational_health,
)


def test_vu_refresh_is_decoupled_from_slower_structural_updates() -> None:
    assert CLIENT_VU_REFRESH_INTERVAL_MS < CLIENT_STATE_REFRESH_INTERVAL_MS
    assert SERVER_VU_REFRESH_INTERVAL_MS < SERVER_STRUCTURE_REFRESH_INTERVAL_MS
    assert 50 <= CLIENT_VU_REFRESH_INTERVAL_MS <= 67
    assert 50 <= SERVER_VU_REFRESH_INTERVAL_MS <= 67


def test_server_snapshot_contract_exposes_ui_audio_health(
    monkeypatch, tmp_path
) -> None:
    class Encoder:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def encode(self, frame) -> bytes:
            return b"opus"

    class Decoder:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def decode(self, payload) -> np.ndarray:
            return np.zeros((480,), dtype=np.float32)

    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", Encoder)
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", Decoder)
    server = server_mod.IntercomServer(
        bind_ip="127.0.0.1",
        port=0,
        outputs=[{"device": 0, "bus_id": 1}],
        preset_path=str(tmp_path / "server.json"),
        discovery_enabled=False,
    )
    try:
        output = next(iter(server.get_outputs_snapshot().values()))
        stats = server.get_stats_snapshot()
        assert {"stream_active", "stream_error"} <= output.keys()
        assert {"return_stream_active", "return_stream_error"} <= stats.keys()
    finally:
        server.stop()


def _healthy_client_stats() -> dict:
    return {
        "control_connected": True,
        "control_welcome_received": True,
        "control_ready": True,
        "network_workers_alive": True,
        "in_stream_active": True,
        "out_stream_active": True,
        "udp_rx_age_s": 0.02,
    }


def test_client_cannot_be_online_before_welcome_and_audio_readiness() -> None:
    stats = _healthy_client_stats()
    stats["control_welcome_received"] = False

    status = derive_client_operational_status(
        session_requested=True,
        ever_online=False,
        stats=stats,
    )

    assert status.state is ClientOperationalState.CONNECTING


def test_client_online_requires_all_positive_health_signals() -> None:
    status = derive_client_operational_status(
        session_requested=True,
        ever_online=False,
        stats=_healthy_client_stats(),
    )

    assert status.state is ClientOperationalState.ONLINE


def test_client_stale_udp_is_degraded_and_ptt_must_remain_disarmed() -> None:
    stats = _healthy_client_stats()
    stats["udp_rx_age_s"] = 3.0

    status = derive_client_operational_status(
        session_requested=True,
        ever_online=True,
        stats=stats,
    )

    assert status.state is ClientOperationalState.DEGRADED
    assert "PTT désarmé" in status.detail


def test_client_control_loss_after_online_is_reconnecting() -> None:
    stats = _healthy_client_stats()
    stats["control_connected"] = False

    status = derive_client_operational_status(
        session_requested=True,
        ever_online=True,
        stats=stats,
    )

    assert status.state is ClientOperationalState.RECONNECTING


def test_client_missing_stream_health_never_fails_open_to_online() -> None:
    stats = _healthy_client_stats()
    stats.pop("out_stream_active")

    status = derive_client_operational_status(
        session_requested=True,
        ever_online=False,
        stats=stats,
    )

    assert status.state is ClientOperationalState.DEGRADED


@pytest.mark.parametrize(
    "state",
    [
        ClientOperationalState.DISCONNECTED,
        ClientOperationalState.CONNECTING,
        ClientOperationalState.DEGRADED,
        ClientOperationalState.RECONNECTING,
    ],
)
def test_every_non_online_ui_state_forces_all_ptt_routes_off(state) -> None:
    sent: list[tuple[int, bool]] = []
    fake_client = SimpleNamespace(
        set_ptt_bus=lambda bus_id, active: sent.append((int(bus_id), bool(active)))
    )
    fake_window = SimpleNamespace(
        _client=fake_client,
        _global_ptt=None,
        _ptt_bus_keys={0: object(), 1: object(), 2: object()},
        _ptt_bus_modes={0: "always_on", 1: "toggle", 2: "ptt"},
        _ptt_bus_toggle_state={1: True},
        _operational_status=ClientOperationalStatus(state, state.value, ""),
    )

    ClientWindow._apply_ptt_modes_to_client(fake_window)

    assert sent == [(0, False), (1, False), (2, False)]


def _healthy_server_stats() -> dict:
    return {
        "running": True,
        "worker_alive": {
            "udp-rx": True,
            "udp-tx": True,
            "mix": True,
            "ctrl-accept": True,
        },
        "worker_errors": {},
        "control_connected_clients": 1,
        "clients_waiting_udp": 0,
        "return_enabled": False,
    }


def _healthy_server_clients() -> dict:
    return {
        42: {
            "control_connected": True,
            "addr": ("127.0.0.1", 5000),
            "age_s": 0.02,
            "ptt_buses": {},
        }
    }


def test_server_is_healthy_only_when_workers_udp_and_outputs_are_healthy() -> None:
    health = derive_server_operational_health(
        _healthy_server_stats(),
        _healthy_server_clients(),
        {0: {"stream_active": True}},
    )

    assert health.overall.state is ServerHealthState.HEALTHY
    assert health.engine.state is ServerHealthState.HEALTHY
    assert health.udp.state is ServerHealthState.HEALTHY
    assert health.audio.state is ServerHealthState.HEALTHY


def test_silent_idle_client_with_old_udp_is_healthy() -> None:
    clients = _healthy_server_clients()
    clients[42]["age_s"] = 300.0

    health = derive_server_operational_health(
        _healthy_server_stats(),
        clients,
        {0: {"stream_active": True}},
    )

    assert derive_client_transport_state(clients[42]) is ClientTransportState.IDLE
    assert health.udp.state is ServerHealthState.HEALTHY
    assert health.overall.state is ServerHealthState.HEALTHY
    assert "silencieux au repos" in health.udp.detail


def test_client_speaking_without_recent_udp_is_degraded() -> None:
    clients = _healthy_server_clients()
    clients[42]["ptt_buses"] = {0: True}
    clients[42]["age_s"] = 3.0

    health = derive_server_operational_health(
        _healthy_server_stats(),
        clients,
        {0: {"stream_active": True}},
    )

    assert derive_client_transport_state(clients[42]) is ClientTransportState.DEGRADED
    assert health.udp.state is ServerHealthState.DEGRADED
    assert health.overall.state is ServerHealthState.DEGRADED
    assert "en parole" in health.udp.detail


def test_client_speaking_with_recent_udp_is_active_and_healthy() -> None:
    clients = _healthy_server_clients()
    clients[42]["ptt_buses"] = {0: True}

    health = derive_server_operational_health(
        _healthy_server_stats(),
        clients,
        {0: {"stream_active": True}},
    )

    assert derive_client_transport_state(clients[42]) is ClientTransportState.ACTIVE
    assert health.udp.state is ServerHealthState.HEALTHY
    assert health.overall.state is ServerHealthState.HEALTHY


def test_server_dead_worker_is_a_visible_operational_error() -> None:
    stats = _healthy_server_stats()
    stats["worker_alive"]["mix"] = False

    health = derive_server_operational_health(
        stats,
        _healthy_server_clients(),
        {0: {"stream_active": True}},
    )

    assert health.engine.state is ServerHealthState.ERROR
    assert health.overall.state is ServerHealthState.ERROR
    assert "mix" in health.engine.detail


def test_server_waiting_udp_is_degraded_not_green() -> None:
    stats = _healthy_server_stats()
    stats["clients_waiting_udp"] = 1

    health = derive_server_operational_health(
        stats,
        {42: {"control_connected": True, "addr": None, "age_s": None}},
        {0: {"stream_active": True}},
    )

    assert health.udp.state is ServerHealthState.DEGRADED
    assert health.overall.state is ServerHealthState.DEGRADED


def test_server_explicitly_inactive_output_is_an_error() -> None:
    health = derive_server_operational_health(
        _healthy_server_stats(),
        _healthy_server_clients(),
        {0: {"stream_active": False}},
    )

    assert health.audio.state is ServerHealthState.ERROR
    assert health.overall.state is ServerHealthState.ERROR


def test_server_missing_stream_signal_is_conservatively_degraded() -> None:
    health = derive_server_operational_health(
        _healthy_server_stats(),
        _healthy_server_clients(),
        {0: {}},
    )

    assert health.audio.state is ServerHealthState.UNKNOWN
    assert health.overall.state is ServerHealthState.DEGRADED
