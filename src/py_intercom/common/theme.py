from __future__ import annotations

import os
from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

def apply_theme(app: QtWidgets.QApplication) -> None:
    """Apply qt-material dark_teal theme with project-wide customisations."""
    theme = os.environ.get("PY_INTERCOM_QT_THEME", "dark_teal.xml")
    invert_secondary = theme.lower().startswith("light_")
    try:
        from qt_material import apply_stylesheet

        extra = {
            # Compact layout
            "density_scale": "-2",

            # Semantic button colours
            "danger": "#dc3545",
            "warning": "#ffc107",
            "success": "#17a2b8",

            # Font
            "font_family": "Segoe UI, Roboto, sans-serif",
            "font_size": "13px",
            "line_height": "13px",

            # Compact QMenu items
            "QMenu": {
                "height": 28,
                "padding": "4px 8px 4px 8px",
            },
        }
        apply_stylesheet(app, theme=theme, invert_secondary=invert_secondary, extra=extra)
    except Exception as e:
        logger.warning("qt-material apply_stylesheet() failed: {}", e)

    # Refinements that qt-material extra dict cannot express:
    # - combo-box popup: dark selection, compact padding
    # - button borders always visible, hover highlight, disabled state
    # - QGroupBox title spacing
    # Use env vars set by qt-material for colour consistency
    primary = os.environ.get("QTMATERIAL_PRIMARYCOLOR", "#009688")
    primary_light = os.environ.get("QTMATERIAL_PRIMARYLIGHTCOLOR", "#4db6ac")
    primary_text = os.environ.get(
        "QTMATERIAL_PRIMARYTEXTCOLOR",
        "#000000" if invert_secondary else "#ffffff",
    )
    popup_bg = os.environ.get(
        "QTMATERIAL_SECONDARYDARKCOLOR",
        "#ffffff" if invert_secondary else "#2b2b2b",
    )
    popup_fg = os.environ.get(
        "QTMATERIAL_SECONDARYTEXTCOLOR",
        "#000000" if invert_secondary else "#ffffff",
    )

    warning = "#ffc107"
    danger = "#dc3545"
    success = "#17a2b8"

    patch = f"""
        QComboBox {{
            padding: 2px 4px;
            max-height: 22px;
        }}
        QComboBox QAbstractItemView {{
            color: {popup_fg};
            background: {popup_bg};
            selection-background-color: {primary};
            selection-color: {primary_text};
            outline: none;
        }}
        QPushButton, QToolButton {{
            border: 1px solid {primary_light};
            padding: 3px 10px;
        }}
        QPushButton:hover, QToolButton:hover {{
            background-color: {primary};
            border: 1px solid {primary_light};
        }}
        QPushButton[class="warning"], QToolButton[class="warning"] {{
            border: 1px solid {warning};
            background-color: {warning};
            color: #111111;
        }}
        QPushButton[class="warning"]:hover, QToolButton[class="warning"]:hover {{
            background-color: #e0a800;
            border: 1px solid #e0a800;
            color: #111111;
        }}
        QPushButton[class="success"], QToolButton[class="success"] {{
            border: 1px solid {success};
            background-color: {success};
            color: #ffffff;
        }}
        QPushButton[class="success"]:hover, QToolButton[class="success"]:hover {{
            background-color: #138496;
            border: 1px solid #138496;
            color: #ffffff;
        }}
        QPushButton[class="danger"], QToolButton[class="danger"] {{
            border: 1px solid {danger};
            background-color: {danger};
            color: #ffffff;
        }}
        QPushButton[class="danger"]:hover, QToolButton[class="danger"]:hover {{
            background-color: #bd2130;
            border: 1px solid #bd2130;
            color: #ffffff;
        }}
        QPushButton:disabled, QToolButton:disabled {{
            border: 1px solid #555555;
            color: #666666;
            background-color: transparent;
        }}
        QGroupBox {{
            padding-top: 14px;
            margin-top: 6px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            background-color: palette(window);
            padding: 0 6px;
        }}

        /* --- Slider --- */
        QSlider::groove:horizontal {{
            height: 6px;
            background: #3a3a3a;
            border-radius: 3px;
        }}
        QSlider::handle:horizontal {{
            background: {primary};
            border: 1px solid {primary_light};
            width: 14px;
            height: 14px;
            margin: -5px 0;
            border-radius: 7px;
        }}
        QSlider::handle:horizontal:hover {{
            background: {primary_light};
        }}
        QSlider::sub-page:horizontal {{
            background: {primary};
            border-radius: 3px;
        }}

        /* --- Table --- */
        QTableWidget {{
            gridline-color: #3a3a3a;
            alternate-background-color: rgba(255, 255, 255, 6);
        }}
        QHeaderView::section {{
            background-color: #2a2a2a;
            color: {primary_light};
            border: none;
            border-bottom: 2px solid {primary};
            padding: 4px 6px;
            font-weight: bold;
        }}

        /* --- Status bar --- */
        QStatusBar {{
            background: #1e1e1e;
            color: #aaaaaa;
            border-top: 1px solid #3a3a3a;
            font-size: 12px;
        }}
        QStatusBar QLabel {{
            padding: 0 6px;
        }}
    """
    existing = app.styleSheet() or ""
    app.setStyleSheet(existing + patch)

    # Palette: dark highlight for list/table selections
    pal = app.palette()
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(primary))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(primary_text))
    app.setPalette(pal)


