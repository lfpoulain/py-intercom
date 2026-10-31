from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import numpy as np
import pytest
from PySide6 import QtWidgets

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
    OnlineRecoveryHysteresis,
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


def test_server_snapshot_contract_exposes_ui_audio_health(monkeypatch, tmp_path) -> None:
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
    assert "PTT suspendu" in status.detail


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
    fake_client = SimpleNamespace(set_ptt_bus=lambda bus_id, active: sent.append((int(bus_id), bool(active))))
    fake_window = SimpleNamespace(
        _client=fake_client,
        _global_ptt=None,
        _ptt_bus_keys={0: object(), 1: object(), 2: object()},
        _ptt_bus_modes={0: "always_on", 1: "toggle", 2: "ptt"},
        _ptt_bus_toggle_state={1: True},
        _ptt_remote_active={2: True},  # a remote hold must not survive either
        _ptt_pad_held={2: True},  # nor a mouse hold on a talk pad
        _operational_status=ClientOperationalStatus(state, state.value, ""),
    )
    fake_window._ptt_bus_active = lambda bus_id: ClientWindow._ptt_bus_active(fake_window, bus_id)
    fake_window._apply_ptt_bus = lambda bus_id: ClientWindow._apply_ptt_bus(fake_window, bus_id)

    ClientWindow._apply_ptt_modes_to_client(fake_window)

    assert sent == [(0, False), (1, False), (2, False)]


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _status_window(*, start_fails: bool = False) -> SimpleNamespace:
    """Window stub recording when the global shortcuts are armed or disarmed."""
    window = SimpleNamespace(
        _client=None,
        _global_ptt=None,
        _ever_online=False,
        _ptt_start_failed=False,
        _operational_status=ClientOperationalStatus(ClientOperationalState.DISCONNECTED, "Déconnecté", ""),
        _status_label=QtWidgets.QLabel(),
        _connection_state_banner=QtWidgets.QLabel(),
        _refresh_state_labels=lambda: None,
        calls=[],
    )

    def start() -> None:
        if window._global_ptt is not None:
            return  # like the window: a running hook is kept
        window.calls.append("start")
        if start_fails:
            raise RuntimeError("hook refused")
        window._global_ptt = object()

    def disarm() -> None:
        window.calls.append("disarm")
        window._global_ptt = None

    window._start_global_ptt = start
    window._disarm_ptt_for_safety = disarm
    window._suspend_ptt = lambda: window.calls.append("suspend")
    window._apply_ptt_modes_to_client = lambda: window.calls.append("apply")
    return window


def _tick(window: SimpleNamespace, state: ClientOperationalState) -> None:
    ClientWindow._set_operational_status(window, ClientOperationalStatus(state, state.value, ""))


def test_shortcuts_are_never_armed_while_not_online(qt_app) -> None:
    window = _status_window()

    for state in (
        ClientOperationalState.CONNECTING,
        ClientOperationalState.CONNECTING,
        ClientOperationalState.RECONNECTING,
        ClientOperationalState.DEGRADED,
        ClientOperationalState.DEGRADED,
        ClientOperationalState.DISCONNECTED,
    ):
        _tick(window, state)

    assert "start" not in window.calls and "apply" not in window.calls
    assert window._global_ptt is None
    assert window._ever_online is False


def test_shortcuts_are_armed_once_online_and_disarmed_once_on_loss(qt_app) -> None:
    window = _status_window()

    for state in (
        ClientOperationalState.CONNECTING,
        ClientOperationalState.ONLINE,
        ClientOperationalState.ONLINE,
        ClientOperationalState.RECONNECTING,
        ClientOperationalState.RECONNECTING,
        ClientOperationalState.ONLINE,
    ):
        _tick(window, state)

    assert window.calls == ["start", "apply", "disarm", "start", "apply"]
    assert window._ever_online is True


def test_udp_gap_suspends_ptt_without_restarting_the_shortcuts(qt_app) -> None:
    window = _status_window()

    for state in (
        ClientOperationalState.ONLINE,
        ClientOperationalState.DEGRADED,
        ClientOperationalState.DEGRADED,
        ClientOperationalState.ONLINE,
    ):
        _tick(window, state)

    assert window.calls == ["start", "apply", "suspend", "apply"]
    hook = window._global_ptt

    _tick(window, ClientOperationalState.DEGRADED)
    _tick(window, ClientOperationalState.RECONNECTING)

    assert hook is not None
    assert window.calls[-2:] == ["suspend", "disarm"]
    assert window._global_ptt is None


def _client_status(state: ClientOperationalState) -> ClientOperationalStatus:
    return ClientOperationalStatus(state, state.value, "")


def test_degraded_client_comes_back_online_only_once_stable() -> None:
    gate = OnlineRecoveryHysteresis(hold_s=0.5)
    online = _client_status(ClientOperationalState.ONLINE)
    degraded = _client_status(ClientOperationalState.DEGRADED)
    previous = ClientOperationalState.DEGRADED

    assert gate.apply(online, previous=previous, now=10.0).state is ClientOperationalState.DEGRADED
    assert gate.apply(online, previous=previous, now=10.3).state is ClientOperationalState.DEGRADED
    # A relapse restarts the wait.
    assert gate.apply(degraded, previous=previous, now=10.4) is degraded
    assert gate.apply(online, previous=previous, now=10.6).state is ClientOperationalState.DEGRADED
    assert gate.apply(online, previous=previous, now=11.0).state is ClientOperationalState.DEGRADED
    assert gate.apply(online, previous=previous, now=11.2) is online


@pytest.mark.parametrize(
    "previous",
    [ClientOperationalState.CONNECTING, ClientOperationalState.RECONNECTING, ClientOperationalState.ONLINE],
)
def test_coming_online_after_connecting_is_not_delayed(previous) -> None:
    online = _client_status(ClientOperationalState.ONLINE)

    assert OnlineRecoveryHysteresis().apply(online, previous=previous, now=0.0) is online


def test_shortcut_start_failure_is_reported_as_degraded_and_not_retried(qt_app) -> None:
    window = _status_window(start_fails=True)

    _tick(window, ClientOperationalState.CONNECTING)
    _tick(window, ClientOperationalState.ONLINE)
    _tick(window, ClientOperationalState.ONLINE)

    assert window.calls == ["start", "disarm"]
    assert window._ptt_start_failed is True
    assert window._operational_status.state is ClientOperationalState.DEGRADED
    assert "Raccourcis PTT indisponibles" in window._operational_status.detail


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
    assert "ne parle pas n'envoie rien" in health.udp.detail


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
