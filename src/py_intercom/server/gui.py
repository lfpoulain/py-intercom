from __future__ import annotations

import copy
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from loguru import logger
from PySide6 import QtCore, QtWidgets

from .. import __version__
from ..common.buses import BUS_BY_ID, FIXED_BUSES, PLATEAU_BUS_ID, REGIE_BUS_ID, VMIX_BUS_ID
from ..common.console import (
    Card,
    ChannelStrip,
    Fader,
    Lamp,
    LevelMeter,
    Section,
    SegmentedNav,
    StripRack,
    TopBar,
    chip,
    compact_combo,
    format_db,
    readout,
    require_focus_for_wheel,
)
from ..common.constants import AUDIO_UDP_PORT, MAX_GAIN_DB
from ..common.devices import DeviceInfo, format_device_label, list_devices
from ..common.diagnostics import CounterRates, format_rate
from ..common.gui_utils import (
    DEVICE_REFRESH_TIMEOUT_MS,
    DeviceRefresh,
    confirm,
    is_checked,
    make_quit_button,
    minimize_on_user_close,
    run_window,
    set_app_icon,
)
from ..common.identity import client_id_from_uuid
from ..common.jsonio import JsonFileStatus, atomic_write_json, read_json_file_result
from ..common.logging import best_effort
from ..common.persistence import DebouncedJsonWriter
from ..common.preset_schema import PresetSchemaError, migrate_preset, stamp_preset
from ..common.theme import (
    TOKENS,
    StatusIndicator,
    make_field,
    mono_font,
    patch_combo,
    repolish,
    set_status_label,
)
from ..common.update_ui import UpdateController
from ..launcher import open_local_client
from .health import (
    ClientTransportState,
    derive_client_transport_state,
    derive_server_operational_health,
)
from .presets import parse_server_preset
from .server import IntercomServer, PresetLoadError

SERVER_STRUCTURE_REFRESH_INTERVAL_MS = 500
SERVER_VU_REFRESH_INTERVAL_MS = 67
_BUS_HINTS = {
    REGIE_BUS_ID: "Entendu par tous les postes",
    PLATEAU_BUS_ID: "Vers les sorties Plateau",
    VMIX_BUS_ID: "Vers les sorties VMix",
}


def _lamp_legend() -> QtWidgets.QWidget:
    """ "R Régie  P Plateau  V VMix": what the talk lamps of a station mean."""
    legend = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(legend)
    lay.setContentsMargins(0, 0, 8, 0)
    lay.setSpacing(4)
    caption = QtWidgets.QLabel("Parle vers :")
    caption.setProperty("role", "hint")
    lay.addWidget(caption)
    for definition in FIXED_BUSES:
        lamp = Lamp(definition.name[:1].upper(), definition.color)
        lamp.set_on(True)
        lay.addWidget(lamp)
        name = QtWidgets.QLabel(definition.name)
        name.setProperty("role", "hint")
        lay.addWidget(name)
        lay.addSpacing(6)
    return legend


# A fader drag emits one value per pixel and every bus/input gain change is
# pushed to the clients over TCP: apply the first value at once, then at most
# one value per interval, always ending on the last one.
GAIN_PUSH_INTERVAL_MS = 50


def _db_value(raw: object) -> int:
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return 0


