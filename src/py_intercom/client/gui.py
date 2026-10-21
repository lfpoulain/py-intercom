from __future__ import annotations

import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .. import __version__
from ..common.buses import BUS_BY_ID, FIXED_BUSES, REGIE_BUS_ID
from ..common.constants import AUDIO_UDP_PORT, CONTROL_PORT_OFFSET, MAX_GAIN_DB
from ..common.devices import format_device_label, resolve_device
from ..common.discovery import DiscoveryListener
from ..common.gui_utils import DeviceWorker, is_checked
from ..common.identity import client_id_from_uuid
from ..common.jsonio import JsonFileStatus, atomic_write_json, read_json_file_result
from ..common.logging import best_effort
from ..common.persistence import DebouncedJsonWriter
from ..common.preset_schema import (
    PRESET_SCHEMA_VERSION,
    PresetSchemaError,
    migrate_preset,
    stamp_preset,
)
from ..common.theme import (
    VuMeter,
    apply_theme,
    make_field,
    patch_combo,
    set_status_label,
)
from .client import ClientConfig, IntercomClient
from .hotkeys import (
    GlobalPttHotkeys,
)
from .preflight import PreflightDialog
from .status import (
    ClientOperationalState,
    ClientOperationalStatus,
    derive_client_operational_status,
)

CLIENT_STATE_REFRESH_INTERVAL_MS = 250
CLIENT_VU_REFRESH_INTERVAL_MS = 67


def new_client_preset(client_uuid: str) -> dict:
    """Return a reset-ready client preset using the current schema."""

    return stamp_preset(
        {
            "client_uuid": str(client_uuid),
            "ptt_bus_keys": {},
            "listen_regie": True,
            "listen_return_bus": False,
            "duck_return": False,
            "input_gain_db": 0.0,
            "output_gain_db": 0.0,
            "show_all_devices": False,
        },
        preset_type="client",
    )


def _preset_requires_all_devices(preset: Mapping[str, object]) -> bool:
    """Keep a saved non-WASAPI endpoint visible on the next launch."""

    for key in ("input_device_hostapi", "output_device_hostapi"):
        hostapi = str(preset.get(key) or "").strip()
        if hostapi and "WASAPI" not in hostapi.upper():
            return True
    return False


class _ShortcutKeySequenceEdit(QtWidgets.QKeySequenceEdit):
    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        with best_effort("handle shortcut editor key"):
            if event.key() == int(QtCore.Qt.Key.Key_Escape):
                self.clearFocus()
                return
            if event.key() in (int(QtCore.Qt.Key.Key_Delete), int(QtCore.Qt.Key.Key_Backspace)):
                self.setKeySequence(QtGui.QKeySequence(""))
                self.clearFocus()
                return
        super().keyPressEvent(event)


class ClientWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Py-Intercom Client {__version__}")
        self.setMinimumSize(860, 680)
        self.resize(1060, 800)
        self._set_app_icon()

        self._client: IntercomClient | None = None
        self._connected: bool = False
        self._ever_online: bool = False
        self._operational_status = ClientOperationalStatus(
            ClientOperationalState.DISCONNECTED,
            "Déconnecté",
            "Aucune session intercom demandée.",
        )

        # Status bar
        self._status_bar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self._status_bar)
        self._status_label = QtWidgets.QLabel()
        self._status_bar.addWidget(self._status_label, 1)
        set_status_label(
            self._status_label,
            self._operational_status.label,
            "offline",
            detail=self._operational_status.detail,
        )
        self._connection_state_banner = QtWidgets.QLabel()
        self._connection_state_banner.setProperty("role", "statusBanner")
        self._connection_state_banner.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter
        )
        set_status_label(
            self._connection_state_banner,
            self._operational_status.label,
            "offline",
            detail=self._operational_status.detail,
        )

        self._preset_write_blocked = False
        self._preset_load_problem: tuple[JsonFileStatus, str] | None = None
        self._preset_writer = DebouncedJsonWriter(
            self._preset_path(),
            delay_s=0.35,
            write_func=lambda path, data: atomic_write_json(path, data),
            on_error=lambda exc: logger.error("client preset write failed: {}", exc),
            thread_name="client-preset-writer",
        )
        self._preset: dict = {}
        self._preset = self._load_preset()

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        self._discovered_servers = QtWidgets.QComboBox()
        patch_combo(self._discovered_servers)
        self._discovered_servers.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._discovered_servers.setMinimumContentsLength(30)
        self._discovered_servers.addItem("(manual)", None)
        self._discovered_servers.currentIndexChanged.connect(self._on_discovered_server_selected)

        self._discovery_listener: DiscoveryListener | None = None
        self._discovery_pending_update: bool = False
        self._discovery_signal = QtCore.QTimer(self)
        self._discovery_signal.setInterval(100)
        self._discovery_signal.timeout.connect(self._poll_discovery_updates)
        self._start_discovery_listener()

        self._server_ip = QtWidgets.QLineEdit(str(self._preset_get("server_ip", "")))

        client_uuid = str(self._preset_get("client_uuid", ""))
        if not client_uuid:
            client_uuid = str(uuid.uuid4())
            self._preset_set("client_uuid", client_uuid)
        stable_id = int(client_id_from_uuid(client_uuid))
        self._client_id_label = QtWidgets.QLabel("Client ID")
        self._client_id = QtWidgets.QLineEdit(str(stable_id))
        self._client_id.setReadOnly(True)
        self._client_id_label.setVisible(False)
        self._client_id.setVisible(False)

        self._name = QtWidgets.QLineEdit(str(self._preset_get("name", "")))
        self._ptt_bus_keys: dict[int, QtWidgets.QKeySequenceEdit] = {}
        self._ptt_bus_clear: dict[int, QtWidgets.QToolButton] = {}
        self._ptt_bus_mode_widgets: dict[int, QtWidgets.QComboBox] = {}
        self._ptt_bus_modes: dict[int, str] = {}
        self._ptt_bus_toggle_state: dict[int, bool] = {}
        self._ptt_bus_state_labels: dict[int, QtWidgets.QLabel] = {}
        self._ptt_bus_vu: dict[int, VuMeter] = {}
        self._shortcut_widgets: list[QtWidgets.QWidget] = []
        self._known_buses: dict[int, dict] = {
            definition.bus_id: {
                "bus_id": definition.bus_id,
                "name": definition.name,
                "feed_to_regie": definition.default_feed_to_regie,
            }
            for definition in FIXED_BUSES
        }
        self._bus_rows_widget = QtWidgets.QWidget()
        self._bus_rows_layout = QtWidgets.QGridLayout(self._bus_rows_widget)
        self._bus_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._bus_rows_layout.setHorizontalSpacing(8)
        self._bus_rows_layout.setVerticalSpacing(8)
        self._bus_rows_signature: tuple[int, ...] = tuple()

        self._sync_bus_widgets(self._known_buses, apply_saved=True)

        self._input_device = QtWidgets.QComboBox()
        patch_combo(self._input_device)
        self._output_device = QtWidgets.QComboBox()
        patch_combo(self._output_device)
        self._input_device.currentIndexChanged.connect(self._on_input_device_changed)
        self._output_device.currentIndexChanged.connect(self._on_output_device_changed)
        self._show_all_devices = QtWidgets.QCheckBox("Afficher tous les périphériques")
        saved_show_all = bool(self._preset_get("show_all_devices", False))
        if _preset_requires_all_devices(self._preset):
            saved_show_all = True
        self._show_all_devices.setChecked(bool(saved_show_all))
        # Persist the automatic upgrade as well: a DirectSound/MME Bluetooth
        # endpoint must not disappear again at the following startup.
        self._preset_set("show_all_devices", bool(saved_show_all))
        self._refresh_devices_btn = QtWidgets.QPushButton("Actualiser")
        self._refresh_devices_btn.setProperty("class", "secondary")
        self._refresh_devices_btn.setToolTip(
            "Réinterroger les périphériques audio. Disponible uniquement hors connexion."
        )
        self._preflight_btn = QtWidgets.QPushButton("Tester…")
        self._preflight_btn.setProperty("class", "secondary")
        self._preflight_btn.setToolTip(
            "Vérifier réseau, raccourcis, micro et casque avant de se connecter."
        )
        self._preflight_btn.setEnabled(False)
        self._device_status = QtWidgets.QLabel("")

        self._connect_btn = QtWidgets.QPushButton("▶  Connecter")
        self._connect_btn.setProperty("class", "success")
        self._connect_btn.setFixedWidth(130)
        self._connect_btn.setEnabled(False)
        self._connect_btn.setToolTip("Démarrer les flux audio puis établir la session intercom")
        self._disconnect_btn = QtWidgets.QPushButton("■  Déconnecter")
        self._disconnect_btn.setProperty("class", "danger")
        self._disconnect_btn.setFixedWidth(130)
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setToolTip("Désarmer le PTT et quitter la session intercom")

        self._start_minimized = QtWidgets.QCheckBox("Démarrer réduit")
        with best_effort("_start_minimized.setChecked()"):
            self._start_minimized.setChecked(bool(self._preset_get("start_minimized", False)))
        self._start_minimized.stateChanged.connect(self._on_start_minimized_changed)

        self._autoconnect_cb = QtWidgets.QCheckBox("Connexion automatique")
        self._autoconnect_cb.setToolTip("Se connecter automatiquement au démarrage")
        with best_effort("_autoconnect_cb.setChecked()"):
            self._autoconnect_cb.setChecked(bool(self._preset_get("autoconnect", False)))
        self._autoconnect_cb.stateChanged.connect(self._on_autoconnect_changed)

        self._listen_regie = QtWidgets.QCheckBox(
            f"Écouter {BUS_BY_ID[REGIE_BUS_ID].name}"
        )
        self._listen_regie.setEnabled(False)
        with best_effort("_listen_regie.setChecked()"):
            self._listen_regie.setChecked(bool(self._preset_get("listen_regie", True)))

        self._listen_return_bus = QtWidgets.QCheckBox("Écouter le bus Return")
        self._listen_return_bus.setEnabled(False)
        with best_effort("_listen_return_bus.setChecked()"):
            self._listen_return_bus.setChecked(bool(self._preset_get("listen_return_bus", False)))

        self._duck_return = QtWidgets.QCheckBox("Baisser le Return quand on me parle")
        self._duck_return.setToolTip(
            "Le programme baisse automatiquement de 15 dB dans votre casque "
            "pendant que quelqu'un vous parle, puis remonte."
        )
        with best_effort("restore duck_return"):
            self._duck_return.setChecked(bool(self._preset_get("duck_return", False)))

        self._mic_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._mic_gain.setMinimum(-60)
        self._mic_gain.setMaximum(int(MAX_GAIN_DB))
        self._mic_gain.setValue(0)
        self._mic_gain.setEnabled(False)
        self._mic_gain_lbl = QtWidgets.QLabel("0 dB")
        self._mic_gain_lbl.setFixedWidth(50)
        self._mic_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._hp_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._hp_gain.setMinimum(-60)
        self._hp_gain.setMaximum(int(MAX_GAIN_DB))
        self._hp_gain.setValue(0)
        self._hp_gain.setEnabled(False)
        self._hp_gain_lbl = QtWidgets.QLabel("0 dB")
        self._hp_gain_lbl.setFixedWidth(50)
        self._hp_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._return_gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self._return_gain.setMinimum(-60)
        self._return_gain.setMaximum(int(MAX_GAIN_DB))
        self._return_gain.setValue(0)
        self._return_gain.setEnabled(False)
        self._return_gain_lbl = QtWidgets.QLabel("0 dB")
        self._return_gain_lbl.setFixedWidth(50)
        self._return_gain_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)

        try:
            mic_gain = int(self._preset_get("input_gain_db", 0))
        except Exception:
            mic_gain = 0
        try:
            hp_gain = int(self._preset_get("output_gain_db", 0))
        except Exception:
            hp_gain = 0
        try:
            return_gain = int(self._preset_get("return_gain_db", 0))
        except Exception:
            return_gain = 0
        self._mic_gain.setValue(int(mic_gain))
        self._mic_gain_lbl.setText(f"{int(mic_gain)} dB")
        self._hp_gain.setValue(int(hp_gain))
        self._hp_gain_lbl.setText(f"{int(hp_gain)} dB")
        self._return_gain.setValue(int(return_gain))
        self._return_gain_lbl.setText(f"{int(return_gain)} dB")

        self._in_vu = VuMeter()
        self._out_vu = VuMeter()
        self._return_vu = VuMeter()

        self._info_btn = QtWidgets.QToolButton()
        self._info_btn.setText("ℹ")
        self._info_btn.setProperty("class", "ghost")
        self._info_btn.setFixedSize(34, 34)
        self._info_btn.setToolTip("Info / Debug")
        self._info_btn.clicked.connect(self._show_geek_info)

        # -- Operational header --
        hero = QtWidgets.QFrame()
        hero.setProperty("role", "hero")
        hero_lay = QtWidgets.QHBoxLayout(hero)
        hero_lay.setContentsMargins(18, 14, 14, 14)
        hero_lay.setSpacing(16)
        hero_copy = QtWidgets.QVBoxLayout()
        hero_copy.setSpacing(1)
        eyebrow = QtWidgets.QLabel("PY-INTERCOM")
        eyebrow.setProperty("role", "heroEyebrow")
        title = QtWidgets.QLabel("Poste client")
        title.setProperty("role", "heroTitle")
        subtitle = QtWidgets.QLabel(
            "Préparez votre audio, connectez-vous, puis utilisez les touches PTT."
        )
        subtitle.setProperty("role", "heroSubtitle")
        hero_copy.addWidget(eyebrow)
        hero_copy.addWidget(title)
        hero_copy.addWidget(subtitle)
        hero_lay.addLayout(hero_copy, 1)
        self._connection_state_banner.setMinimumWidth(150)
        hero_lay.addWidget(self._connection_state_banner)
        hero_lay.addWidget(self._info_btn)

        # -- Connection preparation --
        conn_box = QtWidgets.QGroupBox("Connexion")
        conn_box.setObjectName("connectionCard")
        conn_box_lay = QtWidgets.QVBoxLayout(conn_box)
        conn_box_lay.setContentsMargins(16, 12, 16, 14)
        conn_box_lay.setSpacing(11)
        conn_fields = QtWidgets.QHBoxLayout()
        conn_fields.setSpacing(12)
        conn_fields.addWidget(
            make_field("SERVEUR DÉTECTÉ", self._discovered_servers, minimum_width=220),
            2,
        )
        conn_fields.addWidget(
            make_field("ADRESSE SERVEUR", self._server_ip, minimum_width=180),
            1,
        )
        conn_fields.addWidget(
            make_field("VOTRE NOM", self._name, minimum_width=160),
            1,
        )
        conn_fields.addWidget(self._client_id_label)
        conn_fields.addWidget(self._client_id)
        conn_box_lay.addLayout(conn_fields)

        conn_footer = QtWidgets.QHBoxLayout()
        conn_footer.setSpacing(12)
        conn_footer.addWidget(self._autoconnect_cb)
        conn_footer.addWidget(self._start_minimized)
        conn_footer.addStretch(1)
        conn_footer.addWidget(self._disconnect_btn)
        conn_footer.addWidget(self._connect_btn)
        conn_box_lay.addLayout(conn_footer)

        # -- Audio devices --
        dev_box = QtWidgets.QGroupBox("Périphériques audio")
        dev_box.setObjectName("devicesCard")
        dev_lay = QtWidgets.QVBoxLayout(dev_box)
        dev_lay.setContentsMargins(16, 12, 16, 14)
        dev_lay.setSpacing(10)
        dev_fields = QtWidgets.QHBoxLayout()
        dev_fields.setSpacing(12)
        dev_fields.addWidget(make_field("MICROPHONE", self._input_device), 1)
        dev_fields.addWidget(make_field("CASQUE / SORTIE", self._output_device), 1)
        dev_lay.addLayout(dev_fields)
        dev_footer = QtWidgets.QHBoxLayout()
        dev_footer.setSpacing(10)
        dev_footer.addWidget(self._show_all_devices)
        dev_footer.addWidget(self._refresh_devices_btn)
        dev_footer.addWidget(self._preflight_btn)
        self._device_status.setProperty("role", "muted")
        dev_footer.addWidget(self._device_status, 1)
        dev_lay.addLayout(dev_footer)

        # -- Monitoring and audio controls --
        ctrl_box = QtWidgets.QGroupBox("Écoute && niveaux")
        ctrl_box.setObjectName("monitoringCard")
        ctrl_lay = QtWidgets.QGridLayout(ctrl_box)
        ctrl_lay.setContentsMargins(16, 12, 16, 14)
        ctrl_lay.setVerticalSpacing(11)
        ctrl_lay.setHorizontalSpacing(10)
        ctrl_lay.addWidget(self._listen_regie, 0, 0, 1, 2)
        ctrl_lay.addWidget(self._listen_return_bus, 0, 2, 1, 2)
        gain_labels = ("Micro", "Casque", "Return")
        gains = (self._mic_gain, self._hp_gain, self._return_gain)
        gain_values = (self._mic_gain_lbl, self._hp_gain_lbl, self._return_gain_lbl)
        meters = (self._in_vu, self._out_vu, self._return_vu)
        for row, (label_text, gain, value, meter) in enumerate(
            zip(gain_labels, gains, gain_values, meters, strict=False),
            start=1,
        ):
            label = QtWidgets.QLabel(label_text)
            label.setProperty("role", "fieldLabel")
            ctrl_lay.addWidget(label, row, 0)
            ctrl_lay.addWidget(gain, row, 1, 1, 2)
            ctrl_lay.addWidget(value, row, 3)
            ctrl_lay.addWidget(meter, row, 4)
        ctrl_lay.addWidget(self._duck_return, 4, 0, 1, 5)
        ctrl_lay.setColumnStretch(1, 2)
        ctrl_lay.setColumnStretch(4, 1)
        ctrl_lay.setRowStretch(5, 1)

        # -- PTT / routing --
        ptt_box = QtWidgets.QGroupBox("Parler — PTT")
        ptt_box.setObjectName("pttCard")
        ptt_lay = QtWidgets.QVBoxLayout(ptt_box)
        ptt_lay.setContentsMargins(16, 12, 16, 14)
        ptt_lay.setSpacing(8)
        ptt_hint = QtWidgets.QLabel(
            "Choisissez le mode et le raccourci de chaque destination."
        )
        ptt_hint.setProperty("role", "muted")
        ptt_lay.addWidget(ptt_hint)
        ptt_lay.addWidget(self._bus_rows_widget, 1)

        live_row = QtWidgets.QHBoxLayout()
        live_row.setSpacing(12)
        live_row.addWidget(ctrl_box, 4)
        live_row.addWidget(ptt_box, 6)

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(hero)
        layout.addWidget(conn_box)
        layout.addWidget(dev_box)
        layout.addLayout(live_row, 1)

        self._refresh_devices_btn.clicked.connect(self._start_device_refresh)
        self._show_all_devices.stateChanged.connect(self._on_show_all_devices_changed)
        self._connect_btn.clicked.connect(self._connect)
        self._disconnect_btn.clicked.connect(self._disconnect)

        self._listen_regie.stateChanged.connect(self._on_listen_regie_changed)
        self._listen_return_bus.stateChanged.connect(self._on_listen_return_bus_changed)
        self._preflight_btn.clicked.connect(self._open_preflight)
        self._duck_return.stateChanged.connect(self._on_duck_return_changed)
        self._mic_gain.valueChanged.connect(self._on_mic_gain_changed)
        self._hp_gain.valueChanged.connect(self._on_hp_gain_changed)
        self._return_gain.valueChanged.connect(self._on_return_gain_changed)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(CLIENT_STATE_REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_stats)

        # VU meters need a fluid refresh, while state/config reconciliation is
        # intentionally slower to keep the GUI thread predictable.
        self._vu_timer = QtCore.QTimer(self)
        self._vu_timer.setInterval(CLIENT_VU_REFRESH_INTERVAL_MS)
        self._vu_timer.timeout.connect(self._refresh_vu_meters)

        self._device_thread: QtCore.QThread | None = None
        self._device_worker: DeviceWorker | None = None
        self._device_timeout: QtCore.QTimer | None = None
        self._last_device_error: str = ""
        self._last_device_count: int = 0
        self._device_refresh_timed_out_flag: bool = False
        self._device_cursor_active: bool = False
        self._close_after_device_refresh: bool = False
        self._is_closing: bool = False
        self._device_cache: dict[int, object] = {}
        self._kick_notified: bool = False
        self._ptt_start_failed: bool = False

        self._global_ptt: GlobalPttHotkeys | None = None

        QtCore.QTimer.singleShot(0, self._start_device_refresh)
        if self._preset_load_problem is not None:
            QtCore.QTimer.singleShot(0, self._show_preset_load_problem)

    def _make_shortcut_widget(self, edit: QtWidgets.QKeySequenceEdit, clear_btn: QtWidgets.QToolButton) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(edit, 1)
        lay.addWidget(clear_btn, 0)
        self._shortcut_widgets.append(w)
        return w

    def _clear_layout(self, layout: QtWidgets.QLayout) -> None:
        while layout.count() > 0:
            item = layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()
                continue
            child = item.layout()
            if child is not None:
                self._clear_layout(child)

    def _disarm_ptt_for_safety(self) -> None:
        """Make every route fail-closed before displaying a non-online state."""

        hotkeys = self._global_ptt
        self._global_ptt = None
        if hotkeys is not None:
            try:
                hotkeys.stop()
            except Exception:
                logger.exception("failed to stop global PTT hotkeys")

        self._ptt_bus_toggle_state = {
            int(bus_id): False for bus_id in self._ptt_bus_toggle_state
        }
        client = self._client
        if client is not None:
            for bus_id in list(self._ptt_bus_keys):
                try:
                    client.set_ptt_bus(int(bus_id), False)
                except Exception:
                    logger.exception("failed to disarm PTT bus {}", int(bus_id))

    def _set_operational_status(self, status: ClientOperationalStatus) -> None:
        if (
            status.state is ClientOperationalState.ONLINE
            and self._ptt_start_failed
        ):
            status = ClientOperationalStatus(
                ClientOperationalState.DEGRADED,
                "Dégradé",
                "Raccourcis PTT indisponibles. Reconnectez le client après correction.",
            )
        previous = self._operational_status.state
        self._operational_status = status

        address = ""
        if (
            self._client is not None
            and status.state is not ClientOperationalState.DISCONNECTED
        ):
            address = (
                f" — {self._client.config.server_ip}:"
                f"{self._client.config.server_port}"
            )
        semantic_state = {
            ClientOperationalState.DISCONNECTED: "offline",
            ClientOperationalState.CONNECTING: "connecting",
            ClientOperationalState.ONLINE: "online",
            ClientOperationalState.DEGRADED: "degraded",
            ClientOperationalState.RECONNECTING: "degraded",
        }[status.state]
        set_status_label(
            self._status_label,
            f"{status.label}{address}",
            semantic_state,
            detail=status.detail,
        )
        set_status_label(
            self._connection_state_banner,
            status.label,
            semantic_state,
            detail=status.detail,
        )

        if (
            status.state is not ClientOperationalState.ONLINE
            and (
                previous is ClientOperationalState.ONLINE
                or self._global_ptt is not None
            )
        ):
            self._disarm_ptt_for_safety()
        elif previous is not ClientOperationalState.ONLINE:
            self._ever_online = True
            if self._global_ptt is None and not self._ptt_start_failed:
                try:
                    self._start_global_ptt()
                except Exception as exc:
                    self._ptt_start_failed = True
                    logger.exception("global PTT hotkeys failed to start")
                    self._operational_status = ClientOperationalStatus(
                        ClientOperationalState.DEGRADED,
                        "Dégradé",
                        f"Raccourcis PTT indisponibles : {exc}",
                    )
                    self._disarm_ptt_for_safety()
                    set_status_label(
                        self._status_label,
                        f"Dégradé{address}",
                        "degraded",
                        detail=f"Raccourcis PTT indisponibles : {exc}",
                    )
                    set_status_label(
                        self._connection_state_banner,
                        "Dégradé",
                        "degraded",
                        detail=f"Raccourcis PTT indisponibles : {exc}",
                    )
                    return
            self._apply_ptt_modes_to_client()

        if previous is not status.state:
            self._refresh_state_labels()

    def _apply_ptt_modes_to_client(self) -> None:
        cli = self._client
        if cli is None:
            return

        active_buses = self._global_ptt.active_buses() if self._global_ptt is not None else {}
        for bid in list(self._ptt_bus_keys.keys()):
            if self._operational_status.state is not ClientOperationalState.ONLINE:
                active = False
            else:
                mode = str(self._ptt_bus_modes.get(int(bid), "ptt"))
                if mode == "always_on":
                    active = True
                elif mode == "toggle":
                    active = bool(self._ptt_bus_toggle_state.get(int(bid), False))
                else:
                    active = bool(active_buses.get(int(bid), False))
            with best_effort("cli.set_ptt_bus()"):
                cli.set_ptt_bus(int(bid), bool(active))

    def _update_toggle_state_label(self, lbl: QtWidgets.QLabel, bus_id: int, mode: str) -> None:
        online = self._operational_status.state is ClientOperationalState.ONLINE
        if not online:
            lbl.setText("DÉSARMÉ")
            lbl.setStyleSheet("color: #aab4c3; font-weight: 600;")
            lbl.setToolTip("PTT désarmé tant que la chaîne audio n'est pas en ligne")
        elif mode == "always_on":
            lbl.setText("ACTIF")
            lbl.setStyleSheet("color: #4caf50; font-weight: bold;")
            lbl.setToolTip("Transmission permanente active")
        elif mode == "toggle":
            on = bool(self._ptt_bus_toggle_state.get(int(bus_id), False))
            lbl.setText("ACTIF" if on else "INACTIF")
            lbl.setStyleSheet("color: #4caf50; font-weight: bold;" if on else "color: #f44336; font-weight: bold;")
            lbl.setToolTip("Transmission activée" if on else "Transmission désactivée")
        elif mode == "ptt":
            gp = getattr(self, "_global_ptt", None)
            active_buses = gp.active_buses() if gp is not None else {}
            on = bool(active_buses.get(int(bus_id), False))
            lbl.setText("ACTIF" if on else "PRÊT")
            lbl.setStyleSheet("color: #4caf50; font-weight: bold;" if on else "color: #aab4c3; font-weight: 600;")
            lbl.setToolTip("Transmission en cours" if on else "Maintenez le raccourci pour parler")
        else:
            lbl.setText("DÉSARMÉ")
            lbl.setStyleSheet("color: #aab4c3; font-weight: 600;")
        lbl.setAccessibleDescription(lbl.toolTip() or lbl.text())

    def _refresh_state_labels(self) -> None:
        for bid, lbl in self._ptt_bus_state_labels.items():
            mode = str(self._ptt_bus_modes.get(int(bid), "ptt"))
            self._update_toggle_state_label(lbl, int(bid), mode)

    def _hotkey_bindings_snapshot(self) -> dict[int, tuple[str, str]]:
        bindings: dict[int, tuple[str, str]] = {}
        for bus_id, edit in self._ptt_bus_keys.items():
            try:
                sequence = str(edit.keySequence().toString())
            except Exception:
                sequence = ""
            bindings[int(bus_id)] = (
                str(self._ptt_bus_modes.get(int(bus_id), "ptt")),
                sequence,
            )
        return bindings

    def _start_global_ptt(self) -> None:
        if self._global_ptt is not None:
            return
        hotkeys = GlobalPttHotkeys(
            self._hotkey_bindings_snapshot(),
            dict(self._ptt_bus_toggle_state),
            self,
        )
        hotkeys.ptt_changed.connect(
            self._on_global_ptt_changed,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )
        hotkeys.start()
        self._global_ptt = hotkeys

    @QtCore.Slot(int, bool)
    def _on_global_ptt_changed(self, bus_id: int, active: bool) -> None:
        # Ignore events left in the Qt queue by a listener that has since been
        # replaced or stopped.
        if (
            self.sender() is not self._global_ptt
            or self._operational_status.state is not ClientOperationalState.ONLINE
        ):
            return
        mode = str(self._ptt_bus_modes.get(int(bus_id), "ptt"))
        if mode == "toggle":
            self._ptt_bus_toggle_state[int(bus_id)] = bool(active)
        cli = self._client
        if cli is not None:
            with best_effort("cli.set_ptt_bus()"):
                cli.set_ptt_bus(int(bus_id), bool(active))
        self._refresh_state_labels()

    def _on_ptt_bus_mode_changed(self, bus_id: int) -> None:
        cb = self._ptt_bus_mode_widgets.get(int(bus_id))
        if cb is None:
            return
        mode = str(cb.currentData() or "ptt")
        self._ptt_bus_modes[int(bus_id)] = mode
        if mode != "toggle":
            self._ptt_bus_toggle_state.pop(int(bus_id), None)
        lbl = self._ptt_bus_state_labels.get(int(bus_id))
        if lbl is not None:
            self._update_toggle_state_label(lbl, int(bus_id), mode)
        saved = {str(k): v for k, v in self._ptt_bus_modes.items()}
        self._preset_set("ptt_bus_modes", saved)
        if self._operational_status.state is ClientOperationalState.ONLINE:
            self._apply_ptt_modes_to_client()

    def _sync_bus_widgets(self, buses: dict[int, dict], *, apply_saved: bool = False) -> None:
        parsed: dict[int, dict] = {}
        for bid, b in buses.items():
            try:
                bus_id = int(bid)
            except (TypeError, ValueError):
                continue
            definition = BUS_BY_ID.get(int(bus_id))
            if definition is None:
                continue
            if not isinstance(b, dict):
                b = {}
            parsed[int(bus_id)] = {
                "bus_id": int(bus_id),
                "name": str(b.get("name") or definition.name),
                "feed_to_regie": bool(
                    b.get("feed_to_regie", definition.default_feed_to_regie)
                ),
            }

        for definition in FIXED_BUSES:
            parsed.setdefault(
                definition.bus_id,
                {
                    "bus_id": definition.bus_id,
                    "name": definition.name,
                    "feed_to_regie": definition.default_feed_to_regie,
                },
            )

        new_signature = tuple((int(bid), str(parsed[bid].get("name"))) for bid in sorted(parsed.keys()))
        if new_signature == self._bus_rows_signature and not apply_saved:
            self._known_buses = dict(parsed)
            return

        old_keys: dict[int, str] = {}
        for bid, edit in self._ptt_bus_keys.items():
            try:
                old_keys[int(bid)] = str(edit.keySequence().toString())
            except Exception:
                old_keys[int(bid)] = ""

        old_modes: dict[int, str] = dict(self._ptt_bus_modes)

        if apply_saved:
            with best_effort("restore saved PTT keys"):
                saved_keys = self._preset_get("ptt_bus_keys", {})
                if isinstance(saved_keys, dict):
                    for k, v in saved_keys.items():
                        with best_effort("parse saved PTT key"):
                            old_keys[int(k)] = str(v or "")
            with best_effort("restore saved PTT modes"):
                saved_modes = self._preset_get("ptt_bus_modes", {})
                if isinstance(saved_modes, dict):
                    for k, v in saved_modes.items():
                        with best_effort("parse saved PTT mode"):
                            old_modes[int(k)] = str(v or "ptt")

        self._clear_layout(self._bus_rows_layout)
        self._shortcut_widgets = []
        self._ptt_bus_keys = {}
        self._ptt_bus_clear = {}
        self._ptt_bus_mode_widgets = {}
        self._ptt_bus_modes = {}
        self._ptt_bus_state_labels = {}
        self._ptt_bus_vu = {}

        headers = ["Bus", "Mode", "État PTT", "VU", "Raccourci PTT"]
        for col, title in enumerate(headers):
            lbl = QtWidgets.QLabel(str(title))
            lbl.setProperty("role", "fieldLabel")
            self._bus_rows_layout.addWidget(lbl, 0, int(col))

        row = 1
        for bus_id in sorted(parsed.keys()):
            b = parsed[int(bus_id)]
            bus_name = str(b.get("name") or BUS_BY_ID[int(bus_id)].name)

            lbl = QtWidgets.QLabel(str(bus_name))

            mode_cb = QtWidgets.QComboBox()
            patch_combo(mode_cb)
            mode_cb.addItem("PTT", "ptt")
            mode_cb.addItem("Toujours actif", "always_on")
            mode_cb.addItem("Bascule", "toggle")
            mode_cb.setAccessibleName(f"Mode PTT {bus_name}")
            mode_cb.setToolTip(f"Choisir le mode de transmission vers {bus_name}")
            saved_mode = str(old_modes.get(int(bus_id), "ptt"))
            idx = mode_cb.findData(saved_mode)
            if idx >= 0:
                mode_cb.setCurrentIndex(idx)
            self._ptt_bus_modes[int(bus_id)] = str(mode_cb.currentData() or "ptt")
            mode_cb.currentIndexChanged.connect(lambda _i, bid=int(bus_id): self._on_ptt_bus_mode_changed(bid))
            mode_cb.setEnabled(not bool(self._connected))

            state_lbl = QtWidgets.QLabel("")
            state_lbl.setProperty("role", "statusChip")
            state_lbl.setFixedWidth(76)
            state_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            state_lbl.setAccessibleName(f"État PTT {bus_name}")
            self._update_toggle_state_label(state_lbl, int(bus_id), str(mode_cb.currentData() or "ptt"))

            edit = _ShortcutKeySequenceEdit()
            edit.setAccessibleName(f"Raccourci PTT {bus_name}")
            edit.setToolTip(
                "Cliquez puis saisissez le raccourci. Échap annule, Suppr efface."
            )
            clear_btn = QtWidgets.QToolButton()
            clear_btn.setText("✕")
            clear_btn.setProperty("class", "ghost")
            clear_btn.setFixedSize(28, 28)
            clear_btn.setAccessibleName(f"Effacer le raccourci PTT {bus_name}")
            clear_btn.setToolTip(f"Effacer le raccourci PTT de {bus_name}")
            clear_btn.clicked.connect(lambda _checked=False, e=edit: e.setKeySequence(QtGui.QKeySequence("")))
            key_enabled = not bool(self._connected)
            edit.setEnabled(bool(key_enabled))
            clear_btn.setEnabled(bool(key_enabled))

            seq = str(old_keys.get(int(bus_id), "") or "")
            with best_effort("edit.setKeySequence()"):
                edit.setKeySequence(QtGui.QKeySequence(seq))
            edit.keySequenceChanged.connect(lambda _seq, bid=int(bus_id): self._on_ptt_bus_key_changed(bid))

            vu_meter = VuMeter()
            vu_meter.setMinimumWidth(60)
            vu_meter.set_level(-60.0)

            self._bus_rows_layout.addWidget(lbl, row, 0)
            self._bus_rows_layout.addWidget(mode_cb, row, 1)
            self._bus_rows_layout.addWidget(state_lbl, row, 2)
            self._bus_rows_layout.addWidget(vu_meter, row, 3)
            self._bus_rows_layout.addWidget(self._make_shortcut_widget(edit, clear_btn), row, 4)

            self._ptt_bus_keys[int(bus_id)] = edit
            self._ptt_bus_clear[int(bus_id)] = clear_btn
            self._ptt_bus_mode_widgets[int(bus_id)] = mode_cb
            self._ptt_bus_state_labels[int(bus_id)] = state_lbl
            self._ptt_bus_vu[int(bus_id)] = vu_meter
            row += 1

        self._known_buses = dict(parsed)
        self._bus_rows_signature = tuple((int(bid), str(parsed[bid].get("name"))) for bid in sorted(parsed.keys()))

        if self._operational_status.state is ClientOperationalState.ONLINE:
            with best_effort("_apply_ptt_modes_to_client()"):
                self._apply_ptt_modes_to_client()

    def _start_discovery_listener(self) -> None:
        if self._discovery_listener is not None:
            return
        self._discovery_listener = DiscoveryListener(
            on_update=self._on_discovery_update,
        )
        self._discovery_listener.start()
        self._discovery_signal.start()

    def _stop_discovery_listener(self) -> None:
        self._discovery_signal.stop()
        if self._discovery_listener is not None:
            self._discovery_listener.stop()
            self._discovery_listener = None

    def _on_discovery_update(self, _servers) -> None:
        self._discovery_pending_update = True

    def _poll_discovery_updates(self) -> None:
        if not self._discovery_pending_update:
            return
        self._discovery_pending_update = False
        if self._discovery_listener is None:
            return
        servers = self._discovery_listener.get_servers()
        self._refresh_discovered_combo(servers)

    def _refresh_discovered_combo(self, servers: dict) -> None:
        current_data = self._discovered_servers.currentData()
        self._discovered_servers.blockSignals(True)
        try:
            self._discovered_servers.clear()
            self._discovered_servers.addItem("(manual)", None)
            for key in sorted(servers.keys()):
                srv = servers[key]
                label = f"{srv.server_name}  ({srv.ip}:{srv.audio_port})"
                self._discovered_servers.addItem(label, key)
            if current_data is not None:
                i = self._discovered_servers.findData(current_data)
                if i >= 0:
                    self._discovered_servers.setCurrentIndex(i)
        finally:
            self._discovered_servers.blockSignals(False)

    def _on_discovered_server_selected(self, index: int) -> None:
        key = self._discovered_servers.currentData()
        if key is None or self._discovery_listener is None:
            return
        servers = self._discovery_listener.get_servers()
        srv = servers.get(str(key))
        if srv is None:
            return
        self._server_ip.setText(srv.ip)

    def _reset_total_config(self) -> None:
        if self._connected:
            QtWidgets.QMessageBox.warning(self, "Connected", "Disconnect first to reset config")
            return

        if (
            QtWidgets.QMessageBox.question(
                self,
                "Reset config",
                "Delete client preset file and reset client configuration?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        # Fully stop persistent client (releases audio devices)
        if self._client is not None:
            with best_effort("_client.stop()"):
                self._client.stop()
            self._client = None

        p = self._preset_path()
        with best_effort("_preset_writer.cancel_pending()"):
            self._preset_writer.cancel_pending()
            self._preset_writer.flush(raise_on_error=False)
        with best_effort("p.with_suffix()"):
            tmp = p.with_suffix(p.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
        with best_effort("delete client preset"):
            if p.exists():
                p.unlink()

        client_uuid = str(uuid.uuid4())
        self._preset = new_client_preset(client_uuid)
        self._preset_write_blocked = False
        self._preset_load_problem = None
        stable_id = int(client_id_from_uuid(client_uuid))

        self._preset_save(flush=True)

        self._server_ip.setText("")
        self._name.setText("")
        self._mic_gain.setValue(0)
        self._hp_gain.setValue(0)
        self._mic_gain_lbl.setText("0 dB")
        self._hp_gain_lbl.setText("0 dB")
        self._return_gain.setValue(0)
        self._return_gain_lbl.setText("0 dB")

        with best_effort("clear PTT shortcut editors"):
            for edit in self._ptt_bus_keys.values():
                edit.setKeySequence(QtGui.QKeySequence(""))

        self._client_id.setText(str(stable_id))

        self._input_device.blockSignals(True)
        self._output_device.blockSignals(True)
        self._show_all_devices.blockSignals(True)
        try:
            self._input_device.setCurrentIndex(-1)
            self._output_device.setCurrentIndex(-1)
            self._show_all_devices.setChecked(False)
        finally:
            self._input_device.blockSignals(False)
            self._output_device.blockSignals(False)
            self._show_all_devices.blockSignals(False)

        self._device_status.setText("Config reset")
        self._update_device_selection_state()
        QtCore.QTimer.singleShot(0, self._start_device_refresh)

    def _preset_path(self) -> Path:
        return Path.home() / "py-intercom" / "client_preset.json"

    def _load_preset(self) -> dict:
        p = self._preset_path()
        result = read_json_file_result(p)
        if result.status is JsonFileStatus.MISSING:
            return {}
        if result.status is not JsonFileStatus.OK:
            self._preset_write_blocked = True
            self._preset_load_problem = (result.status, str(result.error or "unknown error"))
            logger.error("client preset cannot be loaded: {} ({})", p, result.error)
            return {}
        try:
            migration = migrate_preset(result.value, preset_type="client")
        except PresetSchemaError as exc:
            self._preset_write_blocked = True
            self._preset_load_problem = (JsonFileStatus.INVALID, str(exc))
            logger.error("client preset schema is incompatible: {} ({})", p, exc)
            return {}
        if migration.migrated:
            logger.info(
                "client preset migrated in memory from schema {} to {}",
                migration.source_version,
                PRESET_SCHEMA_VERSION,
            )
        return migration.data

    def _show_preset_load_problem(self) -> None:
        problem = self._preset_load_problem
        if problem is None or self._is_closing:
            return
        status, detail = problem
        path = self._preset_path()
        backup = path.with_suffix(path.suffix + ".bak")
        backup_state = "présente" if backup.exists() else "absente"
        QtWidgets.QMessageBox.warning(
            self,
            "Preset client illisible",
            "Le preset client est corrompu ou inaccessible. Il ne sera pas écrasé "
            "automatiquement. Utilisez Reset config pour repartir de zéro, ou restaurez "
            "la sauvegarde avant de relancer l'application.\n\n"
            f"Fichier : {path}\n"
            f"Sauvegarde ({backup_state}) : {backup}\n"
            f"État : {status.value}\n"
            f"Détail : {detail}",
        )

    def _preset_save(self, *, flush: bool = False) -> bool:
        if self._preset_write_blocked:
            logger.warning("client preset write blocked because the loaded file is invalid: {}", self._preset_path())
            return False
        try:
            snapshot = stamp_preset(self._preset, preset_type="client")
            self._preset = snapshot
            self._preset_writer.request(snapshot)
            if flush:
                self._preset_writer.flush(raise_on_error=True)
            return True
        except Exception as exc:
            logger.error("client preset save failed: {}", exc)
            return False

    def _preset_get(self, key: str, default=None):
        try:
            if isinstance(self._preset, dict) and key in self._preset:
                return self._preset.get(key)
        except Exception:
            return default
        return default

    def _preset_set(self, key: str, value) -> None:
        try:
            if not isinstance(self._preset, dict):
                self._preset = {}
            normalized_key = str(key)
            if (
                normalized_key in self._preset
                and self._preset.get(normalized_key) == value
            ):
                return
            self._preset[normalized_key] = value
        except Exception:
            return
        self._preset_save()

    def _on_ptt_bus_key_changed(self, bus_id: int) -> None:
        if int(bus_id) not in self._ptt_bus_keys:
            return
        try:
            if str(self._ptt_bus_keys[int(bus_id)].keySequence().toString()) in {"Ctrl", "Alt", "Shift", "Meta"}:
                self._ptt_bus_keys[int(bus_id)].setKeySequence(QtGui.QKeySequence(""))
                return
        except Exception:
            return
        try:
            cur = self._preset_get("ptt_bus_keys", {})
            if not isinstance(cur, dict):
                cur = {}
            seq = self._ptt_bus_keys[int(bus_id)].keySequence()
            cur[str(int(bus_id))] = str(seq.toString())
            self._preset_set("ptt_bus_keys", cur)
        except Exception:
            return

        with best_effort("_ptt_bus_keys[int(bus_id)].clearFocus()"):
            self._ptt_bus_keys[int(bus_id)].clearFocus()
            self.centralWidget().setFocus()

        if self._connected and self._global_ptt is None:
            self._apply_ptt_modes_to_client()

    def _on_input_device_changed(self, _idx: int) -> None:
        try:
            dev = self._input_device.currentData()
            if dev is None:
                self._update_device_selection_state()
                return
            self._preset_set("input_device", int(dev))
            info = self._device_cache.get(int(dev))
            if info is not None:
                self._preset_set("input_device_name", str(getattr(info, "name", "")))
                self._preset_set("input_device_hostapi", str(getattr(info, "hostapi", "")))
        except Exception:
            return
        self._update_device_selection_state()

    def _on_output_device_changed(self, _idx: int) -> None:
        try:
            dev = self._output_device.currentData()
            if dev is None:
                self._update_device_selection_state()
                return
            self._preset_set("output_device", int(dev))
            info = self._device_cache.get(int(dev))
            if info is not None:
                self._preset_set("output_device_name", str(getattr(info, "name", "")))
                self._preset_set("output_device_hostapi", str(getattr(info, "hostapi", "")))
        except Exception:
            return
        self._update_device_selection_state()

    def _on_show_all_devices_changed(self, state: int) -> None:
        enabled = bool(is_checked(state))
        self._preset_set("show_all_devices", enabled)
        self._start_device_refresh()

    def _update_device_selection_state(self) -> None:
        """Keep connection availability aligned with the two selectors."""

        input_selected = self._input_device.currentData() is not None
        output_selected = self._output_device.currentData() is not None
        refresh_active = self._device_thread is not None
        ready = bool(
            input_selected
            and output_selected
            and not self._connected
            and not refresh_active
        )
        self._connect_btn.setEnabled(ready)
        self._preflight_btn.setEnabled(ready)

        if input_selected and output_selected:
            self._connect_btn.setToolTip(
                "Démarrer les flux audio puis établir la session intercom"
            )
            if getattr(self, "_selection_hint_shown", False):
                self._device_status.setText("")
                self._selection_hint_shown = False
            return

        missing: list[str] = []
        if not input_selected:
            missing.append("microphone")
        if not output_selected:
            missing.append("casque")
        missing_text = " et ".join(missing)
        detail = f"Sélection requise : {missing_text}."
        self._connect_btn.setToolTip(detail)
        if not refresh_active and not self._last_device_error:
            self._device_status.setText(detail)
            self._selection_hint_shown = True

    def _reset_identity(self) -> None:
        if self._connected:
            QtWidgets.QMessageBox.warning(self, "Connected", "Disconnect first to reset identity")
            return

        if (
            QtWidgets.QMessageBox.question(
                self,
                "Reset identity",
                "Generate a new identity for this client?",
            )
            != QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return

        client_uuid = str(uuid.uuid4())
        self._preset_set("client_uuid", client_uuid)
        stable_id = int(client_id_from_uuid(client_uuid))
        self._client_id.setText(str(stable_id))

    def _show_geek_info(self) -> None:
        server_ip = self._server_ip.text().strip()
        server_port = int(AUDIO_UDP_PORT)
        client_uuid = str(self._preset_get("client_uuid", ""))
        client_id = self._client_id.text().strip()
        name = self._name.text().strip()

        st = self._client.get_stats_snapshot() if self._client is not None else {}

        preset_path = str(self._preset_path())
        preset_exists = False
        try:
            preset_exists = Path(preset_path).exists()
        except Exception:
            preset_exists = False

        info = "\n".join(
            [
                f"Server: {server_ip}:{server_port}",
                f"Name: {name}",
                "Mode: PTT only",
                f"Client UUID: {client_uuid or '-'}",
                f"Client ID: {client_id or '-'}",
                f"Control connected: {st.get('control_connected', False)}",
                f"Welcome received: {st.get('control_welcome_received', False)}",
                f"Control ready: {st.get('control_ready', False)}",
                f"Control age (s): {st.get('control_age_s') if st.get('control_age_s') is not None else '-'}",
                f"Control TX age (s): {st.get('control_tx_age_s') if st.get('control_tx_age_s') is not None else '-'}",
                f"UDP RX age (s): {st.get('udp_rx_age_s') if st.get('udp_rx_age_s') is not None else '-'}",
                f"Network workers: {st.get('network_workers_alive', '-')} ",
                f"Input stream active: {st.get('in_stream_active', st.get('input_stream_active', '-'))} ",
                f"Output stream active: {st.get('out_stream_active', st.get('output_stream_active', '-'))} ",
                f"Stopped: {st.get('stopped', False)}",
                f"Kicked: {st.get('kicked', False)}",
                "",
                f"Client preset: {preset_path}",
                f"Preset exists: {preset_exists}",
                "",
                f"In: ch={st.get('in_channels', '-')} sr={st.get('in_samplerate', '-')}",
                f"Out: ch={st.get('out_channels', '-')} sr={st.get('out_samplerate', '-')} ",
                f"Buf capture samples: {st.get('capture_samples', '-')} ",
                f"Buf playback samples: {st.get('playback_samples', '-')} ",
                "",
                f"TX pkts: {st.get('tx_packets', '-')} ",
                f"TX udp sent: {st.get('tx_udp_sent', '-')} ",
                f"TX sock err: {st.get('tx_socket_errors', '-')} ",
                f"Opus enc err: {st.get('opus_encode_errors', '-')} ",
                f"RX pkts: {st.get('rx_packets', '-')} ",
                f"RX sock err: {st.get('rx_socket_errors', '-')} ",
                f"Opus dec err: {st.get('opus_decode_errors', '-')} ",
                f"Opus OK: {st.get('opus_ok', '-')} ",
                f"Opuslib ver: {st.get('opuslib_version', '-')} ",
            ]
        )

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Info")
        box.setText(info)
        reset_cfg_btn = box.addButton("Reset config", QtWidgets.QMessageBox.ActionRole)
        reset_btn = box.addButton("Reset identity", QtWidgets.QMessageBox.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is reset_cfg_btn:
            self._reset_total_config()
        elif box.clickedButton() is reset_btn:
            self._reset_identity()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._device_thread is not None and self._device_thread.isRunning():
            self._close_after_device_refresh = True
            self._device_thread.requestInterruption()
            self._device_status.setText("Waiting for device enumeration to finish before closing...")
            event.ignore()
            return

        self._is_closing = True
        # Full stop: network + audio streams
        with best_effort("_stop_discovery_listener()"):
            self._stop_discovery_listener()
        with best_effort("_timer.stop()"):
            self._timer.stop()
            self._vu_timer.stop()
        with best_effort("stop global PTT hotkeys"):
            if self._global_ptt is not None:
                self._global_ptt.stop()
        self._global_ptt = None
        if self._client is not None:
            with best_effort("_client.stop()"):
                self._client.stop()
            self._client = None
        self._connected = False
        try:
            self._preset_writer.close(raise_on_error=True)
        except Exception as exc:
            logger.error("client preset flush failed during close: {}", exc)
        super().closeEvent(event)

    def _start_device_refresh(self) -> None:
        if self._is_closing:
            return
        if self._device_thread is not None:
            self._device_status.setText("Refreshing devices already in progress...")
            return
        if self._connected:
            self._device_status.setText("Disconnect before refreshing audio devices.")
            return

        self._refresh_devices_btn.setEnabled(False)
        self._show_all_devices.setEnabled(False)
        self._connect_btn.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WaitCursor))
        self._device_cursor_active = True

        hostapi_filter = None if self._show_all_devices.isChecked() else "WASAPI"

        self._device_status.setText(f"Refreshing devices ({hostapi_filter or 'ALL'})...")
        QtWidgets.QApplication.processEvents()
        self._last_device_error = ""
        self._last_device_count = 0
        self._device_refresh_timed_out_flag = False

        self._device_thread = QtCore.QThread(self)
        # If an existing client keeps PortAudio streams open for fast
        # reconnect, enumerate without the private PortAudio reinitialization.
        hard_refresh = self._client is None
        self._device_worker = DeviceWorker(
            hostapi_filter=hostapi_filter,
            hard_refresh=hard_refresh,
        )
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
        self._refresh_devices_btn.setEnabled(not self._connected)
        self._show_all_devices.setEnabled(not self._connected)
        self._restore_device_refresh_cursor()

        if not self._last_device_error:
            self._device_status.setText(f"Devices loaded: {self._last_device_count}")
        self._update_device_selection_state()

        if self._close_after_device_refresh:
            self._close_after_device_refresh = False
            QtCore.QTimer.singleShot(0, self.close)
        elif not self._last_device_error and not self._is_closing:
            QtCore.QTimer.singleShot(0, self._maybe_autoconnect)

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

        self._device_cache = {int(d.index): d for d in devs}

        cur_in = self._input_device.currentData()
        cur_out = self._output_device.currentData()
        try:
            saved_in = int(self._preset_get("input_device", -1))
        except Exception:
            saved_in = -1
        saved_in_name = str(self._preset_get("input_device_name", "") or "")
        saved_in_hostapi = str(self._preset_get("input_device_hostapi", "") or "")
        try:
            saved_out = int(self._preset_get("output_device", -1))
        except Exception:
            saved_out = -1
        saved_out_name = str(self._preset_get("output_device_name", "") or "")
        saved_out_hostapi = str(self._preset_get("output_device_hostapi", "") or "")

        self._input_device.blockSignals(True)
        self._output_device.blockSignals(True)
        try:
            self._input_device.clear()
            self._output_device.clear()

            for d in devs:
                label = format_device_label(d)
                if bool(getattr(d, "supports_input", False)):
                    self._input_device.addItem(label, d.index)
                if bool(getattr(d, "supports_output", False)):
                    self._output_device.addItem(label, d.index)

            # QComboBox selects its first item while it is being populated.
            # Reset explicitly so a missing/ambiguous saved endpoint never
            # turns into a silent switch to an unrelated microphone.
            self._input_device.setCurrentIndex(-1)
            self._output_device.setCurrentIndex(-1)

            resolved_in = resolve_device(
                saved_in_name,
                saved_in_hostapi,
                int(saved_in) if int(saved_in) >= 0 else None,
                devs,
                direction="input",
            )
            if resolved_in is None and cur_in is not None:
                resolved_in = resolve_device(
                    "", "", int(cur_in), devs, direction="input"
                )
            if resolved_in is not None:
                i = self._input_device.findData(int(resolved_in))
                if i >= 0:
                    self._input_device.setCurrentIndex(i)
                info = self._device_cache.get(int(resolved_in))
                if info is not None:
                    self._preset_set("input_device", int(resolved_in))
                    self._preset_set("input_device_name", str(getattr(info, "name", "")))
                    self._preset_set("input_device_hostapi", str(getattr(info, "hostapi", "")))

            resolved_out = resolve_device(
                saved_out_name,
                saved_out_hostapi,
                int(saved_out) if int(saved_out) >= 0 else None,
                devs,
                direction="output",
            )
            if resolved_out is None and cur_out is not None:
                resolved_out = resolve_device(
                    "", "", int(cur_out), devs, direction="output"
                )
            if resolved_out is not None:
                i = self._output_device.findData(int(resolved_out))
                if i >= 0:
                    self._output_device.setCurrentIndex(i)
                info = self._device_cache.get(int(resolved_out))
                if info is not None:
                    self._preset_set("output_device", int(resolved_out))
                    self._preset_set("output_device_name", str(getattr(info, "name", "")))
                    self._preset_set("output_device_hostapi", str(getattr(info, "hostapi", "")))
        finally:
            self._input_device.blockSignals(False)
            self._output_device.blockSignals(False)
        self._update_device_selection_state()

    def _connect(self) -> None:
        if self._connected:
            return
        if self._device_thread is not None:
            self._device_status.setText("Wait for device enumeration before connecting.")
            return

        server_ip = self._server_ip.text().strip()
        if not server_ip:
            QtWidgets.QMessageBox.warning(self, "Missing server", "Please enter server IP")
            return

        server_port = int(AUDIO_UDP_PORT)

        client_id = int(self._client_id.text().strip())

        input_device = self._input_device.currentData()
        output_device = self._output_device.currentData()
        if input_device is None or output_device is None:
            self._update_device_selection_state()
            QtWidgets.QMessageBox.warning(
                self,
                "Périphérique manquant",
                "Sélectionnez explicitement un microphone et un casque.",
            )
            return

        self._preset_set("input_device", int(input_device))
        self._preset_set("output_device", int(output_device))

        name = self._name.text().strip()
        self._preset_set("server_ip", server_ip)
        self._preset_set("server_port", int(AUDIO_UDP_PORT))
        self._preset_set("name", name)

        client_uuid = str(self._preset_get("client_uuid", ""))

        cfg = ClientConfig(
            server_ip=server_ip,
            server_port=int(server_port),
            control_port=int(server_port) + 1,
            name=name,
            client_uuid=client_uuid,
            input_device=int(input_device),
            output_device=int(output_device),
            input_gain_db=float(self._mic_gain.value()),
            output_gain_db=float(self._hp_gain.value()),
            return_gain_db=float(self._return_gain.value()),
            ptt_bus_keys=dict(self._preset_get("ptt_bus_keys", {}) or {}),
            listen_return_bus=bool(self._listen_return_bus.isChecked()),
            duck_return=bool(self._duck_return.isChecked()),
            listen_regie=bool(self._listen_regie.isChecked()),
        )

        # Reuse existing client if audio devices haven't changed (avoids PortAudio reopen issues on Windows)
        can_reuse = (
            self._client is not None
            and self._client.config.input_device == cfg.input_device
            and self._client.config.output_device == cfg.output_device
        )

        new_client: IntercomClient | None = None
        try:
            if can_reuse:
                # Update config fields that may have changed
                self._client.config.server_ip = cfg.server_ip
                self._client.config.server_port = cfg.server_port
                self._client.config.control_port = int(cfg.server_port) + 1
                self._client.config.name = cfg.name
                self._client.config.client_uuid = cfg.client_uuid
                self._client.config.ptt_bus_keys = cfg.ptt_bus_keys
                self._client.config.listen_return_bus = bool(cfg.listen_return_bus)
                self._client.config.listen_regie = bool(cfg.listen_regie)
                self._client.set_input_gain_db(cfg.input_gain_db)
                self._client.set_output_gain_db(cfg.output_gain_db)
                self._client.set_return_gain_db(cfg.return_gain_db)
                self._client.set_listen_return_bus(bool(cfg.listen_return_bus))
                self._client.set_duck_return(bool(cfg.duck_return))
                self._client.set_listen_regie(bool(cfg.listen_regie))
                self._client.reconnect_network()
            else:
                # Full stop of old client if devices changed
                if self._client is not None:
                    with best_effort("_client.stop()"):
                        self._client.stop()
                new_client = IntercomClient(client_id=client_id, config=cfg)
                new_client.start()
                self._client = new_client
        except Exception as e:
            if new_client is not None:
                with best_effort("new_client.stop()"):
                    new_client.stop()
            elif self._client is not None:
                with best_effort("_client.stop()"):
                    self._client.stop()
            self._client = None
            self._connected = False
            QtWidgets.QMessageBox.critical(self, "Connect failed", str(e))
            return

        self._connected = True
        self._ever_online = False
        self._ptt_start_failed = False
        self._set_operational_status(
            ClientOperationalStatus(
                ClientOperationalState.CONNECTING,
                "Connexion…",
                "Démarrage des flux audio et du contrôle serveur.",
            )
        )

        # No PTT (including Always On) is armed until the control handshake is
        # confirmed by the client's live statistics.
        with best_effort("release all PTT buses"):
            for bus_id in self._ptt_bus_keys:
                self._client.set_ptt_bus(int(bus_id), False)

        with best_effort("disable PTT shortcut editors"):
            for edit in self._ptt_bus_keys.values():
                edit.setEnabled(False)
            for b in self._ptt_bus_clear.values():
                b.setEnabled(False)
            for mode_widget in self._ptt_bus_mode_widgets.values():
                mode_widget.setEnabled(False)

        self._name.setReadOnly(True)
        self._server_ip.setReadOnly(True)
        self._discovered_servers.setEnabled(False)
        self._input_device.setEnabled(False)
        self._output_device.setEnabled(False)
        self._show_all_devices.setEnabled(False)
        self._refresh_devices_btn.setEnabled(False)

        self._connect_btn.setEnabled(False)
        self._preflight_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._listen_regie.setEnabled(True)
        self._listen_return_bus.setEnabled(True)
        self._mic_gain.setEnabled(True)
        self._hp_gain.setEnabled(True)
        self._return_gain.setEnabled(True)

        self._timer.start()
        self._vu_timer.start()

    def _disconnect(self) -> None:
        if not self._connected:
            return
        self._kick_notified = False
        self._ever_online = False
        self._ptt_start_failed = False
        try:
            self._timer.stop()
            self._vu_timer.stop()
            with best_effort("release PTT on close"):
                if self._client is not None:
                    for bid in self._ptt_bus_keys.keys():
                        self._client.set_ptt_bus(int(bid), False)

            with best_effort("stop global PTT hotkeys"):
                if self._global_ptt is not None:
                    self._global_ptt.stop()
            self._global_ptt = None

            # Only disconnect network, keep audio streams alive for fast reconnect
            if self._client is not None:
                self._client.disconnect_network()
        finally:
            self._connected = False
            self._set_operational_status(
                ClientOperationalStatus(
                    ClientOperationalState.DISCONNECTED,
                    "Déconnecté",
                    "PTT désarmé. Aucune session intercom active.",
                )
            )

            self._name.setReadOnly(False)
            self._server_ip.setReadOnly(False)
            self._discovered_servers.setEnabled(True)
            self._input_device.setEnabled(True)
            self._output_device.setEnabled(True)
            self._show_all_devices.setEnabled(True)
            self._refresh_devices_btn.setEnabled(self._device_thread is None)

            self._update_device_selection_state()
            self._disconnect_btn.setEnabled(False)
            self._listen_regie.setEnabled(False)
            self._listen_return_bus.setEnabled(False)
            self._mic_gain.setEnabled(False)
            self._hp_gain.setEnabled(False)
            self._return_gain.setEnabled(False)
            self._in_vu.set_level(-60.0)
            self._out_vu.set_level(-60.0)
            self._return_vu.set_level(-60.0)
            for vu_w in self._ptt_bus_vu.values():
                with best_effort("vu_w.set_level()"):
                    vu_w.set_level(-60.0)

            with best_effort("apply PTT modes"):
                for bid in self._ptt_bus_keys.keys():
                    edit = self._ptt_bus_keys.get(int(bid))
                    clear_btn = self._ptt_bus_clear.get(int(bid))
                    if edit is not None:
                        edit.setEnabled(True)
                    if clear_btn is not None:
                        clear_btn.setEnabled(True)
                    mode_widget = self._ptt_bus_mode_widgets.get(int(bid))
                    if mode_widget is not None:
                        mode_widget.setEnabled(True)

    def _on_listen_regie_changed(self, state: int) -> None:
        enabled = is_checked(state)
        with best_effort("save listen_regie"):
            self._preset_set("listen_regie", bool(enabled))
        if self._client is None:
            return
        self._client.set_listen_regie(bool(enabled))

    def _on_listen_return_bus_changed(self, state: int) -> None:
        enabled = is_checked(state)
        with best_effort("save listen_return_bus"):
            self._preset_set("listen_return_bus", bool(enabled))
        if self._client is None:
            return
        self._client.set_listen_return_bus(bool(enabled))

    def _open_preflight(self) -> None:
        input_device = self._input_device.currentData()
        output_device = self._output_device.currentData()
        if self._connected or input_device is None or output_device is None:
            return
        dialog = PreflightDialog(
            self,
            server_host=self._server_ip.text().strip(),
            control_port=int(AUDIO_UDP_PORT) + int(CONTROL_PORT_OFFSET),
            input_device=int(input_device),
            output_device=int(output_device),
            shortcuts={
                int(bus_id): edit.keySequence().toString()
                for bus_id, edit in self._ptt_bus_keys.items()
            },
        )
        dialog.exec()

    def _on_duck_return_changed(self, state: int) -> None:
        enabled = is_checked(state)
        with best_effort("save duck_return"):
            self._preset_set("duck_return", bool(enabled))
        if self._client is not None:
            self._client.set_duck_return(bool(enabled))

    def _on_mic_gain_changed(self, value: int) -> None:
        self._mic_gain_lbl.setText(f"{value} dB")
        with best_effort("save input_gain_db"):
            self._preset_set("input_gain_db", float(value))
        if self._client is None:
            return
        self._client.set_input_gain_db(float(value))

    def _on_hp_gain_changed(self, value: int) -> None:
        self._hp_gain_lbl.setText(f"{value} dB")
        with best_effort("save output_gain_db"):
            self._preset_set("output_gain_db", float(value))
        if self._client is None:
            return
        self._client.set_output_gain_db(float(value))

    def _on_return_gain_changed(self, _val) -> None:
        try:
            val = int(self._return_gain.value())
        except Exception:
            val = 0
        self._return_gain_lbl.setText(f"{int(val)} dB")
        with best_effort("save return_gain_db"):
            self._preset_set("return_gain_db", int(val))
        if self._client is not None:
            with best_effort("_client.set_return_gain_db()"):
                self._client.set_return_gain_db(float(val))

    def _on_start_minimized_changed(self, state: int) -> None:
        with best_effort("save start_minimized"):
            self._preset_set("start_minimized", bool(is_checked(state)))

    def _on_autoconnect_changed(self, state: int) -> None:
        with best_effort("save autoconnect"):
            self._preset_set("autoconnect", bool(is_checked(state)))

    def _maybe_autoconnect(self) -> None:
        if (
            not self._connected
            and self._device_thread is None
            and self._input_device.currentData() is not None
            and self._output_device.currentData() is not None
            and bool(self._autoconnect_cb.isChecked())
        ):
            self._connect()

    def _refresh_stats(self) -> None:
        if self._client is None:
            return
        st = self._client.get_stats_snapshot()

        if bool(st.get("kicked", False)) and not bool(self._kick_notified):
            self._kick_notified = True
            msg = str(st.get("kick_message") or "Le serveur a fermé cette session.")
            QtWidgets.QMessageBox.information(self, "Session fermée", msg)
            self._disconnect()
            return

        status = derive_client_operational_status(
            session_requested=self._connected,
            ever_online=self._ever_online,
            stats=st,
        )
        self._set_operational_status(status)

        try:
            shared_input_gain = int(round(float(st.get("input_gain_db", self._mic_gain.value()))))
        except Exception:
            shared_input_gain = int(self._mic_gain.value())
        if int(self._mic_gain.value()) != int(shared_input_gain):
            self._mic_gain.blockSignals(True)
            try:
                self._mic_gain.setValue(int(shared_input_gain))
            finally:
                self._mic_gain.blockSignals(False)
            self._mic_gain_lbl.setText(f"{int(shared_input_gain)} dB")
            with best_effort("save server input gain"):
                self._preset_set("input_gain_db", float(shared_input_gain))

        buses_raw = st.get("buses")
        parsed_buses: dict[int, dict] = {}
        if isinstance(buses_raw, dict):
            for k, b in buses_raw.items():
                if not isinstance(b, dict):
                    continue
                try:
                    bid = int(k)
                except Exception:
                    try:
                        bid = int(b.get("bus_id"))
                    except (TypeError, ValueError):
                        continue
                definition = BUS_BY_ID.get(int(bid))
                if definition is None:
                    continue
                parsed_buses[int(bid)] = {
                    "bus_id": int(bid),
                    "name": str(b.get("name") or definition.name),
                    "feed_to_regie": bool(
                        b.get("feed_to_regie", definition.default_feed_to_regie)
                    ),
                }
        elif isinstance(buses_raw, list):
            for b in buses_raw:
                if not isinstance(b, dict):
                    continue
                try:
                    bid = int(b.get("bus_id"))
                except (TypeError, ValueError):
                    continue
                definition = BUS_BY_ID.get(int(bid))
                if definition is None:
                    continue
                parsed_buses[int(bid)] = {
                    "bus_id": int(bid),
                    "name": str(b.get("name") or definition.name),
                    "feed_to_regie": bool(
                        b.get("feed_to_regie", definition.default_feed_to_regie)
                    ),
                }

        if parsed_buses:
            self._sync_bus_widgets(parsed_buses)

        with best_effort("_refresh_state_labels()"):
            self._refresh_state_labels()

    def _refresh_vu_meters(self) -> None:
        if self._client is None or not self._connected:
            return
        st = self._client.get_stats_snapshot()
        self._in_vu.set_level(float(st.get("in_vu_dbfs", -60.0)))
        self._out_vu.set_level(float(st.get("out_vu_dbfs", -60.0)))
        self._return_vu.set_level(float(st.get("return_vu_dbfs", -60.0)))

        bus_vu = st.get("bus_vu_dbfs")
        if isinstance(bus_vu, dict):
            for bid, vu_widget in self._ptt_bus_vu.items():
                try:
                    vu_widget.set_level(float(bus_vu.get(int(bid), -60.0)))
                except Exception:
                    vu_widget.set_level(-60.0)

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        with best_effort("release PTT on window deactivation"):
            if event.type() == QtCore.QEvent.Type.WindowDeactivate:
                if self._global_ptt is not None:
                    self._global_ptt.release_all()

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
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("py-intercom.client")


def run_gui(minimized: bool = False) -> int:
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    win = ClientWindow()

    if minimized or bool(win._start_minimized.isChecked()):
        win.showMinimized()
    else:
        win.show()
    return int(app.exec())

