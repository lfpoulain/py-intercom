from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

def apply_theme(app: QtWidgets.QApplication) -> None:
    """Apply project-native Qt theme (no external theme engine)."""
    bg = "#12171f"
    surface = "#1b2330"
    surface_alt = "#222c3b"
    border = "#344256"
    text = "#e8edf4"
    muted = "#aab4c3"
    accent = "#21b0a6"
    accent_hover = "#2cc7bc"
    accent_text = "#0a1317"
    warning = "#f2b632"
    danger = "#e25d5d"
    success = "#2eb67d"
    disabled_fg = "#6f7b8e"

    app.setStyle("Fusion")

    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(bg))
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(text))
    pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(surface))
    pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(surface_alt))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(surface_alt))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(text))
    pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(text))
    pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(surface_alt))
    pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(text))
    pal.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor("#ffffff"))
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(accent))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(accent_text))
    pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(muted))
    pal.setColor(
        QtGui.QPalette.ColorGroup.Disabled,
        QtGui.QPalette.ColorRole.Text,
        QtGui.QColor(disabled_fg),
    )
    pal.setColor(
        QtGui.QPalette.ColorGroup.Disabled,
        QtGui.QPalette.ColorRole.ButtonText,
        QtGui.QColor(disabled_fg),
    )
    pal.setColor(
        QtGui.QPalette.ColorGroup.Disabled,
        QtGui.QPalette.ColorRole.WindowText,
        QtGui.QColor(disabled_fg),
    )
    app.setPalette(pal)

    patch = f"""
        QWidget {{
            font-family: "Segoe UI", "Noto Sans", sans-serif;
            font-size: 13px;
            color: {text};
        }}
        QToolTip {{
            border: 1px solid {border};
            padding: 4px 8px;
            color: {text};
            background: {surface_alt};
        }}
        QLineEdit {{
            border: 1px solid {border};
            background: {surface};
            padding: 3px 6px;
            min-height: 24px;
            border-radius: 3px;
        }}
        QLineEdit:focus {{
            border: 1px solid {accent};
        }}
        QLineEdit:disabled {{
            color: {disabled_fg};
            background: {surface_alt};
        }}
        QComboBox {{
            border: 1px solid {border};
            background: {surface_alt};
            padding: 3px 6px;
            min-height: 24px;
            border-radius: 3px;
        }}
        QComboBox::drop-down {{
            border: none;
            width: 20px;
        }}
        QComboBox:hover {{
            border: 1px solid {accent_hover};
        }}
        QComboBox:focus {{
            border: 1px solid {accent};
        }}
        QComboBox QAbstractItemView {{
            color: {text};
            background: {surface};
            selection-background-color: {accent};
            selection-color: {accent_text};
            outline: none;
        }}
        QPushButton {{
            border: 1px solid {border};
            background: {surface_alt};
            padding: 5px 16px;
            min-height: 28px;
            border-radius: 4px;
            font-weight: 500;
        }}
        QToolButton {{
            border: 1px solid {border};
            background: {surface_alt};
            padding: 3px 8px;
            border-radius: 4px;
        }}
        QPushButton:hover {{
            background-color: {accent};
            color: {accent_text};
            border: 1px solid {accent_hover};
        }}
        QToolButton:hover {{
            background-color: {accent};
            color: {accent_text};
            border: 1px solid {accent_hover};
        }}
        QPushButton[class="warning"] {{
            border: 1px solid {warning};
            background-color: {warning};
            color: #111111;
            font-weight: 600;
        }}
        QPushButton[class="warning"]:hover {{
            background-color: #e0a800;
            border: 1px solid #e0a800;
            color: #111111;
        }}
        QToolButton[class="warning"] {{
            border: 1px solid {warning};
            background-color: {warning};
            color: #111111;
        }}
        QToolButton[class="warning"]:hover {{
            background-color: #e0a800;
            border: 1px solid #e0a800;
            color: #111111;
        }}
        QPushButton[class="success"] {{
            border: 1px solid {success};
            background-color: {success};
            color: #ffffff;
            font-weight: 600;
        }}
        QPushButton[class="success"]:hover {{
            background-color: #25a06e;
            border: 1px solid #25a06e;
            color: #ffffff;
        }}
        QToolButton[class="success"] {{
            border: 1px solid {success};
            background-color: {success};
            color: #ffffff;
        }}
        QToolButton[class="success"]:hover {{
            background-color: #25a06e;
            border: 1px solid #25a06e;
            color: #ffffff;
        }}
        QPushButton[class="danger"] {{
            border: 1px solid {danger};
            background-color: {danger};
            color: #ffffff;
            font-weight: 600;
        }}
        QPushButton[class="danger"]:hover {{
            background-color: #c94040;
            border: 1px solid #c94040;
            color: #ffffff;
        }}
        QToolButton[class="danger"] {{
            border: 1px solid {danger};
            background-color: {danger};
            color: #ffffff;
        }}
        QToolButton[class="danger"]:hover {{
            background-color: #c94040;
            border: 1px solid #c94040;
            color: #ffffff;
        }}
        QPushButton:disabled, QToolButton:disabled {{
            border: 1px solid {border};
            color: {disabled_fg};
            background-color: {surface};
            font-weight: normal;
        }}
        QGroupBox {{
            border: 1px solid {border};
            border-radius: 6px;
            padding-top: 18px;
            margin-top: 8px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            background-color: {bg};
            padding: 0 8px;
            font-weight: 600;
            color: {accent_hover};
        }}

        /* --- Slider --- */
        QSlider::groove:horizontal {{
            height: 6px;
            background: {surface_alt};
            border-radius: 3px;
        }}
        QSlider::handle:horizontal {{
            background: {accent};
            border: 1px solid {accent_hover};
            width: 14px;
            height: 14px;
            margin: -5px 0;
            border-radius: 7px;
        }}
        QSlider::handle:horizontal:hover {{
            background: {accent_hover};
        }}
        QSlider::sub-page:horizontal {{
            background: {accent};
            border-radius: 3px;
        }}

        /* --- Table --- */
        QTableWidget {{
            border: 1px solid {border};
            gridline-color: {border};
            alternate-background-color: {surface_alt};
        }}
        QHeaderView::section {{
            background-color: {surface_alt};
            color: {accent_hover};
            border: none;
            border-bottom: 2px solid {accent};
            padding: 4px 6px;
            font-weight: bold;
        }}

        /* --- Status bar --- */
        QStatusBar {{
            background: {surface};
            color: {muted};
            border-top: 1px solid {border};
            font-size: 12px;
        }}
        QStatusBar QLabel {{
            padding: 0 6px;
        }}

        /* --- Splitter --- */
        QSplitter::handle {{
            background: {border};
        }}
        QSplitter::handle:horizontal {{
            width: 2px;
        }}
        QSplitter::handle:vertical {{
            height: 2px;
        }}
    """
    app.setStyleSheet(patch)


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

_STATUS_ONLINE = QtGui.QColor(76, 175, 80)
_STATUS_OFFLINE = QtGui.QColor(198, 40, 40)


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
