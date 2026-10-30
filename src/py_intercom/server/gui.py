from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .. import __version__
from ..common.buses import BUS_BY_ID, FIXED_BUSES, REGIE_BUS_ID
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
)
from ..common.constants import AUDIO_UDP_PORT, MAX_GAIN_DB
from ..common.devices import format_device_label, list_devices, resolve_device
from ..common.gui_utils import DeviceWorker, is_checked
from ..common.identity import client_id_from_uuid
from ..common.jsonio import JsonFileStatus, atomic_write_json, read_json_file_result
from ..common.logging import best_effort
from ..common.preset_schema import PresetSchemaError, migrate_preset, stamp_preset
from ..common.theme import (
    TOKENS,
    StatusIndicator,
    apply_theme,
    make_field,
    patch_combo,
    set_status_label,
)
from .health import (
    ClientTransportState,
    derive_client_transport_state,
    derive_server_operational_health,
)
from .server import IntercomServer, PresetLoadError

SERVER_STRUCTURE_REFRESH_INTERVAL_MS = 500
SERVER_VU_REFRESH_INTERVAL_MS = 67


def _db_value(raw: object) -> int:
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return 0


class ServerWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Py-Intercom Server {__version__}")
        self.setMinimumSize(1040, 680)
        self.resize(1320, 820)
        self._set_app_icon()

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

        self._server_name = QtWidgets.QLineEdit("py-intercom")
        self._server_name.setPlaceholderText("Nom annoncé aux postes")
        self._discovery_cb = QtWidgets.QCheckBox("Annoncer le serveur sur le réseau local")
        self._discovery_cb.setChecked(True)
        self._discovery_cb.setToolTip("Broadcast LAN beacon so clients can auto-detect this server")
        self._autostart_cb = QtWidgets.QCheckBox("Démarrer le serveur au lancement")
        self._autostart_cb.setChecked(bool(self._settings.value("autostart", False, type=bool)))
        self._start_minimized_cb = QtWidgets.QCheckBox("Démarrer réduit")
        self._start_minimized_cb.setChecked(bool(self._settings.value("start_minimized", False, type=bool)))

        self._return_enabled = chip("ACTIF", "Diffuser l'entrée Return (programme) aux postes qui l'écoutent")
        self._return_enabled.setChecked(False)
        self._return_input_device = QtWidgets.QComboBox()
        patch_combo(self._return_input_device)
        self._return_input_device.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._return_input_device.setMinimumContentsLength(20)
        self._return_vu = LevelMeter()
        self._regie_vu = LevelMeter()
        self._show_all_devices = QtWidgets.QCheckBox("Afficher tous les périphériques (pas seulement WASAPI)")
        self._show_all_devices.setChecked(
            bool(self._settings.value("show_all_devices", False, type=bool))
        )
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

        self._client_rack = StripRack(
            "Aucun poste pour l'instant.\nLes postes apparaissent ici dès leur première connexion."
        )
        self._forget_client_btn = QtWidgets.QPushButton("Oublier le poste")
        self._forget_client_btn.setProperty("class", "secondary")
        self._forget_client_btn.setToolTip("Retirer le poste sélectionné de la liste et du preset")
        self._forget_client_btn.setEnabled(False)
        self._forget_client_btn.clicked.connect(self._forget_selected_client)

        self._team_section = Section("Équipe")
        self._team_section.actions.addWidget(self._forget_client_btn)
        self._team_section.body.addWidget(self._client_rack, 1)

        # -- Buses: fixed, built once ------------------------------------------
        self._bus_strips: dict[int, ChannelStrip] = {}
        self._bus_gain_widgets: dict[int, Fader] = {}
        self._bus_gain_labels: dict[int, QtWidgets.QLabel] = {}
        self._bus_feed_widgets: dict[int, QtWidgets.QCheckBox] = {}
        self._bus_vu_widgets: dict[int, LevelMeter] = {}
        buses_section = Section("Bus")
        bus_row = QtWidgets.QHBoxLayout()
        bus_row.setSpacing(8)
        for definition in FIXED_BUSES:
            bid = int(definition.bus_id)
            fader = Fader(-60, int(MAX_GAIN_DB))
            fader.valueChanged.connect(lambda value, bus_id=bid: self._on_bus_gain_changed(bus_id, value))
            meter = self._regie_vu if bid == REGIE_BUS_ID else LevelMeter()
            value = readout()
            strip = ChannelStrip(definition.name, color=definition.color, fader=fader, meter=meter, readout=value, width=100)
            strip.reserve_top(1)
            if bid == REGIE_BUS_ID:
                strip.set_caption("bus principal")
            else:
                feed = chip(
                    f"→ {BUS_BY_ID[REGIE_BUS_ID].name.upper()}",
                    f"Renvoyer aussi ce bus dans {BUS_BY_ID[REGIE_BUS_ID].name}",
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
        return_section = Section("Return")
        self._return_strip = ChannelStrip("Programme", meter=self._return_vu, width=100, color=TOKENS["muted"])
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
        add_hint = QtWidgets.QLabel("Envoie un bus vers une carte son : retour plateau, vMix, enregistreur…")
        add_hint.setProperty("role", "stripCaption")
        add_hint.setWordWrap(True)
        add_lay.addWidget(add_title)
        add_lay.addWidget(add_hint)
        add_lay.addWidget(make_field("PÉRIPHÉRIQUE", self._add_output_device))
        add_lay.addWidget(make_field("BUS", self._bus_selector))
        add_lay.addStretch(1)
        add_lay.addWidget(self._add_output_btn)

        self._output_rack = StripRack("")
        self._output_rack.add_trailing(add_card)
        outputs_section = Section("Sorties")
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
        self._top_bar.right.addWidget(self._server_state_banner)
        self._top_bar.right.addWidget(self._start_btn)
        self._top_bar.right.addWidget(self._stop_btn)
        self._top_bar.right.addWidget(self._info_btn)
        self._server_name.textChanged.connect(self._refresh_identity_header)
        self._discovery_cb.stateChanged.connect(self._refresh_identity_header)
        self._refresh_identity_header()

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._top_bar)
        layout.addWidget(self._pages, 1)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(self._on_show_all_devices_changed)
        self._start_btn.clicked.connect(self._start_server)
        self._stop_btn.clicked.connect(self._stop_server)
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

        self._device_thread: QtCore.QThread | None = None
        self._device_worker: DeviceWorker | None = None
        self._device_timeout: QtCore.QTimer | None = None
        self._last_device_error: str = ""
        self._last_device_count: int = 0
        self._device_refresh_timed_out_flag: bool = False
        self._device_cursor_active: bool = False
        self._close_after_device_refresh: bool = False
        self._is_closing: bool = False
        self._preset_load_problem: tuple[JsonFileStatus, str] | None = None
        self._preset_problem_notified: str | None = None
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
        with best_effort("set window icon"):
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
            with best_effort("set Windows AppUserModelID"):
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("py-intercom.server")

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
        self._start_btn.setVisible(not running)
        self._stop_btn.setVisible(running)

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
        with best_effort("QtWidgets.QApplication.restoreOverrideCursor()"):
            QtWidgets.QApplication.restoreOverrideCursor()

    def _cleanup_device_refresh(self) -> None:
        if self._device_thread is None and self._device_worker is None:
            return

        if self._device_timeout is not None:
            with best_effort("_device_timeout.stop()"):
                self._device_timeout.stop()

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

    def _read_server_preset(self) -> dict | None:
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

        uuid_to_id: dict[str, int] = {}
        for u in clients_raw.keys():
            try:
                uuid_to_id[str(u)] = int(client_id_from_uuid(str(u)))
            except (TypeError, ValueError):
                continue

        feed_by_id = {
            definition.bus_id: definition.default_feed_to_regie
            for definition in FIXED_BUSES
        }
        gain_by_id = {definition.bus_id: 0.0 for definition in FIXED_BUSES}
        for bus_id_str, b in buses_raw.items():
            try:
                bid = int(bus_id_str)
            except (TypeError, ValueError):
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

        snap: dict[int, dict] = {}
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

        outs: dict[int, dict] = {}
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
        self._refresh_bus_strips(buses)
        self._refresh_output_strips(outs, buses)
        self._refresh_client_strips(snap, buses)
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

    def _reopen_outputs_after_start(self) -> None:
        if self._server is None:
            return
        with best_effort("_server.reopen_outputs()"):
            self._server.reopen_outputs(force=True)

    def _reopen_return_after_start(self) -> None:
        if self._server is None:
            return
        with best_effort("_server.reopen_return_input()"):
            self._server.reopen_return_input()

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
            gain_value = _db_value(b.get("gain_db", 0.0))
            slider = self._bus_gain_widgets[bid]
            slider.blockSignals(True)
            try:
                slider.setEnabled(running)
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
            label.setText(format_db(value))
        if self._server is None:
            return
        self._server.set_bus_gain(int(bus_id), float(value))

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
        strip.header.addWidget(indicator)
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
        ):
            cache.clear()

    def _refresh_output_strips(self, outs: dict[int, dict], buses: dict[int, dict]) -> None:
        output_ids = sorted(outs.keys())
        bus_names = {
            definition.bus_id: str(
                buses.get(definition.bus_id, {}).get("name") or definition.name
            )
            for definition in FIXED_BUSES
            if definition.bus_id in buses
        }
        if output_ids != self._output_row_ids:
            self._clear_output_strips()
            for oid in output_ids:
                self._output_rack.add_strip(self._build_output_strip(int(oid), bus_names))
            self._output_row_ids = list(output_ids)

        running = self._server is not None
        for oid in output_ids:
            st = outs.get(int(oid), {})

            dev_cb = self._output_device_widgets[int(oid)]
            dev_cb.blockSignals(True)
            try:
                cur_dev = st.get("device")
                if cur_dev is not None:
                    i = dev_cb.findData(int(cur_dev))
                    if i >= 0:
                        dev_cb.setCurrentIndex(i)
                dev_cb.setEnabled(running)
                dev_cb.setToolTip(dev_cb.currentText())
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

            with best_effort("output vu"):
                self._output_vu_widgets[int(oid)].set_level(float(st.get("vu_dbfs") or -60.0))
            indicator = self._output_status_widgets[int(oid)]
            label = f"Sortie {int(oid) + 1}"
            error = str(st.get("stream_error") or st.get("last_error") or "")
            if error:
                indicator.set_state("error", description=f"{label} en erreur : {error}")
            elif st.get("stream_active") is True:
                indicator.set_state("online", description=f"{label} active")
            elif "stream_active" in st:
                indicator.set_state("error", description=f"{label} inactive")
            else:
                indicator.set_state("unknown", description=f"Activité de la {label.lower()} non confirmée")

    def _on_client_input_gain_changed(self, client_id: int, value: int) -> None:
        label = self._input_gain_labels.get(int(client_id))
        if label is not None:
            label.setText(format_db(value))
        if self._server is None:
            return
        self._server.set_client_input_gain(int(client_id), float(value))

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
            QtWidgets.QMessageBox.warning(self, "Output", str(e))

    def _on_remove_output(self, output_id: int) -> None:
        if self._server is None:
            return
        self._server.remove_output(int(output_id))

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
            except Exception as exc:
                logger.debug("output {} device list refresh failed: {}", oid, exc)
            finally:
                with best_effort("dev_cb.blockSignals()"):
                    dev_cb.blockSignals(False)

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
        self._refresh_return_caption()

        if self._server is None:
            self._output_row_ids = []
            self._load_preset_preview()

    def _refresh_return_caption(self) -> None:
        dev = self._return_input_device.currentData()
        self._return_strip.set_caption(self._return_input_device.currentText() if dev is not None else "aucune entrée")

    def _on_return_enabled_changed(self, state: int) -> None:
        if self._server is None:
            return
        self._server.set_return_enabled(is_checked(state))

    def _on_show_all_devices_changed(self, state: int) -> None:
        self._settings.setValue("show_all_devices", bool(is_checked(state)))
        self._start_device_refresh()

    def _on_return_input_device_changed(self, _idx: int) -> None:
        dev = self._return_input_device.currentData()
        self._refresh_return_caption()
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
        with best_effort("atomic_write_json()"):
            atomic_write_json(server_preset, data)

    def _forget_selected_client(self) -> None:
        if self._selected_client_id is None:
            return
        client_id = int(self._selected_client_id)

        if self._server is not None:
            self._server.forget_client(int(client_id))
            return

        client_uuid = str(self._client_uuid_by_id.get(int(client_id), "") or "")
        if not client_uuid:
            return

        server_preset = self._preset_paths()
        with best_effort("_read_server_preset()"):
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

        self._load_preset_preview()

    def _start_server(self) -> None:
        if self._server is not None:
            return
        if self._device_thread is not None:
            self._device_status.setText("Wait for device enumeration before starting the server.")
            return

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
            self._refresh_return_caption()
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
        self._refresh_identity_header()

        with best_effort("_server.get_clients_snapshot()"):
            snap = self._server.get_clients_snapshot()
            buses = self._server.get_buses_snapshot()
            outs = self._server.get_outputs_snapshot()
            self._refresh_bus_selectors(buses)
            self._refresh_bus_strips(buses)
            self._refresh_client_strips(snap, buses)
            self._refresh_output_strips(outs, buses)
            self._update_server_health(
                self._server.get_stats_snapshot(),
                snap,
                outs,
            )

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
        self._refresh_client_strips(snap, buses)
        self._refresh_output_strips(outs, buses)

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
        bus_levels: dict[int, float] = {}
        for output_id, vu in self._output_vu_widgets.items():
            output = outputs.get(int(output_id), {})
            try:
                level = float(output.get("vu_dbfs", -60.0))
            except Exception:
                level = -60.0
            vu.set_level(level)
            with best_effort("aggregate bus level"):
                bid = int(output.get("bus_id"))
                bus_levels[bid] = max(level, bus_levels.get(bid, -60.0))
        # Régie has its own mix meter; other buses show their loudest output.
        for bid, meter in self._bus_vu_widgets.items():
            if bid != REGIE_BUS_ID:
                meter.set_level(bus_levels.get(int(bid), -60.0))
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
        strip.reserve_top(1)
        strip.top.addWidget(lamps_row)
        strip.setToolTip("Cliquer pour sélectionner ce poste")
        strip.clicked.connect(lambda cid=int(client_id): self._select_client(cid))

        self._client_strips[int(client_id)] = strip
        self._input_gain_widgets[int(client_id)] = fader
        self._input_gain_labels[int(client_id)] = value
        self._vu_widgets[int(client_id)] = meter
        self._status_widgets[int(client_id)] = indicator
        self._talk_lamps[int(client_id)] = lamps
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
        ):
            cache.clear()

    def _select_client(self, client_id: int | None) -> None:
        self._selected_client_id = client_id
        for cid, strip in self._client_strips.items():
            strip.set_flag("selected", cid == client_id)
        self._forget_client_btn.setEnabled(client_id is not None)

    def _refresh_client_strips(self, snap: dict[int, dict], buses: dict[int, dict]) -> None:
        client_ids = sorted(snap.keys())
        if client_ids != self._client_row_ids:
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
            elif transport_state is ClientTransportState.WAITING_UDP:
                indicator.set_state("connecting", description=f"{client_name} : association audio UDP en attente")
            elif transport_state is ClientTransportState.IDLE:
                indicator.set_state("idle", description=f"{client_name} : en ligne, au repos")
            elif transport_state is ClientTransportState.DEGRADED:
                indicator.set_state("degraded", description=f"{client_name} : en parole, audio UDP absent ou ancien")
            else:
                indicator.set_state("online", description=f"{client_name} : en parole, audio UDP actif")
            disconnected = transport_state is ClientTransportState.OFFLINE
            online += 0 if disconnected else 1
            strip.set_flag("offline", disconnected)

            gain_value = _db_value(st.get("input_gain_db", 0.0))
            slider = self._input_gain_widgets[int(client_id)]
            slider.blockSignals(True)
            try:
                slider.setEnabled(self._server is not None)
                slider.setValue(gain_value)
            finally:
                slider.blockSignals(False)
            self._input_gain_labels[int(client_id)].setText(format_db(gain_value))
        self._update_talk_lamps(snap)

        total = len(client_ids)
        self._team_section.set_count(f"{online} en ligne sur {total}" if total else "")

    def _update_talk_lamps(self, snap: Mapping[int, Mapping[str, object]]) -> None:
        for client_id, lamps in self._talk_lamps.items():
            st = snap.get(int(client_id), {})
            ptt = st.get("ptt_buses") if isinstance(st.get("ptt_buses"), dict) else {}
            online = bool(st.get("control_connected"))
            for bus_id, lamp in lamps.items():
                lamp.set_on(online and bool(ptt.get(bus_id) or ptt.get(str(bus_id))))

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
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    win = ServerWindow()
    if minimized or bool(win._start_minimized_cb.isChecked()):
        win.showMinimized()
    else:
        win.show()
    return int(app.exec())
