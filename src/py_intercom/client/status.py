"""Operator-facing connection state of the client, derived from engine snapshots (no Qt)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

# A degraded chain must stay healthy this long before PTT is armed again, so a
# link flapping around the UDP threshold cannot switch the microphones on and
# off at every refresh.
ONLINE_RECOVERY_HOLD_S = 0.5


class ClientOperationalState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    ONLINE = "online"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"


@dataclass(frozen=True)
class ClientOperationalStatus:
    state: ClientOperationalState
    label: str
    detail: str


def _workers_are_alive(value: object) -> bool:
    if isinstance(value, Mapping):
        return bool(value) and all(bool(alive) for alive in value.values())
    return value is True


def derive_client_operational_status(
    *,
    session_requested: bool,
    ever_online: bool,
    stats: Mapping[str, object],
    udp_recent_threshold_s: float = 1.0,
) -> ClientOperationalStatus:
    """Derive the operator-facing state from independent control/audio signals."""

    if not session_requested:
        return ClientOperationalStatus(
            ClientOperationalState.DISCONNECTED,
            "Déconnecté",
            "Aucune session intercom demandée.",
        )

    control_connected = stats.get("control_connected") is True
    welcome_received = stats.get("control_welcome_received") is True
    control_ready = stats.get("control_ready") is True

    if not (control_connected and welcome_received and control_ready):
        missing = []
        if not control_connected:
            missing.append("contrôle TCP")
        elif not welcome_received:
            missing.append("accueil serveur")
        elif not control_ready:
            missing.append("configuration audio")
        state = ClientOperationalState.RECONNECTING if ever_online else ClientOperationalState.CONNECTING
        return ClientOperationalStatus(
            state,
            "Reconnexion…" if ever_online else "Connexion…",
            f"En attente : {', '.join(missing)}.",
        )

    workers_ok = _workers_are_alive(stats.get("network_workers_alive"))
    input_stream_ok = stats.get("in_stream_active") is True
    output_stream_ok = stats.get("out_stream_active") is True
    try:
        udp_age_s = float(stats.get("udp_rx_age_s"))
    except (TypeError, ValueError):
        udp_age_s = None
    udp_ok = udp_age_s is not None and udp_age_s <= float(udp_recent_threshold_s)

    failures = []
    if not workers_ok:
        failures.append("workers réseau indisponibles")
    if not input_stream_ok:
        failures.append("micro inactif")
    if not output_stream_ok:
        failures.append("casque inactif")
    if not udp_ok:
        if udp_age_s is None:
            failures.append("aucun audio UDP reçu")
        else:
            failures.append(f"audio UDP ancien ({udp_age_s:.1f} s)")

    if failures:
        return ClientOperationalStatus(
            ClientOperationalState.DEGRADED,
            "Dégradé",
            "; ".join(failures) + ". PTT désarmé.",
        )

    return ClientOperationalStatus(
        ClientOperationalState.ONLINE,
        "En ligne",
        "Contrôle, transport UDP et flux audio opérationnels.",
    )


class OnlineRecoveryHysteresis:
    """Keeps a DEGRADED client degraded until its chain has been healthy for a while.

    Only the return from DEGRADED is delayed: going degraded, and coming
    online after (re)connecting, are reported at once.
    """

    def __init__(self, hold_s: float = ONLINE_RECOVERY_HOLD_S) -> None:
        self._hold_s = float(hold_s)
        self._healthy_since: float | None = None

    def apply(
        self,
        status: ClientOperationalStatus,
        *,
        previous: ClientOperationalState,
        now: float,
    ) -> ClientOperationalStatus:
        if status.state is not ClientOperationalState.ONLINE or previous is not ClientOperationalState.DEGRADED:
            self._healthy_since = None
            return status
        if self._healthy_since is None:
            self._healthy_since = float(now)
        if float(now) - self._healthy_since >= self._hold_s:
            self._healthy_since = None
            return status
        return ClientOperationalStatus(
            ClientOperationalState.DEGRADED,
            "Dégradé",
            "Liaison rétablie, vérification de sa stabilité. PTT suspendu.",
        )
