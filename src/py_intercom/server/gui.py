from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Mapping, Optional

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .server import IntercomServer, PresetLoadError
from ..common.buses import BUS_BY_ID, FIXED_BUSES, REGIE_BUS_ID
from ..common.devices import format_device_label, list_devices, resolve_device
from ..common.gui_utils import DeviceWorker, is_checked
from ..common.identity import client_id_from_uuid
from ..common.jsonio import JsonFileStatus, atomic_write_json, read_json_file_result
from ..common.preset_schema import PresetSchemaError, migrate_preset, stamp_preset
from ..common.constants import AUDIO_UDP_PORT, MAX_GAIN_DB
from ..common.theme import (
    EmptyStateTableWidget,
    VuMeter,
    StatusIndicator,
    apply_theme,
    make_field,
    patch_combo,
    centered_checkbox,
    cell_vu,
    set_status_label,
)


SERVER_STRUCTURE_REFRESH_INTERVAL_MS = 500
SERVER_VU_REFRESH_INTERVAL_MS = 67


class ServerHealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"
    UNKNOWN = "unknown"


class ClientTransportState(str, Enum):
    OFFLINE = "offline"
    WAITING_UDP = "waiting_udp"
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


def derive_client_transport_state(
    client: Mapping[str, object],
    *,
    udp_recent_threshold_s: float = 1.0,
) -> ClientTransportState:
    """Separate normal silence from an actual loss of live UDP audio."""

    if client.get("control_connected") is not True:
        return ClientTransportState.OFFLINE
    if not client.get("addr"):
        return ClientTransportState.WAITING_UDP

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
    dead_workers = sorted(
        name for name in _EXPECTED_SERVER_WORKERS if worker_alive.get(name) is not True
    )

    if not running:
        engine = HealthFacet(
            ServerHealthState.ERROR,
            "Moteur : arrêt",
            "Le moteur ne signale plus son état running.",
        )
    elif worker_errors or dead_workers:
        details = []
        if dead_workers:
            details.append("workers absents : " + ", ".join(dead_workers))
        if worker_errors:
            details.append(
                "erreurs : "
                + "; ".join(f"{name}: {error}" for name, error in worker_errors.items())
            )
        engine = HealthFacet(
            ServerHealthState.ERROR,
            "Moteur : erreur",
            "; ".join(details),
        )
    else:
        engine = HealthFacet(
            ServerHealthState.HEALTHY,
            "Moteur : OK",
            "Tous les workers critiques sont actifs.",
        )

    try:
        connected_count = int(stats.get("control_connected_clients", 0))
    except (TypeError, ValueError):
        connected_count = 0
    control = HealthFacet(
        ServerHealthState.HEALTHY,
        f"Ctrl : {connected_count}",
        (
            f"{connected_count} client(s) avec contrôle TCP actif."
            if connected_count
            else "Canal de contrôle prêt, aucun client connecté."
        ),
    )

    try:
        waiting_udp = int(stats.get("clients_waiting_udp", 0))
    except (TypeError, ValueError):
        waiting_udp = 0
    connected_snapshots = 0
    associated_udp = 0
    active_udp = 0
    stale_active_udp = 0
    for client in clients.values():
        if client.get("control_connected") is not True:
            continue
        connected_snapshots += 1
        transport_state = derive_client_transport_state(
            client,
            udp_recent_threshold_s=udp_recent_threshold_s,
        )
        if transport_state in {
            ClientTransportState.IDLE,
            ClientTransportState.ACTIVE,
            ClientTransportState.DEGRADED,
        }:
            associated_udp += 1
        if transport_state is ClientTransportState.ACTIVE:
            active_udp += 1
        elif transport_state is ClientTransportState.DEGRADED:
            stale_active_udp += 1

    missing_snapshots = max(0, connected_count - connected_snapshots)
    missing_associations = max(0, connected_count - associated_udp)
    if (
        waiting_udp > 0
        or missing_snapshots > 0
        or missing_associations > 0
        or stale_active_udp > 0
    ):
        details = []
        if waiting_udp > 0 or missing_associations > 0:
            details.append(
                f"{max(waiting_udp, missing_associations)} client(s) attendent l'association UDP"
            )
        if missing_snapshots > 0:
            details.append(f"{missing_snapshots} état(s) client manquant(s)")
        if stale_active_udp > 0:
            details.append(
                f"{stale_active_udp} client(s) en parole n'envoient plus d'audio récent"
            )
        udp = HealthFacet(
            ServerHealthState.DEGRADED,
            f"UDP : {associated_udp}/{connected_count}",
            "; ".join(details) + ".",
        )
    else:
        udp = HealthFacet(
            ServerHealthState.HEALTHY,
            f"UDP : {associated_udp}",
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
    return_error = (
        str(stats.get("return_stream_error") or "") if return_enabled else ""
    )
    if not outputs:
        audio = HealthFacet(
            ServerHealthState.DEGRADED,
            "Audio : aucune",
            "Aucune sortie audio n'est configurée.",
        )
    elif explicit_output_errors or inactive_outputs or return_inactive or return_error:
        details = list(explicit_output_errors)
        if inactive_outputs:
            details.append(
                "streams inactifs : " + ", ".join(str(i) for i in inactive_outputs)
            )
        if return_inactive:
            details.append("entrée Return inactive")
        if return_error:
            details.append(f"Return : {return_error}")
        audio = HealthFacet(
            ServerHealthState.ERROR,
            "Audio : erreur",
            "; ".join(details),
        )
    elif unknown_outputs or (return_enabled and not return_active_known):
        audio = HealthFacet(
            ServerHealthState.UNKNOWN,
            f"Audio : {len(outputs)}",
            "L'activité de certains streams audio n'est pas encore confirmée.",
        )
    else:
        audio = HealthFacet(
            ServerHealthState.HEALTHY,
            f"Audio : {len(outputs)} OK",
            "Tous les streams audio configurés sont actifs.",
        )

    facets = (engine, control, udp, audio)
    if any(facet.state is ServerHealthState.ERROR for facet in facets):
        overall = HealthFacet(
            ServerHealthState.ERROR,
            "Erreur opérationnelle",
            "Au moins un composant critique est en erreur.",
        )
    elif any(
        facet.state in {ServerHealthState.DEGRADED, ServerHealthState.UNKNOWN}
        for facet in facets
    ):
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



class ServerWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Py-Intercom Server")
        self.setMinimumSize(1040, 720)
        self.resize(1280, 820)
        self._set_app_icon()

        self._server: Optional[IntercomServer] = None
        self._settings = QtCore.QSettings("py-intercom", "server-gui")

        # Status bar
        self._status_bar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_label = QtWidgets.QLabel()
        self._status_bar.addWidget(self._status_label, 1)
        self._engine_health_label = QtWidgets.QLabel()
        self._control_health_label = QtWidgets.QLabel()
        self._udp_health_label = QtWidgets.QLabel()
        self._audio_health_label = QtWidgets.QLabel()
        for label in (
            self._engine_health_label,
            self._control_health_label,
            self._udp_health_label,
            self._audio_health_label,
        ):
            self._status_bar.addPermanentWidget(label)
        set_status_label(
            self._status_label,
            "Arrêté",
            "offline",
            detail="Serveur intercom arrêté.",
        )
        self._server_state_banner = QtWidgets.QLabel()
        self._server_state_banner.setProperty("role", "statusBanner")
        self._server_state_banner.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        set_status_label(
            self._server_state_banner,
            "Arrêté",
            "offline",
            detail="Serveur intercom arrêté.",
        )
        self._set_health_facets_unavailable()

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        # -- Server configuration --
        config_box = QtWidgets.QGroupBox("Configuration")
        config_box.setObjectName("serverConfigCard")
        config_lay = QtWidgets.QGridLayout(config_box)
        config_lay.setContentsMargins(16, 12, 16, 14)
        config_lay.setVerticalSpacing(10)
        config_lay.setHorizontalSpacing(14)

        self._server_name = QtWidgets.QLineEdit("py-intercom")
        self._server_name.setPlaceholderText("Server name")
        self._server_name.setMinimumWidth(180)
        self._server_name.setMaximumWidth(340)
        self._discovery_cb = QtWidgets.QCheckBox("Découverte réseau")
        self._discovery_cb.setChecked(True)
        self._discovery_cb.setToolTip("Broadcast LAN beacon so clients can auto-detect this server")
        self._autostart_cb = QtWidgets.QCheckBox("Démarrage automatique")
        self._autostart_cb.setChecked(bool(self._settings.value("autostart", False, type=bool)))
        self._start_minimized_cb = QtWidgets.QCheckBox("Démarrer réduit")
        self._start_minimized_cb.setChecked(bool(self._settings.value("start_minimized", False, type=bool)))

        self._return_enabled = QtWidgets.QCheckBox("Return")
        self._return_enabled.setChecked(False)
        self._return_input_device = QtWidgets.QComboBox()
        patch_combo(self._return_input_device)
        self._return_vu = VuMeter()
        self._regie_vu = VuMeter()
        self._show_all_devices = QtWidgets.QCheckBox("Tous les périphériques")
        self._show_all_devices.setChecked(
            bool(self._settings.value("show_all_devices", False, type=bool))
        )
        self._refresh_devices_btn = QtWidgets.QToolButton()
        self._refresh_devices_btn.setText("\u21bb")
        self._refresh_devices_btn.setProperty("class", "ghost")
        self._refresh_devices_btn.setFixedSize(34, 34)
        self._refresh_devices_btn.setAccessibleName("Actualiser les périphériques audio")
        self._refresh_devices_btn.setToolTip(
            "Actualiser les périphériques audio. Disponible uniquement serveur arrêté."
        )
        self._device_status = QtWidgets.QLabel("")

        self._start_btn = QtWidgets.QPushButton("▶  Démarrer")
        self._start_btn.setProperty("class", "success")
        self._start_btn.setFixedWidth(120)
        self._stop_btn = QtWidgets.QPushButton("■  Arrêter")
        self._stop_btn.setProperty("class", "danger")
        self._stop_btn.setFixedWidth(120)
        self._stop_btn.setEnabled(False)

        config_lay.addWidget(
            make_field("NOM DU SERVEUR", self._server_name, minimum_width=230),
            0,
            0,
            1,
            2,
        )
        config_lay.addWidget(self._discovery_cb, 1, 0)
        config_lay.addWidget(self._autostart_cb, 1, 1)
        config_lay.addWidget(self._start_minimized_cb, 2, 0, 1, 2)
        config_lay.setColumnStretch(0, 1)
        config_lay.setColumnStretch(1, 1)

        # -- Return bus + Régie group (combined) --
        return_box = QtWidgets.QGroupBox("Retour audio")
        return_box.setObjectName("returnCard")
        return_lay = QtWidgets.QVBoxLayout(return_box)
        return_lay.setContentsMargins(16, 12, 16, 14)
        return_lay.setSpacing(10)

        # Row 0 — Enable + Input device
        ret_input_lay = QtWidgets.QHBoxLayout()
        ret_input_lay.setSpacing(8)
        ret_input_lay.addWidget(self._return_enabled)
        ret_input_lay.addWidget(
            make_field("ENTRÉE RETURN", self._return_input_device),
            1,
        )
        return_lay.addLayout(ret_input_lay)

        # Row 1 — VU meters side by side
        vu_lay = QtWidgets.QHBoxLayout()
        vu_lay.setSpacing(12)
        lbl_ret = QtWidgets.QLabel("Return")
        lbl_ret.setFixedWidth(50)
        vu_lay.addWidget(lbl_ret)
        vu_lay.addWidget(self._return_vu, 1)
        lbl_regie = QtWidgets.QLabel(BUS_BY_ID[REGIE_BUS_ID].name)
        lbl_regie.setFixedWidth(50)
        vu_lay.addWidget(lbl_regie)
        vu_lay.addWidget(self._regie_vu, 1)
        return_lay.addLayout(vu_lay)

        # -- Clients group --
        clients_box = QtWidgets.QGroupBox("Équipe connectée")
        clients_box.setObjectName("clientsCard")
        clients_lay = QtWidgets.QVBoxLayout(clients_box)
        clients_lay.setContentsMargins(12, 10, 12, 12)
        clients_lay.setSpacing(8)

        # Base columns: 0=ClientID(hidden), 1=Name, 2=Addr(hidden), 3=Status, 4=Talk gain, 5=VU
        self._clients = EmptyStateTableWidget(
            0,
            6,
            "Aucun client connecté. Les postes apparaîtront ici dès leur connexion.",
        )
        self._clients.setHorizontalHeaderLabels(
            ["Client ID", "Nom", "Adresse", "État", "Gain micro", "VU"]
        )
        self._clients.setColumnHidden(0, True)
        self._clients.setColumnHidden(2, True)   # Addr hidden, shown in Info dialog
        self._clients.setMinimumHeight(180)
        hdr = self._clients.horizontalHeader()
        hdr.setDefaultSectionSize(60)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)   # Name
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)   # Status
        self._clients.setColumnWidth(3, 54)
        hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Fixed)   # Talk gain
        self._clients.setColumnWidth(4, 150)
        hdr.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Stretch)   # VU
        self._clients.verticalHeader().setDefaultSectionSize(32)
        self._clients.verticalHeader().setVisible(False)
        self._clients.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._clients.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._clients.setAlternatingRowColors(True)

        self._client_row_ids: list[int] = []
        self._client_uuid_by_id: Dict[int, str] = {}
        self._input_gain_widgets: Dict[int, QtWidgets.QSlider] = {}
        self._input_gain_labels: Dict[int, QtWidgets.QLabel] = {}
        self._vu_widgets: Dict[int, VuMeter] = {}
        self._status_widgets: Dict[int, StatusIndicator] = {}

        self._forget_client_btn = QtWidgets.QPushButton("Oublier le client")
        self._forget_client_btn.setProperty("class", "danger")
        self._forget_client_btn.setEnabled(False)
        self._forget_client_btn.clicked.connect(self._forget_selected_client)

        clients_bottom = QtWidgets.QHBoxLayout()
        clients_bottom.addWidget(self._forget_client_btn)
        clients_bottom.addStretch(1)

        clients_lay.addWidget(self._clients)
        clients_lay.addLayout(clients_bottom)

        # -- Outputs group --
        self._output_row_ids: list[int] = []
        self._output_device_widgets: Dict[int, QtWidgets.QComboBox] = {}
        self._output_bus_widgets: Dict[int, QtWidgets.QComboBox] = {}
        self._output_gain_widgets: Dict[int, QtWidgets.QSlider] = {}
        self._output_gain_labels: Dict[int, QtWidgets.QLabel] = {}
        self._output_vu_widgets: Dict[int, VuMeter] = {}
        self._output_status_widgets: Dict[int, StatusIndicator] = {}
        self._output_devices_cache: list[tuple[str, int]] = []
        self._bus_selector = QtWidgets.QComboBox()
        patch_combo(self._bus_selector)
        self._bus_selector_ids: list[int] = []
        self._bus_row_ids: list[int] = []
        self._bus_gain_widgets: Dict[int, QtWidgets.QSlider] = {}
        self._bus_gain_labels: Dict[int, QtWidgets.QLabel] = {}
        self._bus_feed_widgets: Dict[int, QtWidgets.QCheckBox] = {}

        self._buses = EmptyStateTableWidget(
            0,
            4,
            "Les bus disponibles apparaîtront au démarrage du serveur.",
        )
        self._buses.setHorizontalHeaderLabels(
            ["ID", "Nom", "Gain", f"Retour {BUS_BY_ID[REGIE_BUS_ID].name}"]
        )
        self._buses.verticalHeader().setVisible(False)
        self._buses.setMinimumHeight(180)
        self._buses.verticalHeader().setDefaultSectionSize(32)
        self._buses.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._buses.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._buses.setAlternatingRowColors(True)
        self._buses.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self._buses.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._buses.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self._buses.setColumnWidth(2, 160)
        self._buses.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

        self._add_output_device = QtWidgets.QComboBox()
        patch_combo(self._add_output_device)
        self._add_output_device.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._add_output_device.setMinimumContentsLength(40)
        self._add_output_btn = QtWidgets.QPushButton("Ajouter")
        self._add_output_btn.setProperty("class", "success")
        self._remove_output_btn = QtWidgets.QPushButton("Supprimer")
        self._remove_output_btn.setProperty("class", "danger")
        self._remove_output_btn.setEnabled(False)

        self._outputs = EmptyStateTableWidget(
            0,
            6,
            "Aucune sortie configurée. Sélectionnez un périphérique et un bus, puis ajoutez-les.",
        )
        self._outputs.setHorizontalHeaderLabels(
            ["ID", "Périphérique", "Bus", "Gain", "VU", "État"]
        )
        out_hdr = self._outputs.horizontalHeader()
        out_hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self._outputs.setColumnWidth(0, 30)
        out_hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        out_hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self._outputs.setColumnWidth(2, 75)
        out_hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self._outputs.setColumnWidth(3, 160)
        out_hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        out_hdr.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Fixed)
        self._outputs.setColumnWidth(5, 54)
        self._outputs.verticalHeader().setDefaultSectionSize(32)
        self._outputs.verticalHeader().setVisible(False)
        self._outputs.setMinimumHeight(210)
        self._outputs.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._outputs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._outputs.setAlternatingRowColors(True)
        self._outputs.itemSelectionChanged.connect(self._on_output_selection_changed)

        outputs_box = QtWidgets.QGroupBox("Routage audio")
        outputs_box.setObjectName("routingCard")
        outputs_layout = QtWidgets.QVBoxLayout(outputs_box)
        outputs_layout.setContentsMargins(12, 10, 12, 12)
        outputs_layout.setSpacing(8)

        buses_title = QtWidgets.QLabel("BUS DISPONIBLES")
        buses_title.setProperty("role", "fieldLabel")
        outputs_layout.addWidget(buses_title)
        outputs_layout.addWidget(self._buses)

        outputs_top = QtWidgets.QGridLayout()
        outputs_top.setHorizontalSpacing(10)
        outputs_top.addWidget(
            make_field("NOUVELLE SORTIE", self._add_output_device),
            0,
            0,
            1,
            3,
        )
        outputs_top.addWidget(make_field("BUS", self._bus_selector), 0, 3)
        outputs_top.addWidget(self._add_output_btn, 0, 4)
        outputs_top.addWidget(self._remove_output_btn, 0, 5)
        outputs_top.setColumnStretch(0, 1)
        outputs_layout.addLayout(outputs_top)
        outputs_title = QtWidgets.QLabel("DESTINATIONS ACTIVES")
        outputs_title.setProperty("role", "fieldLabel")
        outputs_layout.addWidget(outputs_title)
        outputs_layout.addWidget(self._outputs)

        # -- Info button --
        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("ℹ")
        self._info_btn.setProperty("class", "ghost")
        self._info_btn.setFixedSize(34, 34)
        self._info_btn.setToolTip("Info / Debug")
        self._info_btn.clicked.connect(self._show_geek_info)

        device_footer = QtWidgets.QHBoxLayout()
        device_footer.setSpacing(9)
        device_footer.addWidget(self._show_all_devices)
        device_footer.addWidget(self._refresh_devices_btn)
        self._device_status.setProperty("role", "muted")
        device_footer.addWidget(self._device_status, 1)
        outputs_layout.addLayout(device_footer)

        # -- Operational header --
        hero = QtWidgets.QFrame()
        hero.setProperty("role", "hero")
        hero_lay = QtWidgets.QHBoxLayout(hero)
        hero_lay.setContentsMargins(18, 14, 14, 14)
        hero_lay.setSpacing(12)
        hero_copy = QtWidgets.QVBoxLayout()
        hero_copy.setSpacing(1)
        eyebrow = QtWidgets.QLabel("PY-INTERCOM")
        eyebrow.setProperty("role", "heroEyebrow")
        title = QtWidgets.QLabel("Régie intercom")
        title.setProperty("role", "heroTitle")
        subtitle = QtWidgets.QLabel(
            "Démarrez le moteur, surveillez l’équipe et routez les retours audio."
        )
        subtitle.setProperty("role", "heroSubtitle")
        hero_copy.addWidget(eyebrow)
        hero_copy.addWidget(title)
        hero_copy.addWidget(subtitle)
        hero_lay.addLayout(hero_copy, 1)
        self._server_state_banner.setMinimumWidth(130)
        hero_lay.addWidget(self._server_state_banner)
        hero_lay.addWidget(self._stop_btn)
        hero_lay.addWidget(self._start_btn)
        hero_lay.addWidget(self._info_btn)

        # -- Two-column workspace --
        left_panel = QtWidgets.QWidget()
        left_panel.setObjectName("supervisionColumn")
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(config_box)
        left_layout.addWidget(return_box)
        left_layout.addWidget(clients_box, 1)

        right_panel = QtWidgets.QWidget()
        right_panel.setObjectName("routingColumn")
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(outputs_box, 1)

        self._workspace_splitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Horizontal
        )
        self._workspace_splitter.setObjectName("serverWorkspace")
        self._workspace_splitter.setAccessibleName(
            "Répartition supervision et routage audio"
        )
        self._workspace_splitter.setChildrenCollapsible(False)
        self._workspace_splitter.setHandleWidth(8)
        self._workspace_splitter.addWidget(left_panel)
        self._workspace_splitter.addWidget(right_panel)
        self._workspace_splitter.setStretchFactor(0, 4)
        self._workspace_splitter.setStretchFactor(1, 6)
        self._workspace_splitter.setSizes([470, 730])

        # -- Main layout --
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(hero)
        layout.addWidget(self._workspace_splitter, 1)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(self._on_show_all_devices_changed)
        self._start_btn.clicked.connect(self._start_server)
        self._stop_btn.clicked.connect(self._stop_server)
        self._add_output_btn.clicked.connect(self._on_add_output)
        self._remove_output_btn.clicked.connect(self._on_remove_output)
        self._return_enabled.stateChanged.connect(self._on_return_enabled_changed)
        self._return_input_device.currentIndexChanged.connect(self._on_return_input_device_changed)
        self._autostart_cb.stateChanged.connect(self._on_autostart_changed)
        self._start_minimized_cb.stateChanged.connect(self._on_start_minimized_changed)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(SERVER_STRUCTURE_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_clients)

        self._vu_timer = QtCore.QTimer(self)
        self._vu_timer.setInterval(SERVER_VU_REFRESH_INTERVAL_MS)
        self._vu_timer.timeout.connect(self._refresh_vu_and_health)

        self._device_thread: Optional[QtCore.QThread] = None
        self._device_worker: Optional[DeviceWorker] = None
        self._device_timeout: Optional[QtCore.QTimer] = None
        self._last_device_error: str = ""
        self._last_device_count: int = 0
        self._device_refresh_timed_out_flag: bool = False
        self._device_cursor_active: bool = False
        self._close_after_device_refresh: bool = False
        self._is_closing: bool = False
        self._preset_load_problem: Optional[tuple[JsonFileStatus, str]] = None
        self._preset_problem_notified: Optional[str] = None
        self._devices_cache: list[object] = []
        self._return_input_devices_cache: list[tuple[str, int]] = []

        # A preset may reference a DirectSound/MME endpoint that is invisible
        # under the default WASAPI-only filter. Restore the broader inventory
        # before the first refresh instead of losing that saved selection.
        if not self._show_all_devices.isChecked():
            preset = self._read_server_preset()
            hostapis: list[str] = []
            if isinstance(preset, dict):
                hostapis.append(str(preset.get("return_input_device_hostapi") or ""))
                for output in preset.get("outputs") or []:
                    if isinstance(output, dict):
                        hostapis.append(str(output.get("device_hostapi") or ""))
            if any(api and "WASAPI" not in api.upper() for api in hostapis):
                self._show_all_devices.blockSignals(True)
                try:
                    self._show_all_devices.setChecked(True)
                    self._settings.setValue("show_all_devices", True)
                finally:
                    self._show_all_devices.blockSignals(False)

        self._update_controls_for_server_state()

        QtCore.QTimer.singleShot(0, self._start_device_refresh)

    def _set_health_facets_unavailable(self) -> None:
        for label, text in (
            (self._engine_health_label, "Moteur : —"),
            (self._control_health_label, "Ctrl : —"),
            (self._udp_health_label, "UDP : —"),
            (self._audio_health_label, "Audio : —"),
        ):
            set_status_label(
                label,
                text,
                "unknown",
                detail="Information disponible lorsque le serveur est démarré.",
            )

    def _update_server_health(
        self,
        stats: Mapping[str, object],
        clients: Mapping[int, Mapping[str, object]],
        outputs: Mapping[int, Mapping[str, object]],
    ) -> None:
        health = derive_server_operational_health(stats, clients, outputs)
        set_status_label(
            self._status_label,
            f"{health.overall.label} — port {int(AUDIO_UDP_PORT)}",
            health.overall.state.value,
            detail=health.overall.detail,
        )
        set_status_label(
            self._server_state_banner,
            health.overall.label,
            health.overall.state.value,
            detail=health.overall.detail,
        )
        for label, facet in (
            (self._engine_health_label, health.engine),
            (self._control_health_label, health.control),
            (self._udp_health_label, health.udp),
            (self._audio_health_label, health.audio),
        ):
            set_status_label(
                label,
                facet.label,
                facet.state.value,
                detail=facet.detail,
            )

    def _set_app_icon(self) -> None:
        try:
            import sys as _sys
            _base = getattr(_sys, "_MEIPASS", None)
            if _base:
                icon_path = Path(_base) / "py_intercom" / "img" / "logo.ico"
                if not icon_path.is_file():
                    icon_path = Path(_base) / "img" / "logo.ico"
                if not icon_path.is_file():
                    icon_path = Path(_base) / "logo.ico"
            else:
                icon_path = Path(__file__).resolve().parents[1] / "img" / "logo.ico"
                if not icon_path.is_file():
                    icon_path = Path(__file__).resolve().parents[1] / "img" / "logo.png"
            if icon_path.is_file():
                icon = QtGui.QIcon(str(icon_path))
                self.setWindowIcon(icon)
                app = QtWidgets.QApplication.instance()
                if app is not None:
                    app.setWindowIcon(icon)
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("py-intercom.server")
            except Exception:
                pass
        except Exception:
            pass

    def _update_controls_for_server_state(self) -> None:
        running = self._server is not None

        self._server_name.setReadOnly(bool(running))
        self._discovery_cb.setEnabled(not bool(running))
        can_refresh = not bool(running) and getattr(self, "_device_thread", None) is None
        self._refresh_devices_btn.setEnabled(bool(can_refresh))
        self._show_all_devices.setEnabled(bool(can_refresh))

        self._add_output_device.setEnabled(bool(running))
        self._bus_selector.setEnabled(bool(running))
        can_add_output = bool(running) and self._add_output_device.currentData() is not None and self._bus_selector.currentData() is not None
        self._add_output_btn.setEnabled(bool(can_add_output))
        for slider in self._bus_gain_widgets.values():
            slider.setEnabled(bool(running))
        for slider in self._input_gain_widgets.values():
            slider.setEnabled(bool(running))
        for slider in self._output_gain_widgets.values():
            slider.setEnabled(bool(running))
        self._on_output_selection_changed()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._device_thread is not None and self._device_thread.isRunning():
            self._close_after_device_refresh = True
            self._device_thread.requestInterruption()
            self._device_status.setText("Waiting for device enumeration to finish before closing...")
            event.ignore()
            return
        self._is_closing = True
        self._stop_server()
        super().closeEvent(event)

    def _start_device_refresh(self) -> None:
        if self._is_closing:
            return
        if self._device_thread is not None:
            self._device_status.setText("Refreshing devices already in progress...")
            return
        if self._server is not None:
            self._device_status.setText("Stop the server before refreshing audio devices.")
            return

        self._refresh_devices_btn.setEnabled(False)
        self._show_all_devices.setEnabled(False)
        self._start_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WaitCursor))
        self._device_cursor_active = True

        hostapi_filter = None if self._show_all_devices.isChecked() else "WASAPI"

        self._device_status.setText(f"Refreshing devices ({hostapi_filter or 'ALL'})...")
        QtWidgets.QApplication.processEvents()
        self._last_device_error = ""
        self._last_device_count = 0
        self._device_refresh_timed_out_flag = False

        self._device_thread = QtCore.QThread(self)
        self._device_worker = DeviceWorker(hostapi_filter=hostapi_filter, hard_refresh=True)
        self._device_worker.moveToThread(self._device_thread)
        self._device_thread.started.connect(self._device_worker.run)
        self._device_worker.finished.connect(self._on_devices_refreshed)
        self._device_worker.finished.connect(self._device_thread.quit)
        self._device_worker.finished.connect(self._device_worker.deleteLater)
        self._device_thread.finished.connect(self._cleanup_device_refresh)
        self._device_thread.finished.connect(self._device_thread.deleteLater)

        self._device_timeout = QtCore.QTimer(self)
        self._device_timeout.setSingleShot(True)
        self._device_timeout.timeout.connect(self._device_refresh_timed_out)
        self._device_timeout.start(5000)

        self._device_thread.start()

    def _device_refresh_timed_out(self) -> None:
        if self._device_thread is None:
            return

        self._last_device_error = "Device enumeration timed out (5s)."
        self._device_refresh_timed_out_flag = True
        self._device_status.setText(self._last_device_error)
        self._device_thread.requestInterruption()
        self._restore_device_refresh_cursor()

    def _restore_device_refresh_cursor(self) -> None:
        if not self._device_cursor_active:
            return
        self._device_cursor_active = False
        try:
            QtWidgets.QApplication.restoreOverrideCursor()
        except Exception:
            pass

    def _cleanup_device_refresh(self) -> None:
        if self._device_thread is None and self._device_worker is None:
            return

        if self._device_timeout is not None:
            try:
                self._device_timeout.stop()
            except Exception:
                pass

        self._device_timeout = None

        self._device_thread = None
        self._device_worker = None
        self._restore_device_refresh_cursor()
        self._update_controls_for_server_state()
        if self._server is None:
            self._start_btn.setEnabled(True)

        if not self._last_device_error:
            self._device_status.setText(f"Devices loaded: {self._last_device_count}")

        if self._close_after_device_refresh:
            self._close_after_device_refresh = False
            QtCore.QTimer.singleShot(0, self.close)
        elif not self._last_device_error and not self._is_closing:
            QtCore.QTimer.singleShot(0, self._maybe_autostart)

    def _preset_paths(self) -> Path:
        return Path.home() / "py-intercom" / "server_preset.json"

    def _read_server_preset(self) -> Optional[dict]:
        path = self._preset_paths()
        result = read_json_file_result(path)
        if result.status is JsonFileStatus.MISSING:
            self._preset_load_problem = None
            self._preset_problem_notified = None
            return stamp_preset({}, preset_type="server")
        if result.status is JsonFileStatus.OK and isinstance(result.value, dict):
            try:
                migration = migrate_preset(result.value, preset_type="server")
            except PresetSchemaError as exc:
                status = JsonFileStatus.INVALID
                detail = str(exc)
            else:
                self._preset_load_problem = None
                self._preset_problem_notified = None
                if migration.migrated:
                    logger.info(
                        "server preset preview migrated in memory from schema {}",
                        int(migration.source_version),
                    )
                return migration.data
        else:
            status = result.status if result.status is not JsonFileStatus.OK else JsonFileStatus.INVALID
            detail = str(result.error or "JSON root must be an object")

        self._preset_load_problem = (status, detail)
        signature = f"{status.value}:{detail}"
        logger.error("server preset cannot be loaded: {} ({})", path, detail)
        if self._preset_problem_notified != signature:
            self._preset_problem_notified = signature
            QtCore.QTimer.singleShot(0, self._show_server_preset_problem)
        return None

    def _show_server_preset_problem(self) -> None:
        problem = self._preset_load_problem
        if problem is None or self._is_closing:
            return
        status, detail = problem
        path = self._preset_paths()
        backup = path.with_suffix(path.suffix + ".bak")
        backup_state = "présente" if backup.exists() else "absente"
        QtWidgets.QMessageBox.warning(
            self,
            "Preset serveur illisible",
            "Le preset serveur est corrompu ou inaccessible. Il ne sera pas écrasé "
            "automatiquement et le serveur ne démarrera pas avec une configuration "
            "incertaine. Utilisez Reset config ou restaurez la sauvegarde.\n\n"
            f"Fichier : {path}\n"
            f"Sauvegarde ({backup_state}) : {backup}\n"
            f"État : {status.value}\n"
            f"Détail : {detail}",
        )

    def _load_preset_preview(self) -> None:
        if self._server is not None:
            return

        server_preset = self._preset_paths()
        outputs: list[dict] = []
        buses_raw: dict = {}
        clients_raw: dict = {}
        data = self._read_server_preset()
        if data is None:
            data = {}
        if isinstance(data, dict) and isinstance(data.get("outputs"), list):
            outputs = list(data.get("outputs") or [])
        if isinstance(data, dict) and isinstance(data.get("buses"), dict):
            buses_raw = dict(data.get("buses") or {})
        if isinstance(data, dict) and isinstance(data.get("clients"), dict):
            clients_raw = dict(data.get("clients") or {})
        return_enabled = bool(data.get("return_enabled", False)) if isinstance(data, dict) else False
        return_input_device = None
        if isinstance(data, dict):
            return_input_device = data.get("return_input_device")
            try:
                return_input_device = int(return_input_device) if return_input_device is not None else None
            except Exception:
                return_input_device = None
        return_input_device_name = str(data.get("return_input_device_name") or "") if isinstance(data, dict) else ""
        return_input_device_hostapi = str(data.get("return_input_device_hostapi") or "") if isinstance(data, dict) else ""

        devices = list(self._devices_cache)
        if not devices:
            try:
                devices = list_devices(hostapi_substring=None, hard_refresh=False, validate=False)
            except Exception:
                devices = []

        return_input_device = resolve_device(
            return_input_device_name,
            return_input_device_hostapi,
            return_input_device,
            devices,
            direction="input",
        )

        uuid_to_id: Dict[str, int] = {}
        for u in clients_raw.keys():
            try:
                uuid_to_id[str(u)] = int(client_id_from_uuid(str(u)))
            except Exception:
                continue

        feed_by_id = {
            definition.bus_id: definition.default_feed_to_regie
            for definition in FIXED_BUSES
        }
        gain_by_id = {definition.bus_id: 0.0 for definition in FIXED_BUSES}
        for bus_id_str, b in buses_raw.items():
            try:
                bid = int(bus_id_str)
            except Exception:
                continue
            if not isinstance(b, dict):
                continue
            if bid not in BUS_BY_ID:
                continue
            if bid != REGIE_BUS_ID:
                feed_by_id[int(bid)] = bool(
                    b.get(
                        "feed_to_regie",
                        BUS_BY_ID[int(bid)].default_feed_to_regie,
                    )
                )
            try:
                gain_by_id[int(bid)] = float(b.get("gain_db", 0.0))
            except Exception:
                gain_by_id[int(bid)] = 0.0

        buses = {
            definition.bus_id: {
                "bus_id": definition.bus_id,
                "name": definition.name,
                "gain_db": float(gain_by_id[definition.bus_id]),
                "feed_to_regie": bool(feed_by_id[definition.bus_id]),
            }
            for definition in FIXED_BUSES
        }

        snap: Dict[int, dict] = {}
        for u, c in clients_raw.items():
            cid = uuid_to_id.get(str(u))
            if cid is None:
                continue
            name = ""
            input_gain_db = 0.0
            if isinstance(c, dict):
                name = str(c.get("name") or "")
                try:
                    input_gain_db = float(c.get("input_gain_db", 0.0))
                except Exception:
                    input_gain_db = 0.0
            snap[int(cid)] = {
                "client_id": int(cid),
                "name": name,
                "client_uuid": str(u),
                "input_gain_db": float(input_gain_db),
                "addr": None,
                "age_s": None,
                "vu_dbfs": -60.0,
                "last_timestamp_ms": 0,
                "last_sequence_number": 0,
                "control_connected": False,
                "control_age_s": None,
            }

        outs: Dict[int, dict] = {}
        for oid, o in enumerate(outputs):
            if not isinstance(o, dict):
                continue
            raw_device = o.get("device")
            try:
                saved_device = int(raw_device) if raw_device is not None else None
            except (TypeError, ValueError, OverflowError):
                saved_device = None
            resolved_device = resolve_device(
                str(o.get("device_name") or ""),
                str(o.get("device_hostapi") or ""),
                saved_device,
                devices,
                direction="output",
            )
            outs[int(oid)] = {
                "output_id": int(oid),
                "device": resolved_device,
                "bus_id": o.get("bus_id", REGIE_BUS_ID),
                "gain_db": o.get("gain_db", 0.0),
                "samplerate": "",
                "queued_ms": "",
                "underflows": "",
                "vu_dbfs": "",
            }

        self._refresh_bus_selectors(buses)
        self._refresh_buses_table(buses)
        self._refresh_outputs_table(outs, buses)
        self._set_clients_table(snap, buses)
        self._return_enabled.setChecked(bool(return_enabled))
        if return_input_device is not None:
            idx = self._return_input_device.findData(int(return_input_device))
            if idx >= 0:
                self._return_input_device.setCurrentIndex(idx)

    def _reset_total_config(self) -> None:
        if self._server is not None:
            QtWidgets.QMessageBox.warning(self, "Running", "Stop the server first")
            return

        if (
            QtWidgets.QMessageBox.question(
                self,
                "Reset config",
                "Delete server preset file and reset server configuration?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        server_preset = self._preset_paths()
        try:
            tmp = server_preset.with_suffix(server_preset.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

        try:
            if server_preset.exists():
                server_preset.unlink()
        except Exception:
            pass

        self._preset_load_problem = None
        self._preset_problem_notified = None

        self._outputs.setRowCount(0)
        self._buses.setRowCount(0)
        self._output_row_ids = []
        self._bus_row_ids = []
        self._output_device_widgets.clear()
        self._output_bus_widgets.clear()
        self._bus_feed_widgets.clear()
        self._load_preset_preview()

    def _reopen_outputs_after_start(self) -> None:
        if self._server is None:
            return
        try:
            self._server.reopen_outputs(force=True)
        except Exception:
            pass

    def _reopen_return_after_start(self) -> None:
        if self._server is None:
            return
        try:
            self._server.reopen_return_input()
        except Exception:
            pass

    def _refresh_bus_selectors(self, buses: Dict[int, dict]) -> None:
        sorted_buses = [
            (definition.bus_id, buses.get(definition.bus_id, {}))
            for definition in FIXED_BUSES
            if definition.bus_id in buses
        ]
        bus_ids = [int(bid) for bid, _ in sorted_buses]

        if bus_ids == self._bus_selector_ids and self._bus_selector.count() > 0:
            return

        self._bus_selector.blockSignals(True)
        try:
            current = self._bus_selector.currentData()
            self._bus_selector.clear()
            for bid, b in sorted_buses:
                self._bus_selector.addItem(
                    str(b.get("name") or BUS_BY_ID[int(bid)].name),
                    int(bid),
                )
            if current is not None:
                i = self._bus_selector.findData(current)
                if i >= 0:
                    self._bus_selector.setCurrentIndex(i)
            self._bus_selector_ids = list(bus_ids)
        finally:
            self._bus_selector.blockSignals(False)

    def _refresh_buses_table(self, buses: Dict[int, dict]) -> None:
        bus_ids = [
            definition.bus_id
            for definition in FIXED_BUSES
            if definition.bus_id in buses
        ]
        rebuild = bus_ids != self._bus_row_ids
        if rebuild:
            self._buses.setRowCount(len(bus_ids))
            self._bus_row_ids = list(bus_ids)
            self._bus_gain_widgets.clear()
            self._bus_gain_labels.clear()
            self._bus_feed_widgets.clear()

        for row, bid in enumerate(bus_ids):
            b = buses.get(int(bid), {})

            def _set_item(col: int, text: str) -> None:
                it = self._buses.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem(text)
                    self._buses.setItem(row, col, it)
                else:
                    it.setText(text)

            _set_item(0, str(int(bid)))
            _set_item(1, str(b.get("name") or BUS_BY_ID[int(bid)].name))
            if rebuild:
                gain_wrap = QtWidgets.QWidget()
                gain_lay = QtWidgets.QHBoxLayout(gain_wrap)
                gain_lay.setContentsMargins(6, 0, 6, 0)
                gain_lay.setSpacing(6)
                gain_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
                gain_slider.setMinimum(-60)
                gain_slider.setMaximum(int(MAX_GAIN_DB))
                gain_slider.setSingleStep(1)
                gain_slider.valueChanged.connect(lambda value, bus_id=int(bid): self._on_bus_gain_changed(bus_id, value))
                gain_lbl = QtWidgets.QLabel("0 dB")
                gain_lbl.setFixedWidth(42)
                gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                gain_lay.addWidget(gain_slider, 1)
                gain_lay.addWidget(gain_lbl)
                self._buses.setCellWidget(row, 2, gain_wrap)
                self._bus_gain_widgets[int(bid)] = gain_slider
                self._bus_gain_labels[int(bid)] = gain_lbl
            gain_slider = self._bus_gain_widgets.get(int(bid))
            gain_lbl = self._bus_gain_labels.get(int(bid))
            try:
                gain_value = int(round(float(b.get("gain_db", 0.0))))
            except Exception:
                gain_value = 0
            if gain_slider is not None:
                gain_slider.blockSignals(True)
                try:
                    gain_slider.setEnabled(self._server is not None)
                    gain_slider.setValue(int(gain_value))
                finally:
                    gain_slider.blockSignals(False)
            if gain_lbl is not None:
                gain_lbl.setText(f"{int(gain_value)} dB")
            if rebuild:
                cb = QtWidgets.QCheckBox()
                cb.stateChanged.connect(lambda state, bus_id=int(bid): self._on_bus_feed_to_regie_changed(bus_id, state))
                self._buses.setCellWidget(row, 3, centered_checkbox(cb))
                self._bus_feed_widgets[int(bid)] = cb

            cb = self._bus_feed_widgets.get(int(bid))
            if cb is not None:
                cb.blockSignals(True)
                try:
                    definition = BUS_BY_ID[int(bid)]
                    cb.setEnabled(
                        self._server is not None and int(bid) != REGIE_BUS_ID
                    )
                    if int(bid) == REGIE_BUS_ID:
                        cb.setChecked(True)
                    else:
                        cb.setChecked(
                            bool(
                                b.get(
                                    "feed_to_regie",
                                    definition.default_feed_to_regie,
                                )
                            )
                        )
                finally:
                    cb.blockSignals(False)

    def _on_bus_feed_to_regie_changed(self, bus_id: int, state: int) -> None:
        if (
            self._server is None
            or int(bus_id) not in BUS_BY_ID
            or int(bus_id) == REGIE_BUS_ID
        ):
            return
        self._server.set_bus_feed_to_regie(int(bus_id), is_checked(state))

    def _on_bus_gain_changed(self, bus_id: int, value: int) -> None:
        if int(bus_id) not in BUS_BY_ID:
            return
        label = self._bus_gain_labels.get(int(bus_id))
        if label is not None:
            label.setText(f"{int(value)} dB")
        if self._server is None:
            return
        self._server.set_bus_gain(int(bus_id), float(value))

    def _refresh_outputs_table(self, outs: Dict[int, dict], buses: Dict[int, dict]) -> None:
        output_ids = [oid for oid in sorted(outs.keys())]
        rebuild = output_ids != self._output_row_ids
        if rebuild:
            self._outputs.setRowCount(len(output_ids))
            self._output_row_ids = list(output_ids)
            self._output_device_widgets.clear()
            self._output_bus_widgets.clear()
            self._output_gain_widgets.clear()
            self._output_gain_labels.clear()
            self._output_vu_widgets.clear()
            self._output_status_widgets.clear()

        bus_names = {
            definition.bus_id: str(
                buses.get(definition.bus_id, {}).get("name") or definition.name
            )
            for definition in FIXED_BUSES
            if definition.bus_id in buses
        }
        sorted_bus_ids = [
            definition.bus_id
            for definition in FIXED_BUSES
            if definition.bus_id in bus_names
        ]

        for row, oid in enumerate(output_ids):
            st = outs.get(int(oid), {})

            def _set_item(col: int, text: str) -> None:
                it = self._outputs.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem(text)
                    self._outputs.setItem(row, col, it)
                else:
                    it.setText(text)

            _set_item(0, str(oid))

            if rebuild:
                dev_cb = QtWidgets.QComboBox()
                patch_combo(dev_cb)
                dev_cb.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                dev_cb.setMinimumContentsLength(40)
                dev_cb.blockSignals(True)
                for label, idx in self._output_devices_cache:
                    dev_cb.addItem(label, idx)
                dev_cb.blockSignals(False)
                dev_cb.currentIndexChanged.connect(lambda _i, out_id=int(oid): self._on_output_device_changed(out_id))
                self._outputs.setCellWidget(row, 1, dev_cb)
                self._output_device_widgets[int(oid)] = dev_cb

                bus_cb = QtWidgets.QComboBox()
                patch_combo(bus_cb)
                bus_cb.blockSignals(True)
                for bid in sorted_bus_ids:
                    bus_cb.addItem(str(bus_names[bid]), int(bid))
                bus_cb.blockSignals(False)
                bus_cb.currentIndexChanged.connect(lambda _i, out_id=int(oid): self._on_output_bus_changed(out_id))
                self._outputs.setCellWidget(row, 2, bus_cb)
                self._output_bus_widgets[int(oid)] = bus_cb

                gain_wrap = QtWidgets.QWidget()
                gain_lay = QtWidgets.QHBoxLayout(gain_wrap)
                gain_lay.setContentsMargins(6, 0, 6, 0)
                gain_lay.setSpacing(6)
                gain_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
                gain_slider.setMinimum(-60)
                gain_slider.setMaximum(int(MAX_GAIN_DB))
                gain_slider.setSingleStep(1)
                gain_slider.valueChanged.connect(lambda value, out_id=int(oid): self._on_output_gain_changed(out_id, value))
                gain_lbl = QtWidgets.QLabel("0 dB")
                gain_lbl.setFixedWidth(42)
                gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                gain_lay.addWidget(gain_slider, 1)
                gain_lay.addWidget(gain_lbl)
                self._outputs.setCellWidget(row, 3, gain_wrap)
                self._output_gain_widgets[int(oid)] = gain_slider
                self._output_gain_labels[int(oid)] = gain_lbl

            dev_cb = self._output_device_widgets.get(int(oid))
            if dev_cb is not None:
                dev_cb.blockSignals(True)
                try:
                    cur_dev = st.get("device")
                    if cur_dev is not None:
                        i = dev_cb.findData(int(cur_dev))
                        if i >= 0:
                            dev_cb.setCurrentIndex(i)
                    dev_cb.setEnabled(self._server is not None)
                finally:
                    dev_cb.blockSignals(False)

            bus_cb = self._output_bus_widgets.get(int(oid))
            if bus_cb is not None:
                bus_cb.blockSignals(True)
                try:
                    cur_bus = st.get("bus_id")
                    if cur_bus is not None:
                        i = bus_cb.findData(int(cur_bus))
                        if i >= 0:
                            bus_cb.setCurrentIndex(i)
                    bus_cb.setEnabled(self._server is not None)
                finally:
                    bus_cb.blockSignals(False)

            gain_slider = self._output_gain_widgets.get(int(oid))
            gain_lbl = self._output_gain_labels.get(int(oid))
            try:
                gain_value = int(round(float(st.get("gain_db", 0.0))))
            except Exception:
                gain_value = 0
            if gain_slider is not None:
                gain_slider.blockSignals(True)
                try:
                    gain_slider.setEnabled(self._server is not None)
                    gain_slider.setValue(int(gain_value))
                finally:
                    gain_slider.blockSignals(False)
            if gain_lbl is not None:
                gain_lbl.setText(f"{int(gain_value)} dB")

            if rebuild:
                vu = VuMeter()
                self._outputs.setCellWidget(row, 4, cell_vu(vu))
                self._output_vu_widgets[int(oid)] = vu
                indicator = StatusIndicator()
                self._outputs.setCellWidget(row, 5, centered_checkbox(indicator))
                self._output_status_widgets[int(oid)] = indicator
            vu = self._output_vu_widgets.get(int(oid))
            if vu is not None:
                try:
                    vu.set_level(float(st.get("vu_dbfs", -60.0)))
                except Exception:
                    pass
            indicator = self._output_status_widgets.get(int(oid))
            if indicator is not None:
                error = str(st.get("stream_error") or st.get("last_error") or "")
                if error:
                    indicator.set_state(
                        "error",
                        description=f"Sortie {oid} en erreur : {error}",
                    )
                elif st.get("stream_active") is True:
                    indicator.set_state(
                        "online",
                        description=f"Sortie {oid} active",
                    )
                elif "stream_active" in st:
                    indicator.set_state(
                        "error",
                        description=f"Sortie {oid} inactive",
                    )
                else:
                    indicator.set_state(
                        "unknown",
                        description=f"Activité de la sortie {oid} non confirmée",
                    )

    def _on_output_selection_changed(self) -> None:
        self._remove_output_btn.setEnabled(self._server is not None and len(self._outputs.selectedItems()) > 0)

    def _on_client_input_gain_changed(self, client_id: int, value: int) -> None:
        label = self._input_gain_labels.get(int(client_id))
        if label is not None:
            label.setText(f"{int(value)} dB")
        if self._server is None:
            return
        self._server.set_client_input_gain(int(client_id), float(value))

    def _on_output_gain_changed(self, output_id: int, value: int) -> None:
        label = self._output_gain_labels.get(int(output_id))
        if label is not None:
            label.setText(f"{int(value)} dB")
        if self._server is None:
            return
        self._server.set_output_gain(int(output_id), float(value))

    def _on_add_output(self) -> None:
        if self._server is None:
            return
        dev = self._add_output_device.currentData()
        if dev is None:
            return
        bus_data = self._bus_selector.currentData()
        if bus_data is None:
            return
        bus_id = int(bus_data)
        if bus_id not in BUS_BY_ID:
            return
        try:
            self._server.create_output(int(dev), int(bus_id))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Output", str(e))

    def _on_remove_output(self) -> None:
        if self._server is None:
            return
        row = self._outputs.currentRow()
        if row < 0:
            return
        item = self._outputs.item(row, 0)
        if item is None:
            return
        try:
            oid = int(item.text())
        except Exception:
            return
        self._server.remove_output(int(oid))

    def _on_output_device_changed(self, output_id: int) -> None:
        if self._server is None:
            return
        cb = self._output_device_widgets.get(int(output_id))
        if cb is None:
            return
        dev = cb.currentData()
        if dev is None:
            return
        self._server.set_output_device(int(output_id), int(dev))

    def _on_output_bus_changed(self, output_id: int) -> None:
        if self._server is None:
            return
        cb = self._output_bus_widgets.get(int(output_id))
        if cb is None:
            return
        bid = cb.currentData()
        if bid is None:
            return
        if int(bid) not in BUS_BY_ID:
            return
        self._server.set_output_bus(int(output_id), int(bid))

    def _on_devices_refreshed(self, devs, error: str) -> None:
        if self._device_refresh_timed_out_flag:
            return
        if error:
            self._last_device_error = error
            self._device_status.setText(f"Device refresh failed: {error}")
            QtWidgets.QMessageBox.warning(self, "Device refresh failed", error)
            return

        self._last_device_count = int(len(devs))
        self._device_status.setText(f"Devices loaded: {self._last_device_count}")
        self._devices_cache = list(devs)

        self._add_output_device.blockSignals(True)
        try:
            current = self._add_output_device.currentData()
            self._add_output_device.clear()
            self._add_output_device.setCurrentIndex(-1)
            self._output_devices_cache = []
            for d in devs:
                if not d.supports_output:
                    continue
                label = format_device_label(d)
                self._add_output_device.addItem(label, d.index)
                self._output_devices_cache.append((label, int(d.index)))
            if current is not None:
                i = self._add_output_device.findData(current)
                if i >= 0:
                    self._add_output_device.setCurrentIndex(i)
        finally:
            self._add_output_device.blockSignals(False)

        outs_snap = self._server.get_outputs_snapshot() if self._server is not None else {}
        for oid, dev_cb in self._output_device_widgets.items():
            try:
                selected = dev_cb.currentData()
                if selected is None and self._server is not None:
                    st = outs_snap.get(int(oid), {})
                    cur_dev = st.get("device")
                    if cur_dev is not None:
                        selected = int(cur_dev)
                dev_cb.blockSignals(True)
                dev_cb.clear()
                dev_cb.setCurrentIndex(-1)
                for label, idx in self._output_devices_cache:
                    dev_cb.addItem(label, idx)
                if selected is not None:
                    i = dev_cb.findData(selected)
                    if i >= 0:
                        dev_cb.setCurrentIndex(i)
            except Exception:
                pass
            finally:
                try:
                    dev_cb.blockSignals(False)
                except Exception:
                    pass

        self._return_input_device.blockSignals(True)
        try:
            current_in = self._return_input_device.currentData()
            self._return_input_device.clear()
            self._return_input_devices_cache = []
            self._return_input_device.addItem("(none)", None)
            for d in devs:
                if not d.supports_input:
                    continue
                label = format_device_label(d)
                self._return_input_device.addItem(label, d.index)
                self._return_input_devices_cache.append((label, int(d.index)))
            target_in = current_in
            if target_in is None:
                data = self._read_server_preset()
                if isinstance(data, dict):
                    saved_in = data.get("return_input_device")
                    try:
                        saved_in = int(saved_in) if saved_in is not None else None
                    except Exception:
                        saved_in = None
                    saved_name = str(data.get("return_input_device_name") or "")
                    saved_hostapi = str(data.get("return_input_device_hostapi") or "")

                    target_in = resolve_device(
                        saved_name,
                        saved_hostapi,
                        saved_in,
                        devs,
                        direction="input",
                    )

            if target_in is not None:
                i = self._return_input_device.findData(int(target_in))
                if i >= 0:
                    self._return_input_device.setCurrentIndex(i)
        finally:
            self._return_input_device.blockSignals(False)

        if self._server is None:
            self._output_row_ids = []
            self._load_preset_preview()

    def _on_return_enabled_changed(self, state: int) -> None:
        if self._server is None:
            return
        self._server.set_return_enabled(is_checked(state))

    def _on_show_all_devices_changed(self, state: int) -> None:
        self._settings.setValue("show_all_devices", bool(is_checked(state)))
        self._start_device_refresh()

    def _on_return_input_device_changed(self, _idx: int) -> None:
        dev = self._return_input_device.currentData()
        if self._server is not None:
            self._server.set_return_input_device(int(dev) if dev is not None else None)
            return

        server_preset = self._preset_paths()
        data = self._read_server_preset()
        if data is None:
            self._device_status.setText("Preset illisible : modification non enregistrée")
            return
        try:
            device_idx = int(dev) if dev is not None else None
        except Exception:
            device_idx = None

        device_name = ""
        device_hostapi = ""
        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=False, validate=False)
        except Exception:
            devices = []
        if device_idx is not None:
            for d in devices:
                if int(d.index) == int(device_idx):
                    device_name = str(d.name)
                    device_hostapi = str(d.hostapi)
                    break

        data["return_input_device"] = int(device_idx) if device_idx is not None else None
        data["return_input_device_name"] = str(device_name)
        data["return_input_device_hostapi"] = str(device_hostapi)
        try:
            atomic_write_json(server_preset, data)
        except Exception:
            pass

    def _forget_selected_client(self) -> None:
        try:
            items = self._clients.selectedItems()
            if not items:
                return
            row = int(items[0].row())
            if row < 0 or row >= len(self._client_row_ids):
                return
            client_id = int(self._client_row_ids[row])
        except Exception:
            return

        if self._server is not None:
            self._server.forget_client(int(client_id))
            return

        client_uuid = str(self._client_uuid_by_id.get(int(client_id), "") or "")
        if not client_uuid:
            return

        server_preset = self._preset_paths()
        try:
            data = self._read_server_preset()
            if data is None:
                return

            clients = data.get("clients") if isinstance(data.get("clients"), dict) else {}
            clients.pop(str(client_uuid), None)
            data["clients"] = clients

            buses = data.get("buses") if isinstance(data.get("buses"), dict) else {}
            for _bid, b in buses.items():
                if not isinstance(b, dict):
                    continue
                uuids = b.get("source_uuids")
                if isinstance(uuids, list):
                    b["source_uuids"] = [str(u) for u in uuids if str(u) and str(u) != str(client_uuid)]
            data["buses"] = buses

            atomic_write_json(server_preset, data)
        except Exception:
            pass

        self._load_preset_preview()

    def _start_server(self) -> None:
        if self._server is not None:
            return
        if self._device_thread is not None:
            self._device_status.setText("Wait for device enumeration before starting the server.")
            return

        # reset cached widgets (they may refer to deleted Qt objects after a previous stop/start)
        self._client_row_ids = []
        self._input_gain_widgets.clear()
        self._input_gain_labels.clear()
        self._vu_widgets.clear()
        self._status_widgets.clear()

        self._output_row_ids = []
        self._bus_row_ids = []
        self._output_device_widgets.clear()
        self._output_bus_widgets.clear()
        self._output_gain_widgets.clear()
        self._output_gain_labels.clear()
        self._output_vu_widgets.clear()
        self._output_status_widgets.clear()
        self._bus_gain_widgets.clear()
        self._bus_gain_labels.clear()
        self._bus_feed_widgets.clear()

        try:
            self._server = IntercomServer(
                bind_ip="0.0.0.0",
                port=int(AUDIO_UDP_PORT),
                output_device=None,
                server_name=self._server_name.text().strip() or "py-intercom",
                discovery_enabled=self._discovery_cb.isChecked(),
                return_input_device=self._return_input_device.currentData(),
                return_enabled=self._return_enabled.isChecked(),
            )
        except OSError as e:
            self._server = None
            QtWidgets.QMessageBox.critical(
                self,
                "Start failed",
                f"Impossible d'ouvrir le port UDP {int(AUDIO_UDP_PORT)}. "
                f"Un autre serveur py-intercom est peut-être déjà lancé.\n\nDétail: {e}",
            )
            return
        except Exception as e:
            self._server = None
            QtWidgets.QMessageBox.critical(self, "Start failed", str(e))
            return

        try:
            self._server.load_preset()
            # Sync GUI back from the preset-loaded server state so the checkbox
            # reflects reality before start() opens the return stream.
            self._return_enabled.blockSignals(True)
            self._return_enabled.setChecked(bool(self._server._return_enabled))
            self._return_enabled.blockSignals(False)
            if self._server._return_input_device is not None:
                idx = self._return_input_device.findData(int(self._server._return_input_device))
                if idx >= 0:
                    self._return_input_device.blockSignals(True)
                    self._return_input_device.setCurrentIndex(idx)
                    self._return_input_device.blockSignals(False)
            self._server.save_preset()
        except PresetLoadError as e:
            try:
                self._server.stop()
            finally:
                self._server = None
            backup_state = "présente" if e.backup_path.exists() else "absente"
            QtWidgets.QMessageBox.critical(
                self,
                "Preset serveur illisible",
                "Démarrage annulé pour éviter d'utiliser puis d'écraser une configuration "
                "incertaine. Restaurez la sauvegarde ou utilisez Reset config.\n\n"
                f"Fichier : {e.path}\n"
                f"Sauvegarde ({backup_state}) : {e.backup_path}\n"
                f"Détail : {e.detail}",
            )
            return
        except Exception as e:
            try:
                self._server.stop()
            finally:
                self._server = None
            QtWidgets.QMessageBox.critical(self, "Preset", f"Preset load failed: {e}")
            return

        try:
            self._server.start()
        except Exception as e:
            logger.exception("server start failed")
            self._server = None
            QtWidgets.QMessageBox.critical(self, "Start failed", str(e))
            return

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._update_controls_for_server_state()

        try:
            snap = self._server.get_clients_snapshot()
            buses = self._server.get_buses_snapshot()
            outs = self._server.get_outputs_snapshot()
            self._refresh_bus_selectors(buses)
            self._refresh_buses_table(buses)
            self._set_clients_table(snap, buses)
            self._refresh_outputs_table(outs, buses)
            self._update_server_health(
                self._server.get_stats_snapshot(),
                snap,
                outs,
            )
        except Exception:
            pass

        self._timer.start()
        self._vu_timer.start()
        QtCore.QTimer.singleShot(1200, self._reopen_outputs_after_start)
        QtCore.QTimer.singleShot(2500, self._reopen_return_after_start)

    def _stop_server(self) -> None:
        if self._server is None:
            return
        try:
            self._timer.stop()
            self._vu_timer.stop()
            self._server.stop()
        finally:
            self._server = None
            self._start_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
            self._clients.setRowCount(0)
            self._outputs.setRowCount(0)
            self._remove_output_btn.setEnabled(False)
            self._forget_client_btn.setEnabled(False)
            set_status_label(
                self._status_label,
                "Arrêté",
                "offline",
                detail="Serveur intercom arrêté.",
            )
            set_status_label(
                self._server_state_banner,
                "Arrêté",
                "offline",
                detail="Serveur intercom arrêté.",
            )
            self._set_health_facets_unavailable()
            self._return_vu.set_level(-60.0)
            self._regie_vu.set_level(-60.0)

            self._client_row_ids = []
            self._input_gain_widgets.clear()
            self._input_gain_labels.clear()
            self._vu_widgets.clear()
            self._status_widgets.clear()

            self._output_row_ids = []
            self._output_device_widgets.clear()
            self._output_bus_widgets.clear()
            self._output_gain_widgets.clear()
            self._output_gain_labels.clear()
            self._output_vu_widgets.clear()
            self._output_status_widgets.clear()
            self._buses.setRowCount(0)
            self._bus_row_ids = []
            self._bus_gain_widgets.clear()
            self._bus_gain_labels.clear()
            self._bus_feed_widgets.clear()

            self._update_controls_for_server_state()
            self._load_preset_preview()

    def _refresh_clients(self) -> None:
        if self._server is None:
            return
        snap = self._server.get_clients_snapshot()
        buses = self._server.get_buses_snapshot()
        outs = self._server.get_outputs_snapshot()
        self._refresh_bus_selectors(buses)
        self._refresh_buses_table(buses)
        self._set_clients_table(snap, buses)
        self._refresh_outputs_table(outs, buses)

        try:
            items = self._clients.selectedItems()
            have_sel = bool(items)
        except Exception:
            have_sel = False
        self._forget_client_btn.setEnabled(bool(have_sel))
        self._update_controls_for_server_state()

    def _refresh_vu_and_health(self) -> None:
        if self._server is None:
            return
        clients = self._server.get_clients_snapshot()
        outputs = self._server.get_outputs_snapshot()
        stats = self._server.get_stats_snapshot()

        try:
            self._return_vu.set_level(float(stats.get("return_vu_dbfs", -60.0)))
        except Exception:
            self._return_vu.set_level(-60.0)
        try:
            self._regie_vu.set_level(float(stats.get("regie_vu_dbfs", -60.0)))
        except Exception:
            self._regie_vu.set_level(-60.0)

        for client_id, vu in self._vu_widgets.items():
            client = clients.get(int(client_id), {})
            try:
                vu.set_level(float(client.get("vu_dbfs", -60.0)))
            except Exception:
                vu.set_level(-60.0)
        for output_id, vu in self._output_vu_widgets.items():
            output = outputs.get(int(output_id), {})
            try:
                vu.set_level(float(output.get("vu_dbfs", -60.0)))
            except Exception:
                vu.set_level(-60.0)

        self._update_server_health(stats, clients, outputs)

    def _show_geek_info(self) -> None:
        port = int(AUDIO_UDP_PORT)
        ctrl_port = port + 1
        running = self._server is not None
        snap = self._server.get_clients_snapshot() if self._server is not None else {}
        stats = self._server.get_stats_snapshot() if self._server is not None else {}

        selected = None
        try:
            items = self._clients.selectedItems()
            if items:
                row = int(items[0].row())
                if 0 <= row < len(self._client_row_ids):
                    selected = int(self._client_row_ids[row])
        except Exception:
            selected = None

        lines = [
            f"Audio UDP port: {port}",
            f"Control TCP port: {ctrl_port}",
            f"Running: {running}",
            f"Clients: {len(snap)}",
            f"Control connected: {stats.get('control_connected_clients', '-')}",
            f"Waiting UDP: {stats.get('clients_waiting_udp', '-')}",
            f"Outputs: {stats.get('outputs', '-')}",
            f"Mix queue: {stats.get('mix_q', '-')}",
            f"Underflows: {stats.get('underflows', '-')}",
            f"RX datagrams: {stats.get('rx_datagrams', '-')}",
            f"RX bytes: {stats.get('rx_bytes', '-')}",
            f"RX sock err: {stats.get('rx_socket_errors', '-')}",
            f"RX pkts: {stats.get('rx_packets', '-')}",
            f"RX dec err: {stats.get('rx_decode_errors', '-')}",
            f"TX pkts: {stats.get('tx_packets', '-')}",
            f"TX sock err: {stats.get('tx_socket_errors', '-')}",
            f"Workers: {stats.get('worker_alive', '-')}",
            f"Worker errors: {stats.get('worker_errors', '-')}",
            f"Return stream active: {stats.get('return_stream_active', '-')}",
            f"Opus OK: {stats.get('opus_ok', '-')}",
            f"Opuslib ver: {stats.get('opuslib_version', '-')}",
        ]

        if snap:
            lines.append("")
            lines.append("--- Connected clients ---")
            for cid, st in sorted(snap.items()):
                addr = st.get("addr")
                addr_str = f"{addr[0]}:{addr[1]}" if addr else "-"
                ctrl = "✓" if st.get("control_connected") else "✗"
                lines.append(f"  [{cid}] {st.get('name', '?')} — {addr_str} (ctrl: {ctrl})")

        if selected is not None and selected in snap:
            st = snap.get(int(selected), {})
            addr = st.get("addr")
            lines.extend(
                [
                    "",
                    f"Selected client_id: {selected}",
                    f"Name: {st.get('name', '')}",
                    f"UUID: {st.get('client_uuid') or '-'}",
                    f"Addr: {addr[0]}:{addr[1]}" if addr else "Addr: -",
                    f"Ctrl: {st.get('control_connected', False)}",
                ]
            )

        server_preset = ""
        if self._server is not None:
            try:
                server_preset = str(self._server.get_preset_paths_snapshot().get("server_preset") or "")
            except Exception:
                server_preset = ""
        if not server_preset:
            server_preset = str(self._preset_paths())

        exists = False
        try:
            exists = Path(server_preset).exists()
        except Exception:
            exists = False

        lines.extend(["", f"Server preset: {server_preset}", f"Preset exists: {exists}"])

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Info")
        box.setText("\n".join(lines))
        reset_btn = box.addButton("Reset config", QtWidgets.QMessageBox.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is reset_btn:
            self._reset_total_config()

    def _set_clients_table(self, snap: Dict[int, dict], buses: Dict[int, dict]) -> None:
        client_ids = [client_id for client_id, _st in sorted(snap.items(), key=lambda kv: kv[0])]
        rebuild = client_ids != self._client_row_ids
        if rebuild:
            self._clients.setRowCount(len(client_ids))
            self._client_row_ids = list(client_ids)
            self._input_gain_widgets.clear()
            self._input_gain_labels.clear()
            self._vu_widgets.clear()
            self._status_widgets.clear()

        for row, client_id in enumerate(client_ids):
            st = snap.get(int(client_id), {})
            try:
                self._client_uuid_by_id[int(client_id)] = str(st.get("client_uuid") or "")
            except Exception:
                pass

            def _set_item(col: int, text: str) -> None:
                it = self._clients.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem(text)
                    self._clients.setItem(row, col, it)
                else:
                    it.setText(text)

            _set_item(0, str(client_id))
            _set_item(1, str(st.get("name", "")))
            addr = st.get("addr")
            _set_item(2, f"{addr[0]}:{addr[1]}" if addr else "")

            # col 3 — independent control/UDP status
            transport_state = derive_client_transport_state(st)
            if rebuild:
                si = StatusIndicator()
                self._clients.setCellWidget(row, 3, centered_checkbox(si))  # reuse centering helper
                self._status_widgets[int(client_id)] = si
            si = self._status_widgets.get(int(client_id))
            if si is not None:
                client_name = str(st.get("name") or client_id)
                if transport_state is ClientTransportState.OFFLINE:
                    si.set_state(
                        "offline",
                        description=f"{client_name} : hors ligne",
                    )
                elif transport_state is ClientTransportState.WAITING_UDP:
                    si.set_state(
                        "connecting",
                        description=f"{client_name} : association audio UDP en attente",
                    )
                elif transport_state is ClientTransportState.IDLE:
                    si.set_state(
                        "idle",
                        description=f"{client_name} : en ligne, au repos",
                    )
                elif transport_state is ClientTransportState.DEGRADED:
                    si.set_state(
                        "degraded",
                        description=(
                            f"{client_name} : en parole, audio UDP absent ou ancien"
                        ),
                    )
                else:
                    si.set_state(
                        "online",
                        description=f"{client_name} : en parole, audio UDP actif",
                    )

            # col 4 — Talk gain
            if rebuild:
                gain_wrap = QtWidgets.QWidget()
                gain_lay = QtWidgets.QHBoxLayout(gain_wrap)
                gain_lay.setContentsMargins(6, 0, 6, 0)
                gain_lay.setSpacing(6)
                gain_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
                gain_slider.setMinimum(-60)
                gain_slider.setMaximum(int(MAX_GAIN_DB))
                gain_slider.setSingleStep(1)
                gain_slider.valueChanged.connect(lambda value, cid=int(client_id): self._on_client_input_gain_changed(cid, value))
                gain_lbl = QtWidgets.QLabel("0 dB")
                gain_lbl.setFixedWidth(42)
                gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                gain_lay.addWidget(gain_slider, 1)
                gain_lay.addWidget(gain_lbl)
                self._clients.setCellWidget(row, 4, gain_wrap)
                self._input_gain_widgets[int(client_id)] = gain_slider
                self._input_gain_labels[int(client_id)] = gain_lbl
            gain_slider = self._input_gain_widgets.get(int(client_id))
            gain_lbl = self._input_gain_labels.get(int(client_id))
            try:
                gain_value = int(round(float(st.get("input_gain_db", 0.0))))
            except Exception:
                gain_value = 0
            if gain_slider is not None:
                gain_slider.blockSignals(True)
                try:
                    gain_slider.setEnabled(self._server is not None)
                    gain_slider.setValue(int(gain_value))
                finally:
                    gain_slider.blockSignals(False)
            if gain_lbl is not None:
                gain_lbl.setText(f"{int(gain_value)} dB")

            # col 5 — VU meter
            if rebuild:
                vu = VuMeter()
                self._clients.setCellWidget(row, 5, cell_vu(vu))
                self._vu_widgets[int(client_id)] = vu
            vu = self._vu_widgets.get(int(client_id))
            if vu is not None:
                try:
                    vu.set_level(float(st.get("vu_dbfs", -60.0)))
                except Exception:
                    pass

            # Row styling: dimmed for offline, normal for online
            fg = QtGui.QColor(100, 100, 100) if disconnected else QtGui.QColor(220, 220, 220)
            for col in range(self._clients.columnCount()):
                it = self._clients.item(row, col)
                if it is None:
                    it = QtWidgets.QTableWidgetItem("")
                    self._clients.setItem(row, col, it)
                it.setForeground(QtGui.QBrush(fg))

    def _on_autostart_changed(self, state: int) -> None:
        self._settings.setValue("autostart", bool(is_checked(state)))

    def _on_start_minimized_changed(self, state: int) -> None:
        self._settings.setValue("start_minimized", bool(is_checked(state)))

    def _maybe_autostart(self) -> None:
        if self._server is None and bool(self._autostart_cb.isChecked()):
            self._start_server()


def run_gui(minimized: bool = False) -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    win = ServerWindow()
    if minimized or bool(win._start_minimized_cb.isChecked()):
        win.showMinimized()
    else:
        win.show()
    return int(app.exec())
