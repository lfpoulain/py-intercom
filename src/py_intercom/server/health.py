"""Operator-facing health model of the server, derived from engine snapshots (no Qt)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class ServerHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"
    UNKNOWN = "unknown"


class ClientTransportState(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    ACTIVE = "active"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class HealthFacet:
    state: ServerHealthState
    label: str
    detail: str


@dataclass(frozen=True)
class ServerOperationalHealth:
    overall: HealthFacet
    engine: HealthFacet
    control: HealthFacet
    udp: HealthFacet
    audio: HealthFacet


_EXPECTED_SERVER_WORKERS = frozenset({"udp-rx", "udp-tx", "mix", "ctrl-accept"})


def _count(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def derive_client_transport_state(
    client: Mapping[str, object],
    *,
    udp_recent_threshold_s: float = 1.0,
) -> ClientTransportState:
    """Separate normal silence from an actual loss of live UDP audio.

    The hello declares the client's UDP endpoint, so a client whose control
    session is open always has one: there is no "waiting for UDP" state.
    """

    if client.get("control_connected") is not True:
        return ClientTransportState.OFFLINE

    ptt_buses_raw = client.get("ptt_buses")
    ptt_buses = ptt_buses_raw if isinstance(ptt_buses_raw, Mapping) else {}
    if not any(value is True for value in ptt_buses.values()):
        return ClientTransportState.IDLE

    try:
        age_s = float(client.get("age_s"))
    except (TypeError, ValueError):
        return ClientTransportState.DEGRADED
    if age_s <= float(udp_recent_threshold_s):
        return ClientTransportState.ACTIVE
    return ClientTransportState.DEGRADED


def derive_server_operational_health(
    stats: Mapping[str, object],
    clients: Mapping[int, Mapping[str, object]],
    outputs: Mapping[int, Mapping[str, object]],
    *,
    udp_recent_threshold_s: float = 1.0,
) -> ServerOperationalHealth:
    """Build a conservative operator view from the server health signals."""

    running = stats.get("running") is True
    worker_alive_raw = stats.get("worker_alive")
    worker_alive = worker_alive_raw if isinstance(worker_alive_raw, Mapping) else {}
    worker_errors_raw = stats.get("worker_errors")
    worker_errors = worker_errors_raw if isinstance(worker_errors_raw, Mapping) else {}
    dead_workers = sorted(name for name in _EXPECTED_SERVER_WORKERS if worker_alive.get(name) is not True)

    if not running:
        engine = HealthFacet(
            ServerHealthState.ERROR,
            "Moteur arrêté",
            "Le moteur ne signale plus son état running.",
        )
    elif worker_errors or dead_workers:
        details = []
        if dead_workers:
            details.append("workers absents : " + ", ".join(dead_workers))
        if worker_errors:
            details.append("erreurs : " + "; ".join(f"{name}: {error}" for name, error in worker_errors.items()))
        engine = HealthFacet(
            ServerHealthState.ERROR,
            "Moteur en erreur",
            "; ".join(details),
        )
    else:
        engine = HealthFacet(
            ServerHealthState.HEALTHY,
            "Moteur OK",
            "Tous les workers critiques sont actifs.",
        )

    try:
        connected_count = int(stats.get("control_connected_clients", 0))
    except (TypeError, ValueError):
        connected_count = 0
    control = HealthFacet(
        ServerHealthState.HEALTHY,
        _count(connected_count, "poste connecté", "postes connectés"),
        (
            f"{connected_count} client(s) avec contrôle TCP actif."
            if connected_count
            else "Canal de contrôle prêt, aucun client connecté."
        ),
    )

    # The UDP view is derived from the clients snapshot alone, never compared
    # with a count taken from another snapshot: that raced with every
    # connection and flashed false alerts.
    associated_udp = 0
    active_udp = 0
    stale_active_udp = 0
    for client in clients.values():
        transport_state = derive_client_transport_state(
            client,
            udp_recent_threshold_s=udp_recent_threshold_s,
        )
        if transport_state is ClientTransportState.OFFLINE:
            continue
        associated_udp += 1
        if transport_state is ClientTransportState.ACTIVE:
            active_udp += 1
        elif transport_state is ClientTransportState.DEGRADED:
            stale_active_udp += 1

    if stale_active_udp > 0:
        udp = HealthFacet(
            ServerHealthState.DEGRADED,
            f"Voix reçue : {associated_udp - stale_active_udp}/{associated_udp} postes",
            f"{stale_active_udp} client(s) en parole n'envoient plus d'audio récent.",
        )
    else:
        udp = HealthFacet(
            ServerHealthState.HEALTHY,
            "Voix reçue OK" if associated_udp else "Réception prête",
            (
                (
                    f"{associated_udp} client(s) associé(s), dont {active_udp} en parole ; "
                    "les clients silencieux au repos sont nominaux."
                )
                if associated_udp
                else "Transport UDP prêt, aucun client connecté."
            ),
        )

    explicit_output_errors = []
    inactive_outputs = []
    unknown_outputs = []
    for output_id, output in outputs.items():
        error = str(output.get("stream_error") or output.get("last_error") or "")
        if error:
            explicit_output_errors.append(f"sortie {output_id}: {error}")
        if "stream_active" not in output:
            unknown_outputs.append(int(output_id))
        elif output.get("stream_active") is not True:
            inactive_outputs.append(int(output_id))

    return_enabled = stats.get("return_enabled") is True
    return_active_known = "return_stream_active" in stats
    return_inactive = return_enabled and stats.get("return_stream_active") is not True
    return_error = str(stats.get("return_stream_error") or "") if return_enabled else ""
    if not outputs:
        audio = HealthFacet(
            ServerHealthState.DEGRADED,
            "Aucune sortie son",
            "Aucune sortie audio n'est configurée.",
        )
    elif explicit_output_errors or inactive_outputs or return_inactive or return_error:
        details = list(explicit_output_errors)
        if inactive_outputs:
            details.append("streams inactifs : " + ", ".join(str(i) for i in inactive_outputs))
        if return_inactive:
            details.append("entrée Return inactive")
        if return_error:
            details.append(f"Return : {return_error}")
        audio = HealthFacet(
            ServerHealthState.ERROR,
            "Sortie son en erreur",
            "; ".join(details),
        )
    elif unknown_outputs or (return_enabled and not return_active_known):
        audio = HealthFacet(
            ServerHealthState.UNKNOWN,
            _count(len(outputs), "sortie son", "sorties son"),
            "L'activité de certains streams audio n'est pas encore confirmée.",
        )
    else:
        audio = HealthFacet(
            ServerHealthState.HEALTHY,
            _count(len(outputs), "sortie son OK", "sorties son OK"),
            "Tous les streams audio configurés sont actifs.",
        )

    facets = (engine, control, udp, audio)
    if any(facet.state is ServerHealthState.ERROR for facet in facets):
        overall = HealthFacet(
            ServerHealthState.ERROR,
            "Erreur opérationnelle",
            "Au moins un composant critique est en erreur.",
        )
    elif any(facet.state in {ServerHealthState.DEGRADED, ServerHealthState.UNKNOWN} for facet in facets):
        overall = HealthFacet(
            ServerHealthState.DEGRADED,
            "En service dégradé",
            "Le serveur fonctionne, mais toute la chaîne audio n'est pas confirmée.",
        )
    else:
        overall = HealthFacet(
            ServerHealthState.HEALTHY,
            "En service",
            "Moteur, transport et sorties audio opérationnels.",
        )
    return ServerOperationalHealth(overall, engine, control, udp, audio)