class ServerWindow(QtWidgets.QMainWindow):
    # Emitted by the stopped-preset writer thread, delivered on the Qt thread.
    _preset_written = QtCore.Signal(object)
    _preset_write_failed = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Py-Intercom Régie {__version__}")
        self.setMinimumSize(1080, 680)
        self.resize(1320, 820)
        set_app_icon(self, "py-intercom.server")

        self._server: IntercomServer | None = None
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

        self._server_name = QtWidgets.QLineEdit(str(self._settings.value("server_name", "py-intercom")))
        self._server_name.setPlaceholderText("Nom annoncé aux postes")
        self._discovery_cb = QtWidgets.QCheckBox("Annoncer le serveur sur le réseau local")
        self._discovery_cb.setChecked(bool(self._settings.value("discovery_enabled", True, type=bool)))
        self._discovery_cb.setToolTip(
            "Les postes du réseau local trouvent ce serveur tout seuls, sans saisir son adresse."
        )
        self._autostart_cb = QtWidgets.QCheckBox("Démarrer le serveur au lancement")
        self._autostart_cb.setChecked(bool(self._settings.value("autostart", False, type=bool)))
        self._start_minimized_cb = QtWidgets.QCheckBox("Démarrer réduit")
        self._start_minimized_cb.setChecked(bool(self._settings.value("start_minimized", False, type=bool)))

        self._return_enabled = chip(
            "Envoyer aux postes",
            "Coché : le son de l'émission est envoyé aux postes ; chacun choisit de l'écouter ou non.",
        )
        self._return_enabled.setChecked(False)
        self._return_input_device = require_focus_for_wheel(QtWidgets.QComboBox())
        patch_combo(self._return_input_device)
        self._return_input_device.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._return_input_device.setMinimumContentsLength(20)
        self._return_vu = LevelMeter()
        self._regie_vu = LevelMeter()
        self._show_all_devices = QtWidgets.QCheckBox("Afficher tous les périphériques (pas seulement WASAPI)")
        self._show_all_devices.setChecked(bool(self._settings.value("show_all_devices", False, type=bool)))
        self._refresh_devices_btn = QtWidgets.QPushButton("Actualiser")
        self._refresh_devices_btn.setProperty("class", "secondary")
        self._refresh_devices_btn.setAccessibleName("Actualiser les périphériques audio")
        self._refresh_devices_btn.setToolTip(
            "Actualiser les périphériques audio. Disponible uniquement serveur arrêté."
        )
        self._device_status = QtWidgets.QLabel("")
        self._device_status.setProperty("role", "hint")

        self._start_btn = QtWidgets.QPushButton("▶  Démarrer")
        self._start_btn.setProperty("class", "primary")
        self._start_btn.setMinimumWidth(120)
        self._stop_btn = QtWidgets.QPushButton("■  Arrêter")
        self._stop_btn.setProperty("class", "danger")
        self._stop_btn.setMinimumWidth(120)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setVisible(False)

        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("ⓘ")
        self._info_btn.setProperty("class", "ghost")
        self._info_btn.setFixedSize(34, 34)
        self._info_btn.setToolTip("Informations techniques et remise à zéro")
        self._info_btn.clicked.connect(self._show_geek_info)

        self._local_station: subprocess.Popen | None = None
        self._local_station_btn = QtWidgets.QPushButton("🎧  Poste sur ce PC")
        self._local_station_btn.setProperty("class", "secondary")
        self._local_station_btn.setToolTip(
            "Ouvre un poste (micro + casque) sur ce PC, déjà réglé pour joindre cette Régie.\n"
            "Pratique quand l'opérateur Régie parle aussi. Le poste tourne dans sa propre fenêtre."
        )
        self._local_station_btn.clicked.connect(self._open_local_station)

        # -- Team: one strip per known client ---------------------------------
        self._client_row_ids: list[int] = []
        self._client_uuid_by_id: dict[int, str] = {}
        self._selected_client_id: int | None = None
        self._client_strips: dict[int, ChannelStrip] = {}
        self._input_gain_widgets: dict[int, Fader] = {}
        self._input_gain_labels: dict[int, QtWidgets.QLabel] = {}
        self._vu_widgets: dict[int, LevelMeter] = {}
        self._status_widgets: dict[int, StatusIndicator] = {}
        self._talk_lamps: dict[int, dict[int, Lamp]] = {}
        self._client_diag: dict[int, tuple[QtWidgets.QLabel, CounterRates]] = {}

        self._client_rack = StripRack(
            "Aucun poste pour l'instant.\nLes postes apparaissent ici dès leur première connexion."
        )
        self._forget_client_btn = QtWidgets.QPushButton("Oublier le poste")
        self._forget_client_btn.setProperty("class", "secondary")
        self._forget_client_btn.setToolTip("Retirer le poste sélectionné de la liste et du preset")
        self._forget_client_btn.setEnabled(False)
        self._forget_client_btn.clicked.connect(self._forget_selected_client)

        self._team_section = Section(
            "Équipe",
            "Une tranche par poste. Ses voyants s'allument quand il parle vers une destination ; "
            "le fader règle le volume de sa voix.",
        )
        self._team_section.actions.addWidget(_lamp_legend())
        self._team_section.actions.addWidget(self._forget_client_btn)
        self._team_section.body.addWidget(self._client_rack, 1)

        # -- Buses: fixed, built once ------------------------------------------
        self._bus_strips: dict[int, ChannelStrip] = {}
        self._bus_gain_widgets: dict[int, Fader] = {}
        self._bus_gain_labels: dict[int, QtWidgets.QLabel] = {}
        self._bus_feed_widgets: dict[int, QtWidgets.QCheckBox] = {}
        self._bus_vu_widgets: dict[int, LevelMeter] = {}
        buses_section = Section("Destinations", "Volume de ce qui est dit vers chaque destination.")
        bus_row = QtWidgets.QHBoxLayout()
        bus_row.setSpacing(8)
        for definition in FIXED_BUSES:
            bid = int(definition.bus_id)
            fader = Fader(-60, int(MAX_GAIN_DB))
            fader.valueChanged.connect(lambda value, bus_id=bid: self._on_bus_gain_changed(bus_id, value))
            meter = self._regie_vu if bid == REGIE_BUS_ID else LevelMeter()
            value = readout()
            strip = ChannelStrip(
                definition.name,
                hint=_BUS_HINTS.get(bid, ""),
                color=definition.color,
                fader=fader,
                meter=meter,
                readout=value,
                width=112,
            )
            strip.reserve_top(1)
            if bid != REGIE_BUS_ID:
                regie = BUS_BY_ID[REGIE_BUS_ID].name
                feed = chip(
                    "Aussi en Régie",
                    f"Coché : ce qui est dit vers {definition.name} est aussi entendu par tous les postes "
                    f"qui écoutent {regie}.",
                )
                feed.stateChanged.connect(lambda state, bus_id=bid: self._on_bus_feed_to_regie_changed(bus_id, state))
                strip.top.addWidget(feed)
                self._bus_feed_widgets[bid] = feed
            self._bus_strips[bid] = strip
            self._bus_gain_widgets[bid] = fader
            self._bus_gain_labels[bid] = value
            self._bus_vu_widgets[bid] = meter
            bus_row.addWidget(strip)
        buses_section.body.addLayout(bus_row, 1)

        # -- Return input --------------------------------------------------------
        return_section = Section("Programme", "Le son de l'émission (Return).")
        self._return_strip = ChannelStrip(
            "Return",
            hint="Entrée choisie dans Réglages",
            meter=self._return_vu,
            width=112,
            color=TOKENS["muted"],
        )
        self._return_strip.reserve_top(1)
        self._return_strip.top.addWidget(self._return_enabled)
        return_section.body.addWidget(self._return_strip, 1)

        # -- Outputs -----------------------------------------------------------
        self._output_row_ids: list[int] = []
        self._output_strips: dict[int, ChannelStrip] = {}
        self._output_device_widgets: dict[int, QtWidgets.QComboBox] = {}
        self._output_bus_widgets: dict[int, QtWidgets.QComboBox] = {}
        self._output_gain_widgets: dict[int, Fader] = {}
        self._output_gain_labels: dict[int, QtWidgets.QLabel] = {}
        self._output_vu_widgets: dict[int, LevelMeter] = {}
        self._output_status_widgets: dict[int, StatusIndicator] = {}
        self._output_reopen_widgets: dict[int, QtWidgets.QToolButton] = {}
        self._output_devices_cache: list[tuple[str, int]] = []
        self._bus_selector = compact_combo(QtWidgets.QComboBox())
        patch_combo(self._bus_selector)
        self._bus_selector_ids: list[int] = []
        self._add_output_device = compact_combo(QtWidgets.QComboBox())
        patch_combo(self._add_output_device)
        self._add_output_btn = QtWidgets.QPushButton("+  Ajouter")
        self._add_output_btn.setProperty("class", "primary")

        add_card = QtWidgets.QFrame()
        add_card.setProperty("role", "strip")
        add_card.setFixedWidth(170)
        add_lay = QtWidgets.QVBoxLayout(add_card)
        add_lay.setContentsMargins(10, 10, 10, 10)
        add_lay.setSpacing(8)
        add_title = QtWidgets.QLabel("Nouvelle sortie")
        add_title.setProperty("role", "stripName")
        add_hint = QtWidgets.QLabel("Fait sortir une destination sur une carte son du serveur.")
        add_hint.setProperty("role", "stripCaption")
        add_hint.setWordWrap(True)
        add_lay.addWidget(add_title)
        add_lay.addWidget(add_hint)
        add_lay.addWidget(make_field("PÉRIPHÉRIQUE", self._add_output_device))
        add_lay.addWidget(make_field("DESTINATION", self._bus_selector))
        add_lay.addStretch(1)
        add_lay.addWidget(self._add_output_btn)

        self._output_rack = StripRack("")
        self._output_rack.add_trailing(add_card)
        outputs_section = Section(
            "Sorties son",
            "Cartes son du serveur qui diffusent une destination : retour plateau, vMix, enregistreur…",
        )
        outputs_section.body.addWidget(self._output_rack, 1)

        # -- Console page --------------------------------------------------------
        routing_row = QtWidgets.QWidget()
        routing_row.setObjectName("routingRow")
        routing_lay = QtWidgets.QHBoxLayout(routing_row)
        routing_lay.setContentsMargins(0, 0, 0, 0)
        routing_lay.setSpacing(12)
        routing_lay.addWidget(buses_section, 0)
        routing_lay.addWidget(return_section, 0)
        routing_lay.addWidget(outputs_section, 1)
        self._team_section.setObjectName("teamRow")

        self._workspace_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self._workspace_splitter.setObjectName("serverWorkspace")
        self._workspace_splitter.setAccessibleName("Répartition équipe et routage audio")
        self._workspace_splitter.setChildrenCollapsible(False)
        self._workspace_splitter.setHandleWidth(12)
        self._workspace_splitter.addWidget(self._team_section)
        self._workspace_splitter.addWidget(routing_row)
        self._workspace_splitter.setStretchFactor(0, 1)
        self._workspace_splitter.setStretchFactor(1, 1)
        self._team_section.setMinimumHeight(260)
        routing_row.setMinimumHeight(300)
        self._workspace_splitter.setSizes([340, 360])

        console_page = QtWidgets.QWidget()
        console_lay = QtWidgets.QVBoxLayout(console_page)
        console_lay.setContentsMargins(16, 14, 16, 14)
        console_lay.addWidget(self._workspace_splitter, 1)

        # -- Settings page ---------------------------------------------------------
        server_card = Card("Serveur", "Nom affiché sur les postes, et comportement au lancement.")
        server_card.body.addWidget(make_field("NOM DU SERVEUR", self._server_name))
        server_card.body.addWidget(self._discovery_cb)
        server_card.body.addWidget(self._autostart_cb)
        server_card.body.addWidget(self._start_minimized_cb)

        return_card = Card(
            "Entrée Return",
            "Le programme (sortie vMix, console…) que les postes peuvent écouter dans leur casque.",
        )
        return_card.body.addWidget(make_field("ENTRÉE", self._return_input_device))

        devices_card = Card("Périphériques audio", "La liste se met à jour serveur arrêté.")
        devices_card.body.addWidget(self._show_all_devices)
        devices_row = QtWidgets.QHBoxLayout()
        devices_row.addWidget(self._refresh_devices_btn)
        devices_row.addWidget(self._device_status, 1)
        devices_card.body.addLayout(devices_row)

        settings_inner = QtWidgets.QWidget()
        settings_grid = QtWidgets.QGridLayout(settings_inner)
        settings_grid.setContentsMargins(16, 14, 16, 14)
        settings_grid.setSpacing(12)
        settings_grid.addWidget(server_card, 0, 0, 2, 1)
        settings_grid.addWidget(return_card, 0, 1)
        settings_grid.addWidget(devices_card, 1, 1)
        settings_grid.setColumnStretch(0, 1)
        settings_grid.setColumnStretch(1, 1)
        settings_grid.setRowStretch(2, 1)
        settings_page = QtWidgets.QScrollArea()
        settings_page.setWidgetResizable(True)
        settings_page.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        settings_page.setWidget(settings_inner)

        self._pages = QtWidgets.QStackedWidget()
        self._pages.addWidget(console_page)
        self._pages.addWidget(settings_page)

        # -- Header ------------------------------------------------------------------
        self._top_bar = TopBar("Régie intercom", "", self.windowIcon())
        self._nav = SegmentedNav(["Console", "Réglages"])
        self._nav.changed.connect(self._pages.setCurrentIndex)
        self._top_bar.center.addWidget(self._nav)
        self._server_state_banner.setMinimumWidth(130)
        self._updates = UpdateController(self, "server")
        self._top_bar.right.addWidget(self._updates.button)
        self._updates.start()
        self._top_bar.right.addWidget(self._server_state_banner)
        self._top_bar.right.addWidget(self._start_btn)
        self._top_bar.right.addWidget(self._stop_btn)
        self._top_bar.right.addWidget(self._local_station_btn)
        self._top_bar.right.addWidget(self._info_btn)
        self._top_bar.right.addWidget(make_quit_button(self))
        self._server_name.textChanged.connect(self._refresh_identity_header)
        self._server_name.editingFinished.connect(self._on_server_name_edited)
        self._discovery_cb.stateChanged.connect(self._refresh_identity_header)
        # clicked, not toggled: only the operator's own choice is remembered.
        self._discovery_cb.clicked.connect(self._on_discovery_clicked)
        self._refresh_identity_header()

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._top_bar)
        layout.addWidget(self._pages, 1)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(self._on_show_all_devices_changed)
        self._start_btn.clicked.connect(self._start_server)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        self._add_output_btn.clicked.connect(self._on_add_output)
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

        self._pending_gains: dict[tuple[str, int], float] = {}
        self._gain_timer = QtCore.QTimer(self)
        self._gain_timer.setSingleShot(True)
        self._gain_timer.setInterval(GAIN_PUSH_INTERVAL_MS)
        self._gain_timer.timeout.connect(self._flush_pending_gains)

        self._device_refresh = DeviceRefresh(self)
        self._device_refresh.inventory.connect(self._on_devices_refreshed)
        self._device_refresh.timed_out.connect(self._on_device_refresh_timed_out)
        self._device_refresh.finished.connect(self._on_device_refresh_finished)
        self._last_device_error: str = ""
        self._last_device_count: int = 0
        self._is_closing: bool = False
        self._preset_load_problem: tuple[JsonFileStatus, str] | None = None
        self._preset_problem_notified: str | None = None
        # Stopped, the window edits the preset file (the engine owns it while
        # it runs). The write leaves the Qt thread: until it lands, the edited
        # preset is kept here and read instead of the older file.
        self._preset_edit: dict | None = None
        self._preset_writer = DebouncedJsonWriter(
            self._preset_paths(),
            write_func=self._write_preset_file,
            on_error=lambda exc: self._preset_write_failed.emit(str(exc)),
            thread_name="server-gui-preset-writer",
        )
        self._preset_written.connect(self._on_preset_written)
        self._preset_write_failed.connect(self._on_preset_write_failed)
        self._devices_cache: list[DeviceInfo] = []
        # False until one inventory succeeded: an empty cache then means
        # "unknown", not "every saved card is missing".
        self._device_inventory_loaded: bool = False

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
            (self._engine_health_label, "Moteur —"),
            (self._control_health_label, "Postes —"),
            (self._udp_health_label, "Voix reçue —"),
            (self._audio_health_label, "Sorties son —"),
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

    def _update_controls_for_server_state(self) -> None:
        running = self._server is not None

        self._server_name.setReadOnly(bool(running))
        self._discovery_cb.setEnabled(not bool(running))
        enumerating = self._device_refresh.running
        can_refresh = not bool(running) and not enumerating
        self._refresh_devices_btn.setEnabled(bool(can_refresh))
        self._show_all_devices.setEnabled(bool(can_refresh))
        # Its slot reads the device inventory: never while it is being rebuilt.
        self._return_input_device.setEnabled(not enumerating)

        self._add_output_device.setEnabled(bool(running))
        self._bus_selector.setEnabled(bool(running))
        can_add_output = (
            bool(running)
            and self._add_output_device.currentData() is not None
            and self._bus_selector.currentData() is not None
        )
        self._add_output_btn.setEnabled(bool(can_add_output))
        for slider in self._bus_gain_widgets.values():
            slider.setEnabled(bool(running))
        for slider in self._input_gain_widgets.values():
            slider.setEnabled(bool(running))
        for slider in self._output_gain_widgets.values():
            slider.setEnabled(bool(running))
        self._start_btn.setVisible(not running)
        self._stop_btn.setVisible(running)

    def _open_local_station(self) -> None:
        title = "Poste sur ce PC"
        if self._local_station is not None and self._local_station.poll() is None:
            QtWidgets.QMessageBox.information(self, title, "Un poste est déjà ouvert sur ce PC depuis la Régie.")
            return
        try:
            self._local_station = open_local_client()
        except OSError as exc:
            logger.error("cannot open a local station: {}", exc)
            QtWidgets.QMessageBox.warning(self, title, f"Impossible d'ouvrir le poste :\n{exc}")

    def closeEvent(self, event) -> None:  # noqa: N802
        if minimize_on_user_close(self, event):
            return
        if self._device_refresh.close_must_wait():
            self._device_status.setText("Fermeture après la fin de l'inventaire audio…")
            event.ignore()
            return
        if not self._confirm_cut_online("Fermer py-intercom", "Arrêter le serveur et fermer ?"):
            event.ignore()
            return
        # A hung enumeration would keep the window open forever: it is
        # abandoned and run_window() ends the process once the window is gone.
        # No engine runs then (_start_server refuses during an inventory).
        self._device_refresh.abandon()
        self._is_closing = True
        self._stop_server()
        # Lands an operator change made just before closing.
        self._preset_writer.close()
        super().closeEvent(event)

    def _start_device_refresh(self) -> None:
        if self._is_closing:
            return
        if self._device_refresh.running:
            self._device_status.setText("Inventaire des périphériques déjà en cours…")
            return
        if self._server is not None:
            self._device_status.setText("Arrêtez le serveur avant d'actualiser les périphériques.")
            return

        self._start_btn.setEnabled(False)
        hostapi_filter = None if self._show_all_devices.isChecked() else "WASAPI"
        self._device_status.setText(f"Inventaire des périphériques ({hostapi_filter or 'tous'})…")
        self._last_device_error = ""
        self._last_device_count = 0
        self._device_refresh.start(hostapi_filter, hard_refresh=True)
        self._update_controls_for_server_state()

    def _on_device_refresh_timed_out(self) -> None:
        self._last_device_error = (
            f"Inventaire audio toujours en cours après {DEVICE_REFRESH_TIMEOUT_MS // 1000} s "
            "(pilote lent ou bloqué) : le serveur pourra démarrer dès qu'il se termine."
        )
        self._device_status.setText(self._last_device_error)

    def _on_device_refresh_finished(self) -> None:
        self._update_controls_for_server_state()
        if self._server is None:
            self._start_btn.setEnabled(True)

        if not self._last_device_error:
            self._device_status.setText(f"Périphériques chargés : {self._last_device_count}")
            if not self._is_closing:
                QtCore.QTimer.singleShot(0, self._maybe_autostart)

    def _preset_paths(self) -> Path:
        return Path.home() / "py-intercom" / "server_preset.json"

    def _read_server_preset(self) -> dict | None:
        if self._preset_edit is not None:
            # An edit still on its way to the file is the newest preset.
            return copy.deepcopy(self._preset_edit)
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
            "incertaine. Utilisez « Réinitialiser la configuration » (bouton ⓘ) ou restaurez la sauvegarde.\n\n"
            f"Fichier : {path}\n"
            f"Sauvegarde ({backup_state}) : {backup}\n"
            f"État : {status.value}\n"
            f"Détail : {detail}",
        )

    def _load_preset_preview(self) -> None:
        if self._server is not None:
            return

        # The engine's own parser, against the last inventory (never
        # enumerated on the GUI thread): the preview shows what Start loads.
        data = self._read_server_preset()
        preset = parse_server_preset(data if data is not None else {}, self._devices_cache)

        buses = {
            definition.bus_id: {
                "bus_id": definition.bus_id,
                "name": definition.name,
                "gain_db": float(preset.bus_gain_db[definition.bus_id]),
                "feed_to_regie": bool(preset.bus_feed_to_regie[definition.bus_id]),
            }
            for definition in FIXED_BUSES
        }
        snap = {
            int(client_id_from_uuid(client_uuid)): {
                "name": profile.name,
                "client_uuid": client_uuid,
                "input_gain_db": float(profile.input_gain_db),
                "addr": None,
                "control_connected": False,
            }
            for client_uuid, profile in preset.profiles.items()
        }
        # An output whose card is absent is listed too: the engine keeps it
        # in the preset and the strip names the missing card.
        outs = {
            output_id: {
                "device": output.device,
                "device_name": output.saved.name,
                "bus_id": int(output.bus_id),
                "gain_db": float(output.gain_db),
            }
            for output_id, output in enumerate(preset.outputs)
        }

        self._refresh_bus_selectors(buses)
        self._refresh_bus_strips(buses)
        self._refresh_output_strips(outs, buses)
        self._refresh_client_strips(snap)
        # Showing the preset must not write it back (stopped-state slots do).
        self._return_enabled.blockSignals(True)
        try:
            self._return_enabled.setChecked(preset.return_enabled)
        finally:
            self._return_enabled.blockSignals(False)
        self._select_return_input(preset.return_device, saved_name=preset.return_saved.name)

    def _reset_total_config(self) -> None:
        if self._server is not None:
            QtWidgets.QMessageBox.warning(self, "Serveur en marche", "Arrêtez d'abord le serveur.")
            return

        if not self._confirm(
            "Réinitialiser la configuration",
            "Supprimer le preset serveur (sorties, postes connus, gains) et repartir de zéro ?\n"
            "La sauvegarde .bak est conservée.",
        ):
            return

        # A pending edit must not bring the deleted file back.
        self._settle_preset_edit(discard=True)
        server_preset = self._preset_paths()
        with best_effort("server_preset.with_suffix()"):
            tmp = server_preset.with_suffix(server_preset.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()

        with best_effort("delete server preset"):
            if server_preset.exists():
                server_preset.unlink()

        self._preset_load_problem = None
        self._preset_problem_notified = None

        self._clear_output_strips()
        self._load_preset_preview()

    def _refresh_bus_selectors(self, buses: dict[int, dict]) -> None:
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

    def _refresh_bus_strips(self, buses: dict[int, dict]) -> None:
        running = self._server is not None
        for definition in FIXED_BUSES:
            bid = int(definition.bus_id)
            b = buses.get(bid, {})
            self._bus_strips[bid].set_title(str(b.get("name") or definition.name))
            slider = self._bus_gain_widgets[bid]
            slider.setEnabled(running)
            if not self._gain_held("bus", bid, slider):
                gain_value = _db_value(b.get("gain_db", 0.0))
                slider.blockSignals(True)
                try:
                    slider.setValue(gain_value)
                finally:
                    slider.blockSignals(False)
                self._bus_gain_labels[bid].setText(format_db(gain_value))
            feed = self._bus_feed_widgets.get(bid)
            if feed is not None:
                feed.blockSignals(True)
                try:
                    feed.setEnabled(running)
                    feed.setChecked(bool(b.get("feed_to_regie", definition.default_feed_to_regie)))
                finally:
                    feed.blockSignals(False)

    def _on_bus_feed_to_regie_changed(self, bus_id: int, state: int) -> None:
        if self._server is None or int(bus_id) not in BUS_BY_ID or int(bus_id) == REGIE_BUS_ID:
            return
        self._server.set_bus_feed_to_regie(int(bus_id), is_checked(state))

    def _on_bus_gain_changed(self, bus_id: int, value: int) -> None:
        if int(bus_id) not in BUS_BY_ID:
            return
        label = self._bus_gain_labels.get(int(bus_id))
        if label is not None:
            label.setText(format_db(value))
        self._queue_gain("bus", int(bus_id), float(value))

    def _queue_gain(self, kind: str, target_id: int, value: float) -> None:
        if self._server is None:
            return
        self._pending_gains[(kind, int(target_id))] = float(value)
        if not self._gain_timer.isActive():
            self._flush_pending_gains()

    def _gain_held(self, kind: str, target_id: int, fader: Fader) -> bool:
        """True while the fader shows a value the engine has not applied yet."""
        return fader.isSliderDown() or (kind, int(target_id)) in self._pending_gains

    def _flush_pending_gains(self) -> None:
        pending, self._pending_gains = self._pending_gains, {}
        if self._server is None or not pending:
            return
        for (kind, target_id), value in pending.items():
            if kind == "bus":
                self._server.set_bus_gain(target_id, value)
            else:
                self._server.set_client_input_gain(target_id, value)
        self._gain_timer.start()

    def _build_output_strip(self, oid: int, bus_names: dict[int, str]) -> ChannelStrip:
        dev_cb = compact_combo(QtWidgets.QComboBox())
        patch_combo(dev_cb)
        dev_cb.blockSignals(True)
        for label, idx in self._output_devices_cache:
            dev_cb.addItem(label, idx)
        dev_cb.blockSignals(False)
        dev_cb.currentIndexChanged.connect(lambda _i, out_id=int(oid): self._on_output_device_changed(out_id))

        bus_cb = compact_combo(QtWidgets.QComboBox())
        patch_combo(bus_cb)
        bus_cb.blockSignals(True)
        for bid, name in bus_names.items():
            bus_cb.addItem(name, int(bid))
        bus_cb.blockSignals(False)
        bus_cb.currentIndexChanged.connect(lambda _i, out_id=int(oid): self._on_output_bus_changed(out_id))

        fader = Fader(-60, int(MAX_GAIN_DB))
        fader.valueChanged.connect(lambda value, out_id=int(oid): self._on_output_gain_changed(out_id, value))
        meter = LevelMeter()
        value = readout()
        strip = ChannelStrip(f"Sortie {int(oid) + 1}", fader=fader, meter=meter, readout=value, width=132)
        indicator = StatusIndicator(size=14)
        remove = QtWidgets.QToolButton()
        remove.setText("✕")
        remove.setProperty("class", "ghost")
        remove.setFixedSize(22, 22)
        remove.setToolTip("Supprimer cette sortie")
        remove.setAccessibleName(f"Supprimer la sortie {int(oid) + 1}")
        remove.clicked.connect(lambda _checked=False, out_id=int(oid): self._on_remove_output(out_id))
        # Shown only on a failed stream: the supervision retries it with a
        # backoff, this retries it now (a card just switched back on).
        reopen = QtWidgets.QToolButton()
        reopen.setText("↻")
        reopen.setProperty("class", "ghost")
        reopen.setFixedSize(22, 22)
        reopen.setToolTip("Rouvrir cette sortie maintenant")
        reopen.setAccessibleName(f"Rouvrir la sortie {int(oid) + 1}")
        reopen.setVisible(False)
        reopen.clicked.connect(lambda _checked=False, out_id=int(oid): self._on_reopen_output(out_id))
        strip.header.addWidget(indicator)
        strip.header.addWidget(reopen)
        strip.header.addWidget(remove)
        strip.reserve_top(2)
        strip.top.addWidget(dev_cb)
        strip.top.addWidget(bus_cb)

        self._output_strips[int(oid)] = strip
        self._output_device_widgets[int(oid)] = dev_cb
        self._output_bus_widgets[int(oid)] = bus_cb
        self._output_gain_widgets[int(oid)] = fader
        self._output_gain_labels[int(oid)] = value
        self._output_vu_widgets[int(oid)] = meter
        self._output_status_widgets[int(oid)] = indicator
        self._output_reopen_widgets[int(oid)] = reopen
        return strip

    def _clear_output_strips(self) -> None:
        self._output_rack.clear()
        self._output_row_ids = []
        for cache in (
            self._output_strips,
            self._output_device_widgets,
            self._output_bus_widgets,
            self._output_gain_widgets,
            self._output_gain_labels,
            self._output_vu_widgets,
            self._output_status_widgets,
            self._output_reopen_widgets,
        ):
            cache.clear()

    def _refresh_output_strips(self, outs: dict[int, dict], buses: dict[int, dict]) -> None:
        output_ids = sorted(outs.keys())
        bus_names = {
            definition.bus_id: str(buses.get(definition.bus_id, {}).get("name") or definition.name)
            for definition in FIXED_BUSES
            if definition.bus_id in buses
        }
        if output_ids != self._output_row_ids:
            if self._fader_in_use(self._output_gain_widgets):
                return  # rebuilt on a later tick, not under the operator's hand
            self._clear_output_strips()
            for oid in output_ids:
                self._output_rack.add_strip(self._build_output_strip(int(oid), bus_names))
            self._output_row_ids = list(output_ids)

        running = self._server is not None
        for oid in output_ids:
            st = outs.get(int(oid), {})

            dev_cb = self._output_device_widgets[int(oid)]
            cur_dev = st.get("device")
            dev_cb.blockSignals(True)
            try:
                # Never leave another card on display for a missing device.
                i = dev_cb.findData(int(cur_dev)) if cur_dev is not None else -1
                dev_cb.setCurrentIndex(i)
                if cur_dev is None:
                    dev_cb.setPlaceholderText(self._missing_device_text(str(st.get("device_name") or "")))
                elif i < 0:
                    dev_cb.setPlaceholderText(f"périphérique #{int(cur_dev)} hors liste")
                dev_cb.setEnabled(running)
                dev_cb.setToolTip(dev_cb.currentText() or dev_cb.placeholderText())
            finally:
                dev_cb.blockSignals(False)

            bus_cb = self._output_bus_widgets[int(oid)]
            bus_cb.blockSignals(True)
            try:
                cur_bus = st.get("bus_id")
                if cur_bus is not None:
                    i = bus_cb.findData(int(cur_bus))
                    if i >= 0:
                        bus_cb.setCurrentIndex(i)
                bus_cb.setEnabled(running)
            finally:
                bus_cb.blockSignals(False)
            definition = BUS_BY_ID.get(int(bus_cb.currentData())) if bus_cb.currentData() is not None else None
            self._output_strips[int(oid)].set_color(definition.color if definition else None)

            gain_value = _db_value(st.get("gain_db", 0.0))
            slider = self._output_gain_widgets[int(oid)]
            slider.blockSignals(True)
            try:
                slider.setEnabled(running)
                slider.setValue(gain_value)
            finally:
                slider.blockSignals(False)
            self._output_gain_labels[int(oid)].setText(format_db(gain_value))

            if not running:
                # Live levels belong to the VU loop only.
                self._output_vu_widgets[int(oid)].set_level(-60.0)
            indicator = self._output_status_widgets[int(oid)]
            label = f"Sortie {int(oid) + 1}"
            error = str(st.get("stream_error") or "")
            if cur_dev is None:
                # Without an inventory the card may well be there: not an error.
                state = "error" if self._device_inventory_loaded else "unknown"
                indicator.set_state(state, description=f"{label} : {dev_cb.placeholderText()}")
            elif error:
                indicator.set_state("error", description=f"{label} en erreur : {error}")
            elif st.get("stream_active") is True:
                indicator.set_state("online", description=f"{label} active")
            elif "stream_active" in st:
                indicator.set_state("error", description=f"{label} inactive")
            else:
                indicator.set_state("unknown", description=f"Activité de la {label.lower()} non confirmée")
            self._output_reopen_widgets[int(oid)].setVisible(running and cur_dev is not None and bool(error))

    def _on_reopen_output(self, output_id: int) -> None:
        if self._server is None:
            return
        # Reopens only the streams that are really dead: a healthy output
        # keeps playing. A failure is logged and stays on the strip.
        self._server.reopen_outputs(force=False)
        self._status_bar.showMessage(f"Sortie {int(output_id) + 1} : nouvelle tentative d'ouverture", 5000)
        self._refresh_clients()

    def _on_client_input_gain_changed(self, client_id: int, value: int) -> None:
        label = self._input_gain_labels.get(int(client_id))
        if label is not None:
            label.setText(format_db(value))
        self._queue_gain("input", int(client_id), float(value))

    def _on_output_gain_changed(self, output_id: int, value: int) -> None:
        label = self._output_gain_labels.get(int(output_id))
        if label is not None:
            label.setText(format_db(value))
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
            QtWidgets.QMessageBox.warning(self, "Sortie son", str(e))

    def _on_remove_output(self, output_id: int) -> None:
        if self._server is None:
            return
        name = f"Sortie {int(output_id) + 1}"
        device_cb = self._output_device_widgets.get(int(output_id))
        bus_cb = self._output_bus_widgets.get(int(output_id))
        details = ", ".join(
            text
            for text in (
                device_cb.currentText() if device_cb is not None else "",
                f"bus {bus_cb.currentText()}" if bus_cb is not None and bus_cb.currentText() else "",
            )
            if text
        )
        question = f"Supprimer {name}" + (f" ({details})" if details else "") + " ?\nSon audio est coupé immédiatement."
        if not self._confirm("Supprimer la sortie", question):
            return
        self._server.remove_output(int(output_id))

    def _confirm(self, title: str, text: str) -> bool:
        return confirm(self, title, text)

    def _on_output_device_changed(self, output_id: int) -> None:
        if self._server is None:
            return
        cb = self._output_device_widgets.get(int(output_id))
        if cb is None:
            return
        dev = cb.currentData()
        if dev is None:
            return
        try:
            self._server.set_output_device(int(output_id), int(dev))
        except Exception as exc:
            # The engine kept the previous device; the next structure tick
            # puts the combo back on it.
            logger.warning("output {} device change failed: {}", int(output_id), exc)
            self._report_device_error(f"Sortie {int(output_id) + 1} : ouverture impossible : {exc}")

    def _report_device_error(self, text: str) -> None:
        """Show a failed device change where the operator is looking."""
        self._device_status.setText(text)
        self._status_bar.showMessage(text, 15000)

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
        if self._is_closing:
            return
        if error:
            # No modal box: an unattended autostart must not wait on a click.
            self._last_device_error = error
            self._device_status.setText(f"Échec de l'inventaire audio : {error}")
            logger.warning("device refresh failed: {}", error)
            return

        # A late inventory (after the timeout warning) is as good as any.
        self._last_device_error = ""
        self._last_device_count = int(len(devs))
        self._device_status.setText(f"Périphériques chargés : {self._last_device_count}")
        self._populate_device_combos(list(devs))
        self._output_row_ids = []
        self._load_preset_preview()

    def _populate_device_combos(self, devs: list[DeviceInfo]) -> None:
        """Fill every device selector from one inventory, keeping selections."""
        self._devices_cache = list(devs)
        self._device_inventory_loaded = True

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

        # Output strips are rebuilt by the caller; the Return keeps its
        # selection until the preset (or the engine) says otherwise.
        current_in = self._return_input_device.currentData()
        self._return_input_device.blockSignals(True)
        try:
            self._return_input_device.clear()
            self._return_input_device.addItem("(aucune)", None)
            for d in devs:
                if d.supports_input:
                    self._return_input_device.addItem(format_device_label(d), d.index)
        finally:
            self._return_input_device.blockSignals(False)
        self._select_return_input(current_in)

    def _remap_device_indices(self) -> None:
        """Re-key the selectors after the engine re-initialised PortAudio.

        ``load_preset`` restarts PortAudio, which may renumber the devices if
        one was plugged in or removed since the last inventory. Selectors
        carry PortAudio indexes, so they are rebuilt from the same validated
        devices under their new indexes (vanished ones are dropped).
        """
        if not self._device_inventory_loaded:
            return  # nothing inventoried yet, so nothing to re-key
        try:
            fresh = list_devices(hostapi_substring=None, hard_refresh=False, validate=False)
        except Exception as exc:
            logger.warning("device list after preset load failed: {}", exc)
            return

        def identity(d: DeviceInfo) -> tuple[str, str, int, int]:
            return (d.name, d.hostapi, int(d.max_input_channels), int(d.max_output_channels))

        identity_at = {int(d.index): identity(d) for d in fresh}
        indexes_by_identity: dict[tuple[str, str, int, int], list[int]] = {}
        for d in fresh:
            indexes_by_identity.setdefault(identity(d), []).append(int(d.index))
        remapped: list[DeviceInfo] = []
        for d in self._devices_cache:
            if identity_at.get(int(d.index)) == identity(d):
                remapped.append(d)
                continue
            candidates = indexes_by_identity.get(identity(d), [])
            if len(candidates) == 1:  # ambiguous duplicates are dropped
                remapped.append(replace(d, index=candidates[0]))
        self._populate_device_combos(remapped)

    def _missing_device_text(self, saved_name: str) -> str:
        """Placeholder for a saved device that did not resolve to a card."""
        if not self._device_inventory_loaded:
            return f"inventaire indisponible : {saved_name}" if saved_name else "inventaire indisponible"
        return f"absent : {saved_name}" if saved_name else "périphérique absent"

    def _select_return_input(self, device: int | None, *, saved_name: str = "") -> None:
        """Show ``device`` in the Return selector without any side effect.

        A device that is not listed, or a saved one (``saved_name``) that
        did not resolve, leaves the selector blank with an explicit
        placeholder rather than on another card.
        """
        combo = self._return_input_device
        if device is None:
            i = -1 if saved_name else 0
            combo.setPlaceholderText(self._missing_device_text(saved_name))
        else:
            i = combo.findData(int(device))
            combo.setPlaceholderText(f"périphérique #{int(device)} hors liste")
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(i)
        finally:
            combo.blockSignals(False)
        self._refresh_return_caption()

    def _refresh_return_caption(self) -> None:
        combo = self._return_input_device
        if combo.currentIndex() < 0:
            caption = combo.placeholderText()
        elif combo.currentData() is None:
            caption = "aucune entrée"
        else:
            caption = combo.currentText()
        self._return_strip.set_caption(caption)

    def _on_return_enabled_changed(self, state: int) -> None:
        enabled = is_checked(state)
        if self._server is not None:
            self._server.set_return_enabled(enabled)
            return

        def change(data: dict) -> None:
            data["return_enabled"] = bool(enabled)

        self._write_stopped_preset(change)

    def _write_stopped_preset(self, change: Callable[[dict], None]) -> bool:
        """Apply an operator change to the preset file while stopped.

        The engine owns the preset while it runs; stopped, the window edits
        the file so the choice survives the next start (which reloads it).
        The file is written off the Qt thread: an antivirus lock can hold a
        write for a while, never the window.
        """
        data = self._read_server_preset()
        if data is None:
            self._device_status.setText("Preset illisible : modification non enregistrée")
            return False
        change(data)
        self._preset_edit = data
        self._preset_writer.request(data)
        return True

    def _write_preset_file(self, path: Path, data: dict) -> None:
        """Writer thread: write the edited preset, then report it to the Qt thread."""
        atomic_write_json(path, data)
        self._preset_written.emit(data)

    def _on_preset_written(self, data: dict) -> None:
        # Forgotten only if no newer edit is waiting behind this write.
        if self._preset_edit == data:
            self._preset_edit = None

    def _on_preset_write_failed(self, detail: str) -> None:
        # The edit stays the shown preset; the next one writes it again.
        logger.warning("server preset write failed: {}", detail)
        self._device_status.setText(f"Preset non enregistré : {detail}")

    def _settle_preset_edit(self, *, discard: bool = False) -> None:
        """Hand the preset file back with no write pending or in flight.

        ``discard`` drops an edit not written yet; otherwise it is written
        first. A failed write was already reported when it happened.
        """
        if discard:
            self._preset_writer.cancel_pending()
        self._preset_writer.flush(raise_on_error=False)
        self._preset_edit = None

    def _on_show_all_devices_changed(self, state: int) -> None:
        self._settings.setValue("show_all_devices", bool(is_checked(state)))
        self._start_device_refresh()

    def _on_return_input_device_changed(self, _idx: int) -> None:
        dev = self._return_input_device.currentData()
        device_idx = int(dev) if dev is not None else None
        self._refresh_return_caption()
        if self._server is not None:
            try:
                self._server.set_return_input_device(device_idx)
            except Exception as exc:
                # The engine kept its previous input: show that one again.
                logger.warning("return input change failed: {}", exc)
                self._report_device_error(f"Entrée Return : ouverture impossible : {exc}")
                self._select_return_input(self._server._return_input_device)
            return

        # The selector only lists inventoried devices: no PortAudio call here.
        info = next((d for d in self._devices_cache if int(d.index) == device_idx), None)

        def change(data: dict) -> None:
            data["return_input_device"] = device_idx
            data["return_input_device_name"] = str(info.name) if info is not None else ""
            data["return_input_device_hostapi"] = str(info.hostapi) if info is not None else ""

        self._write_stopped_preset(change)

    def _forget_selected_client(self) -> None:
        if self._selected_client_id is None:
            return
        client_id = int(self._selected_client_id)

        if self._server is not None:
            st = self._server.get_clients_snapshot().get(client_id, {})
            if st.get("control_connected") and not self._confirm(
                "Oublier le poste",
                f"{st.get('name') or f'Poste {client_id}'} est en ligne : il sera expulsé "
                "et son profil (nom, gain) effacé.\nContinuer ?",
            ):
                return
            self._server.forget_client(int(client_id))
            return

        client_uuid = str(self._client_uuid_by_id.get(int(client_id), "") or "")
        if not client_uuid:
            return

        def change(data: dict) -> None:
            clients = data.get("clients") if isinstance(data.get("clients"), dict) else {}
            clients.pop(client_uuid, None)
            data["clients"] = clients

        if self._write_stopped_preset(change):
            self._load_preset_preview()

    def _start_server(self) -> None:
        if self._server is not None:
            return
        if self._device_refresh.running:
            self._device_status.setText("Attendez la fin de l'inventaire audio avant de démarrer.")
            return
        # The engine loads the file and owns it from now on.
        self._settle_preset_edit()

        # The preview strips belong to the stopped state; rebuild from the engine.
        self._clear_client_strips()
        self._clear_output_strips()

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
            self._abort_start()
            QtWidgets.QMessageBox.critical(
                self,
                "Échec du démarrage",
                f"Impossible d'ouvrir le port UDP {int(AUDIO_UDP_PORT)}. "
                f"Un autre serveur py-intercom est peut-être déjà lancé.\n\nDétail: {e}",
            )
            return
        except Exception as e:
            self._abort_start()
            QtWidgets.QMessageBox.critical(self, "Échec du démarrage", str(e))
            return

        try:
            # Loading only: a save here would rewrite the preset without the
            # outputs whose card is currently missing.
            self._server.load_preset()
            self._remap_device_indices()
            # Sync GUI back from the preset-loaded server state so the checkbox
            # reflects reality before start() opens the return stream.
            self._return_enabled.blockSignals(True)
            self._return_enabled.setChecked(bool(self._server._return_enabled))
            self._return_enabled.blockSignals(False)
            return_device = self._server._return_input_device
            saved_name = ""
            if return_device is None:
                # The engine drops a saved card it cannot find: name it.
                saved = self._read_server_preset() or {}
                saved_name = str(saved.get("return_input_device_name") or "")
            self._select_return_input(return_device, saved_name=saved_name)
        except PresetLoadError as e:
            self._abort_start()
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
            self._abort_start()
            QtWidgets.QMessageBox.critical(self, "Preset illisible", f"Impossible de charger le preset : {e}")
            return

        try:
            self._server.start()
        except Exception as e:
            logger.exception("server start failed")
            self._abort_start()
            QtWidgets.QMessageBox.critical(self, "Échec du démarrage", str(e))
            return

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._update_controls_for_server_state()
        self._refresh_identity_header()

        with best_effort("_server.get_clients_snapshot()"):
            snap = self._server.get_clients_snapshot()
            buses = self._server.get_buses_snapshot()
            outs = self._server.get_outputs_snapshot()
            self._refresh_bus_selectors(buses)
            self._refresh_bus_strips(buses)
            self._refresh_client_strips(snap)
            self._refresh_output_strips(outs, buses)
            self._update_server_health(
                self._server.get_stats_snapshot(),
                snap,
                outs,
            )

        # Dead output and Return streams are reopened by the engine's stream
        # supervision, healthy ones are never touched.
        self._timer.start()
        self._vu_timer.start()

    def _abort_start(self) -> None:
        """Release a half-started engine and show the preset again."""
        server, self._server = self._server, None
        if server is not None:
            # stop() also frees the UDP port bound by the constructor.
            with best_effort("server.stop() after failed start"):
                server.stop()
        self._load_preset_preview()

    def _on_stop_clicked(self) -> None:
        if self._server is None:
            return
        if self._confirm_cut_online("Arrêter le serveur", "Arrêter le serveur ?"):
            self._stop_server()

    def _confirm_cut_online(self, title: str, question: str) -> bool:
        """Ask before cutting the intercom of stations that are online."""
        if self._server is None:
            return True
        online = sum(1 for st in self._server.get_clients_snapshot().values() if st.get("control_connected"))
        return not online or self._confirm(
            title, f"{online} poste(s) en ligne perdront l'intercom immédiatement.\n{question}"
        )

    def _stop_server(self) -> None:
        if self._server is None:
            return
        with best_effort("apply the last fader values before stopping"):
            self._flush_pending_gains()
        self._gain_timer.stop()
        try:
            self._timer.stop()
            self._vu_timer.stop()
            self._server.stop()
        finally:
            self._server = None
            self._start_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
            self._clear_client_strips()
            self._clear_output_strips()
            self._select_client(None)
            self._refresh_identity_header()
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
            for meter in (self._return_vu, *self._bus_vu_widgets.values()):
                meter.set_level(-60.0)

            self._update_controls_for_server_state()
            self._load_preset_preview()

    def _refresh_clients(self) -> None:
        if self._server is None:
            return
        snap = self._server.get_clients_snapshot()
        buses = self._server.get_buses_snapshot()
        outs = self._server.get_outputs_snapshot()
        self._refresh_bus_selectors(buses)
        self._refresh_bus_strips(buses)
        self._refresh_client_strips(snap)
        self._refresh_output_strips(outs, buses)

        self._update_controls_for_server_state()

    def _refresh_vu_and_health(self) -> None:
        if self._server is None:
            return
        clients = self._server.get_clients_snapshot()
        outputs = self._server.get_outputs_snapshot()
        stats = self._server.get_stats_snapshot()

        # Every level is a float the engine produced; a client or output that
        # vanished since the structure tick reads as silence.
        self._return_vu.set_level(float(stats.get("return_vu_dbfs", -60.0)))
        self._regie_vu.set_level(float(stats.get("regie_vu_dbfs", -60.0)))
        for client_id, vu in self._vu_widgets.items():
            vu.set_level(float(clients.get(int(client_id), {}).get("vu_dbfs", -60.0)))
        for output_id, vu in self._output_vu_widgets.items():
            vu.set_level(float(outputs.get(int(output_id), {}).get("vu_dbfs", -60.0)))
        # Each bus after its fader, measured by the mixer even with no output
        # routed to it; Régie keeps its own meter (the mix it sends out).
        bus_vu_dbfs = stats.get("bus_vu_dbfs", {})
        for bid, meter in self._bus_vu_widgets.items():
            if bid != REGIE_BUS_ID:
                meter.set_level(float(bus_vu_dbfs.get(int(bid), -60.0)))
        self._update_talk_lamps(clients)

        self._update_server_health(stats, clients, outputs)

    def _show_geek_info(self) -> None:
        port = int(AUDIO_UDP_PORT)
        ctrl_port = port + 1
        running = self._server is not None
        snap = self._server.get_clients_snapshot() if self._server is not None else {}
        stats = self._server.get_stats_snapshot() if self._server is not None else {}

        selected = self._selected_client_id

        lines = [
            f"Audio UDP port: {port}",
            f"Control TCP port: {ctrl_port}",
            f"Running: {running}",
            f"Clients: {len(snap)}",
            f"Control connected: {stats.get('control_connected_clients', '-')}",
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
                    f"Jitter buffer: {st.get('jb_buf', '-')} buffered, {st.get('jb_missing', '-')} missing, "
                    f"{st.get('jb_late_dropped', '-')} late, {st.get('jb_concealed', '-')} concealed, "
                    f"{st.get('jb_trimmed', '-')} trimmed, {st.get('jb_resets', '-')} resets",
                ]
            )

        if self._server is not None:
            server_preset = Path(self._server.get_preset_paths_snapshot()["server_preset"])
        else:
            server_preset = self._preset_paths()
        exists = os.path.exists(server_preset)  # False on any OS error, never raises

        lines.extend(["", f"Server preset: {server_preset}", f"Preset exists: {exists}"])

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Informations techniques")
        box.setText("\n".join(lines))
        reset_btn = box.addButton("Réinitialiser la configuration", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is reset_btn:
            self._reset_total_config()

    def _build_client_strip(self, client_id: int) -> ChannelStrip:
        fader = Fader(-60, int(MAX_GAIN_DB))
        fader.valueChanged.connect(lambda value, cid=int(client_id): self._on_client_input_gain_changed(cid, value))
        meter = LevelMeter()
        value = readout()
        strip = ChannelStrip(str(client_id), fader=fader, meter=meter, readout=value, color=TOKENS["accent"], width=100)
        indicator = StatusIndicator(size=14)
        strip.header.addWidget(indicator)
        lamps_row = QtWidgets.QWidget()
        lamps_lay = QtWidgets.QHBoxLayout(lamps_row)
        lamps_lay.setContentsMargins(0, 0, 0, 0)
        lamps_lay.setSpacing(3)
        lamps: dict[int, Lamp] = {}
        for definition in FIXED_BUSES:
            lamp = Lamp(definition.name[:1].upper(), definition.color)
            lamp.setToolTip(f"Parle vers {definition.name}")
            lamps_lay.addWidget(lamp)
            lamps[int(definition.bus_id)] = lamp
        lamps_lay.addStretch(1)
        diag = QtWidgets.QLabel("")
        diag.setProperty("role", "diag")
        diag.setFont(mono_font(10))
        diag.setToolTip(
            "Réception de la voix de ce poste. « Réseau OK » : rien de perdu. « Pertes réparées » : "
            "des paquets manquent mais sont reconstruits (inaudible). « Coupures » : des trous sont comblés "
            "(audible), vérifier le réseau du poste."
        )
        strip.reserve_top(2)
        strip.top.addWidget(lamps_row)
        strip.top.addWidget(diag)
        strip.setToolTip("Cliquer pour sélectionner ce poste")
        strip.clicked.connect(lambda cid=int(client_id): self._select_client(cid))

        self._client_strips[int(client_id)] = strip
        self._input_gain_widgets[int(client_id)] = fader
        self._input_gain_labels[int(client_id)] = value
        self._vu_widgets[int(client_id)] = meter
        self._status_widgets[int(client_id)] = indicator
        self._talk_lamps[int(client_id)] = lamps
        self._client_diag[int(client_id)] = (diag, CounterRates())
        return strip

    def _clear_client_strips(self) -> None:
        self._client_rack.clear()
        self._client_row_ids = []
        for cache in (
            self._client_strips,
            self._input_gain_widgets,
            self._input_gain_labels,
            self._vu_widgets,
            self._status_widgets,
            self._talk_lamps,
            self._client_diag,
        ):
            cache.clear()

    def _select_client(self, client_id: int | None) -> None:
        self._selected_client_id = client_id
        for cid, strip in self._client_strips.items():
            strip.set_flag("selected", cid == client_id)
        self._forget_client_btn.setEnabled(client_id is not None)

    @staticmethod
    def _fader_in_use(faders: Mapping[int, Fader]) -> bool:
        return any(fader.isSliderDown() for fader in faders.values())

    def _refresh_client_diag(self, client_id: int, st: Mapping[str, object], *, online: bool, now: float) -> None:
        label, rates = self._client_diag[client_id]
        if not online:
            rates.reset()
            label.setText("")
            return
        per_second = rates.update({"fec": int(st.get("jb_fec", 0)), "plc": int(st.get("jb_concealed", 0))}, now)
        alert = per_second["plc"] > 0.0
        if alert:
            label.setText(f"Coupures {format_rate(per_second['plc'])}")
        elif per_second["fec"] > 0.0:
            label.setText("Pertes réparées")
        else:
            label.setText("Réseau OK")
        if bool(label.property("alert")) != alert:
            label.setProperty("alert", alert)
            repolish(label)

    def _refresh_client_strips(self, snap: dict[int, dict]) -> None:
        now = time.monotonic()
        client_ids = sorted(snap.keys())
        if client_ids != self._client_row_ids:
            if self._fader_in_use(self._input_gain_widgets):
                return  # rebuilt on a later tick, not under the operator's hand
            self._clear_client_strips()
            for client_id in client_ids:
                self._client_rack.add_strip(self._build_client_strip(int(client_id)))
            self._client_row_ids = list(client_ids)
            if self._selected_client_id not in client_ids:
                self._selected_client_id = None
            self._select_client(self._selected_client_id)

        online = 0
        for client_id in client_ids:
            st = snap.get(int(client_id), {})
            with best_effort("record client UUID"):
                self._client_uuid_by_id[int(client_id)] = str(st.get("client_uuid") or "")
            strip = self._client_strips[int(client_id)]
            client_name = str(st.get("name") or f"Poste {client_id}")
            strip.set_title(client_name)
            addr = st.get("addr")
            strip.set_caption(str(addr[0]) if addr else "hors ligne")

            transport_state = derive_client_transport_state(st)
            indicator = self._status_widgets[int(client_id)]
            if transport_state is ClientTransportState.OFFLINE:
                indicator.set_state("offline", description=f"{client_name} : hors ligne")
            elif transport_state is ClientTransportState.IDLE:
                indicator.set_state("idle", description=f"{client_name} : en ligne, au repos")
            elif transport_state is ClientTransportState.DEGRADED:
                indicator.set_state("degraded", description=f"{client_name} : en parole, audio UDP absent ou ancien")
            else:
                indicator.set_state("online", description=f"{client_name} : en parole, audio UDP actif")
            disconnected = transport_state is ClientTransportState.OFFLINE
            online += 0 if disconnected else 1
            strip.set_flag("offline", disconnected)
            self._refresh_client_diag(int(client_id), st, online=not disconnected, now=now)

            slider = self._input_gain_widgets[int(client_id)]
            slider.setEnabled(self._server is not None)
            if not self._gain_held("input", int(client_id), slider):
                gain_value = _db_value(st.get("input_gain_db", 0.0))
                slider.blockSignals(True)
                try:
                    slider.setValue(gain_value)
                finally:
                    slider.blockSignals(False)
                self._input_gain_labels[int(client_id)].setText(format_db(gain_value))

        total = len(client_ids)
        self._team_section.set_count(f"{online} en ligne sur {total}" if total else "")

    def _update_talk_lamps(self, snap: Mapping[int, Mapping[str, object]]) -> None:
        for client_id, lamps in self._talk_lamps.items():
            st = snap.get(int(client_id), {})
            ptt = st.get("ptt_buses") if isinstance(st.get("ptt_buses"), dict) else {}
            online = bool(st.get("control_connected"))
            for bus_id, lamp in lamps.items():
                lamp.set_on(online and bool(ptt.get(bus_id) or ptt.get(str(bus_id))))

    def _on_server_name_edited(self) -> None:
        self._settings.setValue("server_name", self._server_name.text().strip() or "py-intercom")

    def _on_discovery_clicked(self, enabled: bool) -> None:
        self._settings.setValue("discovery_enabled", bool(enabled))

    def _on_autostart_changed(self, state: int) -> None:
        self._settings.setValue("autostart", bool(is_checked(state)))

    def _on_start_minimized_changed(self, state: int) -> None:
        self._settings.setValue("start_minimized", bool(is_checked(state)))

    def _refresh_identity_header(self, *_args) -> None:
        name = self._server_name.text().strip() or "py-intercom"
        self._top_bar.title_label.setText(name)
        parts = [f"Audio UDP {int(AUDIO_UDP_PORT)}", f"contrôle TCP {int(AUDIO_UDP_PORT) + 1}"]
        parts.append("annoncé sur le LAN" if self._discovery_cb.isChecked() else "non annoncé")
        self._top_bar.subtitle_label.setText(" · ".join(parts))

    def _maybe_autostart(self) -> None:
        if self._server is None and bool(self._autostart_cb.isChecked()):
            self._start_server()


def run_gui(minimized: bool = False) -> int:
    # Two Régies on one PC would fight over the UDP port and the preset.
    return run_window(
        ServerWindow,
        lambda win: minimized or win._start_minimized_cb.isChecked(),
        single_instance_role="server",
    )
