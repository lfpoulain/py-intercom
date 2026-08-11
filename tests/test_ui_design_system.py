from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from py_intercom.common.theme import (
    EmptyStateTableWidget,
    apply_theme,
    make_field,
)
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


def test_empty_state_table_is_explicit_and_renderable(qt_app) -> None:
    table = EmptyStateTableWidget(0, 3, "Aucune destination configurée.")
    table.setHorizontalHeaderLabels(["Nom", "Bus", "État"])
    table.resize(520, 120)
    table.show()
    qt_app.processEvents()

    rendered = QtGui.QPixmap(table.size())
    table.render(rendered)

    assert table.empty_text() == "Aucune destination configurée."
    assert table.accessibleDescription() == table.empty_text()
    assert table.showGrid() is False
    assert not rendered.isNull()

    table.set_empty_text("En attente de clients.")
    assert table.empty_text() == "En attente de clients."
    assert table.accessibleDescription() == "En attente de clients."
    table.close()


def test_theme_exposes_shared_operational_roles(qt_app) -> None:
    stylesheet = qt_app.styleSheet()

    assert 'QFrame[role="hero"]' in stylesheet
    assert 'QLabel[role="heroTitle"]' in stylesheet
    assert 'QLabel[role="statusBanner"]' in stylesheet
    assert 'QLabel[role="fieldLabel"]' in stylesheet
    assert qt_app.palette().color(QtGui.QPalette.ColorRole.Window) == QtGui.QColor(
        "#0b111a"
    )


def test_server_workspace_uses_resizable_supervision_and_routing_columns(
    qt_app,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ServerWindow, "_start_device_refresh", lambda _self: None)

    window = ServerWindow()
    window.resize(1280, 820)
    window.show()
    qt_app.processEvents()

    splitter = window._workspace_splitter
    assert splitter.orientation() == QtCore.Qt.Orientation.Horizontal
    assert splitter.count() == 2
    assert splitter.widget(0).objectName() == "supervisionColumn"
    assert splitter.widget(1).objectName() == "routingColumn"
    assert splitter.childrenCollapsible() is False
    assert window._buses.minimumHeight() >= 180
    assert window._outputs.minimumHeight() >= 200

    window.close()
