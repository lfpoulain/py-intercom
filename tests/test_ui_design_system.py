from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from py_intercom.common.console import (
    ChannelStrip,
    Fader,
    LevelMeter,
    StripRack,
    TalkPad,
    chip,
    format_db,
)
from py_intercom.common.theme import TOKENS, apply_theme, make_field
from py_intercom.server.gui import ServerWindow


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_theme(app)
    return app


def test_field_factory_keeps_label_and_control_associated(qt_app) -> None:
    control = QtWidgets.QLineEdit()

    field = make_field("ADRESSE SERVEUR", control, minimum_width=180)
    label = field.findChild(QtWidgets.QLabel)

    assert field.property("role") == "field"
    assert field.minimumWidth() == 180
    assert label is not None
    assert label.text() == "ADRESSE SERVEUR"
    assert label.property("role") == "fieldLabel"
    assert label.buddy() is control


def test_theme_exposes_shared_operational_roles(qt_app) -> None:
    stylesheet = qt_app.styleSheet()

    for selector in (
        'QFrame[role="topbar"]',
        'QFrame[role="strip"]',
        'QLabel[role="statusBanner"]',
        'QLabel[role="fieldLabel"]',
        'QPushButton[role="segment"]',
    ):
        assert selector in stylesheet
    assert qt_app.palette().color(QtGui.QPalette.ColorRole.Window) == QtGui.QColor(TOKENS["bg"])


def test_level_meter_clamps_and_holds_its_peak(qt_app) -> None:
    meter = LevelMeter()

    meter.set_level(12.0)
    assert meter.level() == 0.0
    meter.set_level(-200.0)
    assert meter.level() == -60.0
    assert meter._peak == 0.0  # still held right after the loud frame


def test_fader_maps_clicks_to_values_and_double_click_recalls_zero(qt_app) -> None:
    fader = Fader(-60, 20)
    fader.resize(fader.width(), 300)
    fader.show()
    seen: list[int] = []
    fader.valueChanged.connect(seen.append)

    top = QtCore.QPoint(fader.width() // 2, 2)
    QtTest.QTest.mouseClick(fader, QtCore.Qt.MouseButton.LeftButton, pos=top)
    assert fader.value() == 20

    QtTest.QTest.mouseDClick(fader, QtCore.Qt.MouseButton.LeftButton, pos=top)
    assert fader.value() == 0
    assert seen and seen[-1] == 0
    fader.close()


def test_talk_pad_reports_state_and_emits_press_release(qt_app) -> None:
    pad = TalkPad("Regie", "#38bdf8")
    events: list[str] = []
    pad.pressed.connect(lambda: events.append("pressed"))
    pad.released.connect(lambda: events.append("released"))
    pad.resize(200, 200)
    pad.show()

    pad.set_state("live", "EN PAROLE", "Transmission en cours")
    QtTest.QTest.mousePress(pad, QtCore.Qt.MouseButton.LeftButton)
    QtTest.QTest.mouseRelease(pad, QtCore.Qt.MouseButton.LeftButton)

    assert pad.state == "live"
    assert pad.toolTip() == "Transmission en cours"
    assert events == ["pressed", "released"]
    assert not pad.grab().isNull()
    pad.close()


def test_chip_toggles_from_anywhere_on_its_surface(qt_app) -> None:
    box = chip("DUCKING")
    box.resize(120, 22)
    box.show()

    QtTest.QTest.mouseClick(box, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(110, 11))

    assert box.isChecked()
    box.close()


def test_strip_rack_shows_its_empty_state_only_without_strips(qt_app) -> None:
    rack = StripRack("Aucun poste.")
    rack.resize(400, 300)
    rack.show()
    qt_app.processEvents()
    assert rack._empty.isVisible()

    rack.add_strip(ChannelStrip("Cadreur", fader=Fader(), meter=LevelMeter()))
    qt_app.processEvents()
    assert not rack._empty.isVisible()
    assert len(rack.strips()) == 1

    rack.clear()
    qt_app.processEvents()
    assert rack._empty.isVisible()
    rack.close()


def test_db_readouts_are_signed() -> None:
    assert format_db(3) == "+3 dB"
    assert format_db(0) == "0 dB"
    assert format_db(-12.4) == "-12 dB"


def test_server_console_splits_team_and_routing(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(ServerWindow, "_start_device_refresh", lambda _self: None)

    window = ServerWindow()
    window.resize(1320, 820)
    window.show()
    qt_app.processEvents()

    splitter = window._workspace_splitter
    assert splitter.orientation() == QtCore.Qt.Orientation.Vertical
    assert splitter.count() == 2
    assert splitter.widget(0).objectName() == "teamRow"
    assert splitter.widget(1).objectName() == "routingRow"
    assert splitter.childrenCollapsible() is False
    assert set(window._bus_strips) == {0, 1, 2}
    assert window._start_btn.isVisibleTo(window) and not window._stop_btn.isVisibleTo(window)

    window.close()
