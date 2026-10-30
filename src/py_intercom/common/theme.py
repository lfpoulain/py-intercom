"""Visual identity shared by the server and client windows.

A dark broadcast-console look: near-black surfaces, one teal accent, one
colour per bus (see ``buses.py``) and monospaced read-outs. Colours live in
``TOKENS`` so painted widgets (``console.py``) and the style sheet agree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

TOKENS = {
    "bg": "#090c11",
    "panel": "#10141b",
    "panel_alt": "#151a23",
    "raised": "#1b212c",
    "border": "#232a36",
    "border_strong": "#323b4b",
    "text": "#e8edf4",
    "muted": "#8a96a8",
    "faint": "#556074",
    "accent": "#2dd4bf",
    "accent_hover": "#5eead4",
    "accent_text": "#04130f",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "danger": "#ef4444",
    "danger_hover": "#dc2626",
}

_SEMANTIC_STATUS_COLORS = {
    "online": TOKENS["success"],
    "healthy": TOKENS["success"],
    "idle": TOKENS["accent"],
    "connecting": TOKENS["accent"],
    "degraded": TOKENS["warning"],
    "error": TOKENS["danger"],
    "offline": TOKENS["faint"],
    "unknown": TOKENS["faint"],
}

MONO_FONT = "Cascadia Mono"

_UI_FONTS_REGISTERED = False


def _ensure_ui_fonts(app: QtWidgets.QApplication) -> str:
    """Return a usable UI family, including under Qt's offscreen backend.

    On Windows the offscreen platform plugin may expose an empty font
    database; registering the Segoe UI files keeps screenshots and tests
    representative instead of rendering tofu squares.
    """

    global _UI_FONTS_REGISTERED, MONO_FONT
    families = set(QtGui.QFontDatabase.families())
    if not families and sys.platform == "win32" and not _UI_FONTS_REGISTERED:
        fonts_dir = Path(os.environ.get("WINDIR") or "C:/Windows") / "Fonts"
        for filename in (
            "segoeui.ttf",
            "seguisb.ttf",
            "segoeuib.ttf",
            "seguisym.ttf",
            "seguiemj.ttf",
            "CascadiaMono.ttf",
            "consola.ttf",
            "consolab.ttf",
        ):
            font_path = fonts_dir / filename
            if font_path.is_file():
                QtGui.QFontDatabase.addApplicationFont(str(font_path))
        families = set(QtGui.QFontDatabase.families())
        _UI_FONTS_REGISTERED = True

    if "Cascadia Mono" not in families and "Consolas" in families:
        MONO_FONT = "Consolas"
    if "Segoe UI" in families:
        family = "Segoe UI"
    elif "Noto Sans" in families:
        family = "Noto Sans"
    else:
        family = str(app.font().family() or "Sans Serif")
    app.setFont(QtGui.QFont(family, 10))
    return family


def mono_font(pixel_size: int = 12, *, bold: bool = False) -> QtGui.QFont:
    font = QtGui.QFont(MONO_FONT)
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setPixelSize(int(pixel_size))
    font.setBold(bold)
    return font


def set_status_label(
    label: QtWidgets.QLabel,
    text: str,
    state: str,
    *,
    detail: str = "",
) -> None:
    """Apply a semantic, accessible status to a label.

    The visible text stays the primary signal; colour is only a secondary
    cue so the state reads at a glance even with impaired colour perception.
    """

    semantic_state = str(state or "unknown").lower()
    if semantic_state not in _SEMANTIC_STATUS_COLORS:
        semantic_state = "unknown"
    visible_text = str(text or "")
    previous_state = str(label.property("status") or "")
    changed = previous_state != semantic_state or label.text() != visible_text
    label.setText(visible_text)
    label.setProperty("status", semantic_state)
    label.setAccessibleName(f"État : {visible_text}")
    label.setAccessibleDescription(str(detail or visible_text))
    label.setToolTip(str(detail or visible_text))

    # Dynamic Qt properties do not automatically trigger a style refresh.
    if changed:
        repolish(label)


def repolish(widget: QtWidgets.QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


class ConsoleStyle(QtWidgets.QProxyStyle):
    """Fusion with switch-style check boxes (no image assets needed)."""

    _SWITCH_W = 30
    _SWITCH_H = 16

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802
        if metric == QtWidgets.QStyle.PixelMetric.PM_IndicatorWidth:
            return self._SWITCH_W
        if metric == QtWidgets.QStyle.PixelMetric.PM_IndicatorHeight:
            return self._SWITCH_H
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element != QtWidgets.QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            super().drawPrimitive(element, option, painter, widget)
            return
        if option.rect.width() < 8:
            return
        state = option.state
        on = bool(state & QtWidgets.QStyle.StateFlag.State_On)
        enabled = bool(state & QtWidgets.QStyle.StateFlag.State_Enabled)
        hover = bool(state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        rect = QtCore.QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
        h = min(rect.height(), float(self._SWITCH_H))
        track = QtCore.QRectF(rect.x(), rect.center().y() - h / 2, min(rect.width(), float(self._SWITCH_W)), h)

        if on:
            fill = QtGui.QColor(TOKENS["accent"] if enabled else TOKENS["border_strong"])
            border = fill
            knob = QtGui.QColor(TOKENS["accent_text"] if enabled else TOKENS["muted"])
        else:
            fill = QtGui.QColor(TOKENS["bg"])
            border = QtGui.QColor(TOKENS["muted"] if hover and enabled else TOKENS["border_strong"])
            knob = QtGui.QColor(TOKENS["muted"] if enabled else TOKENS["faint"])

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(track, h / 2, h / 2)
        radius = h / 2 - 3
        cx = track.right() - h / 2 if on else track.left() + h / 2
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(knob)
        painter.drawEllipse(QtCore.QPointF(cx, track.center().y()), radius, radius)
        painter.restore()


def apply_theme(app: QtWidgets.QApplication) -> None:
    """Apply the project-native Qt theme (no external theme engine)."""
    ui_font_family = _ensure_ui_fonts(app)
    t = TOKENS

    app.setStyle(ConsoleStyle("Fusion"))

    pal = QtGui.QPalette()
    roles = QtGui.QPalette.ColorRole
    for role, color in (
        (roles.Window, t["bg"]),
        (roles.WindowText, t["text"]),
        (roles.Base, t["bg"]),
        (roles.AlternateBase, t["panel_alt"]),
        (roles.ToolTipBase, t["raised"]),
        (roles.ToolTipText, t["text"]),
        (roles.Text, t["text"]),
        (roles.Button, t["raised"]),
        (roles.ButtonText, t["text"]),
        (roles.BrightText, "#ffffff"),
        (roles.Highlight, t["accent"]),
        (roles.HighlightedText, t["accent_text"]),
        (roles.PlaceholderText, t["faint"]),
    ):
        pal.setColor(role, QtGui.QColor(color))
    for role in (roles.Text, roles.ButtonText, roles.WindowText):
        pal.setColor(QtGui.QPalette.ColorGroup.Disabled, role, QtGui.QColor(t["faint"]))
    app.setPalette(pal)
    app.setStyleSheet(_style_sheet(ui_font_family))


def _style_sheet(ui_font_family: str) -> str:
    t = TOKENS
    return f"""
        QWidget {{
            font-family: "{ui_font_family}", "Segoe UI Symbol";
            font-size: 13px;
            color: {t["text"]};
        }}
        QMainWindow, QDialog {{
            background: {t["bg"]};
        }}
        QToolTip {{
            border: 1px solid {t["border_strong"]};
            padding: 6px 9px;
            color: {t["text"]};
            background: {t["raised"]};
            border-radius: 6px;
        }}

        /* --- Structure --- */
        QFrame[role="topbar"] {{
            background: {t["panel"]};
            border: none;
            border-bottom: 1px solid {t["border"]};
        }}
        QFrame[role="panel"] {{
            background: {t["panel"]};
            border: 1px solid {t["border"]};
            border-radius: 12px;
        }}
        QFrame[role="card"] {{
            background: {t["panel"]};
            border: 1px solid {t["border"]};
            border-radius: 12px;
        }}
        QFrame[role="strip"] {{
            background: {t["panel_alt"]};
            border: 1px solid {t["border"]};
            border-radius: 10px;
        }}
        QFrame[role="strip"][selected="true"] {{
            border: 1px solid {t["accent"]};
            background: {t["raised"]};
        }}
        QFrame[role="strip"][offline="true"] QLabel {{
            color: {t["faint"]};
        }}
        QScrollArea, QScrollArea > QWidget > QWidget {{
            background: transparent;
            border: none;
        }}
        QStackedWidget {{
            background: transparent;
        }}

        /* --- Typography --- */
        QLabel[role="brand"] {{
            color: {t["accent"]};
            font-size: 11px;
            font-weight: 700;
        }}
        QLabel[role="title"] {{
            color: {t["text"]};
            font-size: 18px;
            font-weight: 700;
        }}
        QLabel[role="subtitle"] {{
            color: {t["muted"]};
            font-size: 12px;
        }}
        QLabel[role="sectionTitle"] {{
            color: {t["muted"]};
            font-size: 11px;
            font-weight: 700;
        }}
        QLabel[role="cardTitle"] {{
            color: {t["text"]};
            font-size: 14px;
            font-weight: 700;
        }}
        QLabel[role="fieldLabel"] {{
            color: {t["muted"]};
            font-size: 11px;
            font-weight: 600;
        }}
        QLabel[role="muted"] {{
            color: {t["muted"]};
        }}
        QLabel[role="hint"] {{
            color: {t["faint"]};
            font-size: 12px;
        }}
        QLabel[role="stripName"] {{
            color: {t["text"]};
            font-size: 12px;
            font-weight: 700;
        }}
        QLabel[role="stripCaption"] {{
            color: {t["faint"]};
            font-size: 10px;
        }}
        QLabel[role="readout"] {{
            font-family: "{MONO_FONT}", "Consolas";
            font-size: 11px;
            color: {t["muted"]};
            background: {t["bg"]};
            border: 1px solid {t["border"]};
            border-radius: 5px;
            padding: 2px 4px;
        }}
        QLabel[role="emptyState"] {{
            color: {t["faint"]};
            font-size: 13px;
        }}

        /* --- Inputs --- */
        QLineEdit, QSpinBox, QComboBox {{
            border: 1px solid {t["border"]};
            background: {t["bg"]};
            padding: 4px 9px;
            min-height: 26px;
            border-radius: 7px;
            selection-background-color: {t["accent"]};
            selection-color: {t["accent_text"]};
        }}
        QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{
            border: 1px solid {t["border_strong"]};
        }}
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
            border: 1px solid {t["accent"]};
        }}
        QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled,
        QLineEdit:read-only {{
            color: {t["muted"]};
            background: {t["panel_alt"]};
        }}
        QComboBox::drop-down {{
            border: none;
            width: 20px;
        }}
        QComboBox QAbstractItemView {{
            color: {t["text"]};
            background: {t["raised"]};
            selection-background-color: {t["accent"]};
            selection-color: {t["accent_text"]};
            outline: none;
            border: 1px solid {t["border_strong"]};
            padding: 4px;
        }}
        QComboBox[role="compact"] {{
            min-height: 20px;
            padding: 2px 6px;
            font-size: 11px;
            border-radius: 6px;
        }}
        QSpinBox::up-button, QSpinBox::down-button {{
            width: 0;
            border: none;
        }}

        /* --- Buttons --- */
        QPushButton {{
            border: 1px solid {t["border_strong"]};
            background: {t["raised"]};
            padding: 5px 16px;
            min-height: 26px;
            border-radius: 8px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {t["border_strong"]};
            border-color: {t["muted"]};
        }}
        QPushButton:pressed {{
            background: {t["border"]};
        }}
        QPushButton[class="primary"], QPushButton[class="success"] {{
            border: 1px solid {t["accent"]};
            background: {t["accent"]};
            color: {t["accent_text"]};
        }}
        QPushButton[class="primary"]:hover, QPushButton[class="success"]:hover {{
            background: {t["accent_hover"]};
            border-color: {t["accent_hover"]};
        }}
        QPushButton[class="danger"] {{
            border: 1px solid {t["danger"]};
            background: {t["danger"]};
            color: #ffffff;
        }}
        QPushButton[class="danger"]:hover {{
            background: {t["danger_hover"]};
            border-color: {t["danger_hover"]};
        }}
        QPushButton[class="secondary"] {{
            background: transparent;
            border-color: {t["border_strong"]};
        }}
        QPushButton[class="secondary"]:hover {{
            background: {t["raised"]};
            border-color: {t["accent"]};
        }}
        QPushButton:disabled {{
            border: 1px solid {t["border"]};
            color: {t["faint"]};
            background: {t["panel_alt"]};
        }}
        QPushButton[role="segment"] {{
            background: transparent;
            border: 1px solid transparent;
            border-radius: 7px;
            padding: 4px 14px;
            color: {t["muted"]};
        }}
        QPushButton[role="segment"]:hover {{
            color: {t["text"]};
        }}
        QPushButton[role="segment"]:checked {{
            background: {t["raised"]};
            border-color: {t["border_strong"]};
            color: {t["text"]};
        }}
        QFrame[role="segmentGroup"] {{
            background: {t["bg"]};
            border: 1px solid {t["border"]};
            border-radius: 9px;
        }}
        QToolButton {{
            border: 1px solid {t["border"]};
            background: {t["raised"]};
            padding: 4px 8px;
            border-radius: 7px;
        }}
        QToolButton:hover {{
            background: {t["border_strong"]};
        }}
        QToolButton[class="ghost"] {{
            font-size: 16px;
            background: transparent;
            border-color: transparent;
            color: {t["muted"]};
        }}
        QToolButton[class="ghost"]:hover {{
            background: {t["raised"]};
            border-color: {t["border"]};
            color: {t["text"]};
        }}
        QToolButton:disabled {{
            color: {t["faint"]};
        }}

        /* --- Check boxes: switches, or chips on strips --- */
        QCheckBox {{
            spacing: 9px;
            color: {t["muted"]};
        }}
        QCheckBox:hover {{
            color: {t["text"]};
        }}
        QCheckBox:checked {{
            color: {t["text"]};
        }}

        /* --- Scroll bars --- */
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 3px;
        }}
        QScrollBar:horizontal {{
            background: transparent;
            height: 10px;
            margin: 3px;
        }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: {t["border_strong"]};
            border-radius: 2px;
            min-height: 24px;
            min-width: 24px;
        }}
        QScrollBar::handle:hover {{
            background: {t["faint"]};
        }}
        QScrollBar::add-line, QScrollBar::sub-line {{
            height: 0;
            width: 0;
        }}
        QScrollBar::add-page, QScrollBar::sub-page {{
            background: transparent;
        }}

        /* --- Status --- */
        QStatusBar {{
            background: {t["panel"]};
            color: {t["muted"]};
            border-top: 1px solid {t["border"]};
            font-size: 12px;
        }}
        QStatusBar::item {{
            border: none;
        }}
        QStatusBar QLabel {{
            padding: 0 6px;
        }}
        QLabel[status="online"], QLabel[status="healthy"] {{
            color: {t["success"]};
            font-weight: 600;
        }}
        QLabel[status="connecting"], QLabel[status="idle"] {{
            color: {t["accent"]};
            font-weight: 600;
        }}
        QLabel[status="degraded"] {{
            color: {t["warning"]};
            font-weight: 600;
        }}
        QLabel[status="error"] {{
            color: {t["danger"]};
            font-weight: 600;
        }}
        QLabel[status="offline"], QLabel[status="unknown"] {{
            color: {t["muted"]};
            font-weight: 500;
        }}
        QLabel[role="statusBanner"] {{
            background: {t["bg"]};
            border: 1px solid {t["border_strong"]};
            border-radius: 15px;
            min-height: 20px;
            max-height: 20px;
            padding: 4px 16px;
            font-weight: 700;
        }}
        QLabel[role="statusBanner"][status="online"],
        QLabel[role="statusBanner"][status="healthy"] {{
            border-color: {t["success"]};
            background: #0c2117;
        }}
        QLabel[role="statusBanner"][status="connecting"] {{
            border-color: {t["accent"]};
            background: #08201e;
        }}
        QLabel[role="statusBanner"][status="degraded"] {{
            border-color: {t["warning"]};
            background: #251a06;
        }}
        QLabel[role="statusBanner"][status="error"] {{
            border-color: {t["danger"]};
            background: #2a0d0d;
        }}

        /* --- Splitter --- */
        QSplitter::handle {{
            background: transparent;
        }}
        QSplitter::handle:hover {{
            background: {t["border"]};
        }}
    """


def make_field(label_text: str, control: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """Build a consistently spaced label/control field for desktop forms."""

    container = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    label = QtWidgets.QLabel(str(label_text))
    label.setProperty("role", "fieldLabel")
    label.setBuddy(control)
    layout.addWidget(label)
    layout.addWidget(control)
    return container


class _CompactItemDelegate(QtWidgets.QStyledItemDelegate):
    """Forces a fixed row height in combo-box popup lists."""

    def __init__(self, height: int = 24, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._height = height

    def sizeHint(self, option, index):  # noqa: N802
        s = super().sizeHint(option, index)
        s.setHeight(self._height)
        return s


def patch_combo(combo: QtWidgets.QComboBox, item_height: int = 24) -> None:
    """Apply compact item height to a QComboBox popup."""
    combo.setItemDelegate(_CompactItemDelegate(item_height, combo))


class StatusIndicator(QtWidgets.QWidget):
    """Small semantic status dot with an accessible textual description."""

    def __init__(self, parent: QtWidgets.QWidget | None = None, *, size: int = 18) -> None:
        super().__init__(parent)
        self._state: str = "unknown"
        self.setFixedSize(size, size)
        self.setAccessibleName("État inconnu")
        self.setToolTip("État inconnu")

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str, *, description: str = "") -> None:
        semantic_state = str(state or "unknown").lower()
        if semantic_state not in _SEMANTIC_STATUS_COLORS:
            semantic_state = "unknown"
        if description:
            self.setAccessibleName(str(description))
            self.setAccessibleDescription(str(description))
            self.setToolTip(str(description))
        if self._state != semantic_state:
            self._state = semantic_state
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        color = QtGui.QColor(_SEMANTIC_STATUS_COLORS.get(self._state, TOKENS["faint"]))
        cx, cy = self.width() / 2, self.height() / 2
        r = min(cx, cy)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        if self._state not in ("offline", "unknown"):
            glow = QtGui.QColor(color)
            glow.setAlpha(55)
            painter.setBrush(glow)
            painter.drawEllipse(QtCore.QPointF(cx, cy), r - 1, r - 1)
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QPointF(cx, cy), r * 0.45, r * 0.45)
        painter.end()
