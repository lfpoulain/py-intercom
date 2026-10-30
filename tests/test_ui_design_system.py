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
    compact_combo,
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

    field = make_field("ADRESSE SERVEUR", control)
    label = field.findChild(QtWidgets.QLabel)

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


def _wheel(widget: QtWidgets.QWidget, notches: int = -1) -> bool:
    """Send one wheel notch; return whether the widget kept the event."""
    pos = QtCore.QPointF(widget.width() / 2, widget.height() / 2)
    event = QtGui.QWheelEvent(
        pos,
        QtCore.QPointF(widget.mapToGlobal(pos.toPoint())),
        QtCore.QPoint(),
        QtCore.QPoint(0, 120 * notches),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
        QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QtWidgets.QApplication.sendEvent(widget, event)
    return event.isAccepted()


def test_wheel_only_acts_on_focused_faders_and_strip_combos(qt_app) -> None:
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    elsewhere = QtWidgets.QLineEdit()
    fader = Fader(-60, 20)
    combo = compact_combo(QtWidgets.QComboBox())
    combo.addItems(["Carte A", "Carte B"])
    lay.addWidget(elsewhere)
    lay.addWidget(fader)
    lay.addWidget(combo)
    host.resize(200, 300)
    host.show()
    host.activateWindow()
    QtTest.QTest.qWaitForWindowActive(host)
    elsewhere.setFocus()
    assert not fader.hasFocus() and not combo.hasFocus()

    # Hovering while scrolling must neither move a gain nor reroute a card;
    # the ignored wheel goes on to the container (the strip rack scrolls).
    assert not _wheel(fader)
    assert not _wheel(combo)
    assert fader.value() == 0
    assert combo.currentIndex() == 0
    assert not fader.hasFocus() and not combo.hasFocus()

    fader.setFocus()
    assert fader.hasFocus()
    _wheel(fader)
    assert fader.value() < 0
    combo.setFocus()
    _wheel(combo)
    assert combo.currentIndex() == 1
    host.close()


def test_fader_repaints_only_when_its_colour_changes(qt_app) -> None:
    fader = Fader()
    repaints: list[bool] = []
    fader.update = lambda: repaints.append(True)

    fader.set_color("#ff0000")
    fader.set_color("#ff0000")

    assert repaints == [True]


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


def test_talk_pad_press_survives_a_slide_off_and_back(qt_app) -> None:
    pad = TalkPad("Plateau", "#fbbf24")
    events: list[str] = []
    pad.pressed.connect(lambda: events.append("pressed"))
    pad.released.connect(lambda: events.append("released"))
    pad.resize(200, 200)
    pad.show()

    QtTest.QTest.mousePress(pad, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(100, 100))
    QtTest.QTest.mouseMove(pad, QtCore.QPoint(260, 100))
    QtTest.QTest.mouseMove(pad, QtCore.QPoint(100, 100))
    QtTest.QTest.mouseRelease(pad, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(100, 100))

    assert events == ["pressed", "released"]
    pad.close()