# ---------------------------------------------------------------------------
# Compact combo-box item delegate
# ---------------------------------------------------------------------------

class _CompactItemDelegate(QtWidgets.QStyledItemDelegate):
    """Forces a fixed row height in combo-box popup lists."""

    def __init__(self, height: int = 22, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._height = height

    def sizeHint(self, option, index):  # noqa: N802
        s = super().sizeHint(option, index)
        s.setHeight(self._height)
        return s


def patch_combo(combo: QtWidgets.QComboBox, item_height: int = 22) -> None:
    """Apply compact item height to a QComboBox popup."""
    combo.setItemDelegate(_CompactItemDelegate(item_height, combo))


def centered_checkbox(cb: QtWidgets.QCheckBox) -> QtWidgets.QWidget:
    """Wrap a QCheckBox in a container that centers it horizontally."""
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(cb)
    return w


# ---------------------------------------------------------------------------
# Status indicator (colored dot: green=online, red=offline)
# ---------------------------------------------------------------------------

_STATUS_ONLINE = QtGui.QColor(76, 175, 80)    # Material green 500
_STATUS_OFFLINE = QtGui.QColor(198, 40, 40)   # Material red 800


class StatusIndicator(QtWidgets.QWidget):
    """Small colored circle indicating online/offline state."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._online: bool = True
        self.setFixedSize(20, 20)

    def set_online(self, online: bool) -> None:
        if self._online != online:
            self._online = online
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        color = _STATUS_ONLINE if self._online else _STATUS_OFFLINE

        # Subtle glow
        glow = QtGui.QColor(color)
        glow.setAlpha(50)
        cx, cy = self.width() / 2, self.height() / 2
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(glow))
        painter.drawEllipse(QtCore.QPointF(cx, cy), 8, 8)

        # Solid dot
        painter.setBrush(QtGui.QBrush(color))
        painter.drawEllipse(QtCore.QPointF(cx, cy), 5, 5)

        # Highlight
        highlight = QtGui.QColor(255, 255, 255, 60)
        painter.setBrush(QtGui.QBrush(highlight))
        painter.drawEllipse(QtCore.QPointF(cx - 1.5, cy - 1.5), 2, 2)

        painter.end()


# ---------------------------------------------------------------------------
# VU-meter widget  (green -> yellow -> red)
# ---------------------------------------------------------------------------

_COLOR_GREEN = QtGui.QColor(56, 142, 60)
_COLOR_GREEN_HOVER = QtGui.QColor(100, 190, 110)
_COLOR_YELLOW = QtGui.QColor(255, 193, 7)
_COLOR_RED = QtGui.QColor(244, 67, 54)
_COLOR_BG = QtGui.QColor(30, 30, 30)


def _vu_color(ratio: float, hovered: bool = False) -> QtGui.QColor:
    green = _COLOR_GREEN_HOVER if hovered else _COLOR_GREEN
    if ratio < 0.6:
        return green
    if ratio < 0.85:
        t = (ratio - 0.6) / 0.25
        r = int(green.red() + t * (_COLOR_YELLOW.red() - green.red()))
        g = int(green.green() + t * (_COLOR_YELLOW.green() - green.green()))
        b = int(green.blue() + t * (_COLOR_YELLOW.blue() - green.blue()))
        return QtGui.QColor(r, g, b)
    t = (ratio - 0.85) / 0.15
    t = min(1.0, t)
    r = int(_COLOR_YELLOW.red() + t * (_COLOR_RED.red() - _COLOR_YELLOW.red()))
    g = int(_COLOR_YELLOW.green() + t * (_COLOR_RED.green() - _COLOR_YELLOW.green()))
    b = int(_COLOR_YELLOW.blue() + t * (_COLOR_RED.blue() - _COLOR_YELLOW.blue()))
    return QtGui.QColor(r, g, b)


class VuMeter(QtWidgets.QWidget):
    """Compact horizontal VU-meter with green/yellow/red gradient."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._ratio: float = 0.0
        self._hovered: bool = False
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setMinimumHeight(10)
        self.setMaximumHeight(16)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def set_level(self, dbfs: float) -> None:
        """Set level in dBFS (range -60 .. 0)."""
        dbfs = max(-60.0, min(0.0, float(dbfs)))
        ratio = (dbfs + 60.0) / 60.0
        if abs(ratio - self._ratio) > 0.005:
            self._ratio = ratio
            self.update()

    def set_ratio(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, float(ratio)))
        if abs(ratio - self._ratio) > 0.005:
            self._ratio = ratio
            self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(QtCore.Qt.PenStyle.NoPen)

        # background
        p.setBrush(QtGui.QBrush(_COLOR_BG))
        p.drawRoundedRect(rect, 3, 3)

        # filled portion
        if self._ratio > 0.0:
            fill_w = int(rect.width() * self._ratio)
            fill_rect = QtCore.QRect(rect.x(), rect.y(), fill_w, rect.height())
            color = _vu_color(self._ratio, self._hovered)
            p.setBrush(QtGui.QBrush(color))
            p.drawRoundedRect(fill_rect, 3, 3)

        p.end()
