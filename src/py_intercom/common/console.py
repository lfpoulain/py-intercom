"""Console widgets: LED meters, faders, talk pads and channel strips.

Everything here is painted with ``TOKENS`` from ``theme.py`` so both windows
share one look. The widgets carry no intercom logic; the windows connect
their signals to the engine exactly as they did with stock Qt widgets
(``Fader`` is a ``QSlider``, ``TalkPad`` a ``QAbstractButton``).
"""

from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from .theme import TOKENS, mono_font, repolish

METER_FLOOR_DB = -60.0
_METER_YELLOW_DB = -18.0
_METER_RED_DB = -6.0
_PEAK_HOLD_S = 1.2
_PEAK_FALL_DB = 1.5  # per update once the hold has expired

_GREEN = QtGui.QColor("#22c55e")
_YELLOW = QtGui.QColor("#f5c518")
_RED = QtGui.QColor("#ef4444")


def _qcolor(color: str | QtGui.QColor, alpha: int | None = None) -> QtGui.QColor:
    c = QtGui.QColor(color)
    if alpha is not None:
        c.setAlpha(int(alpha))
    return c


def _segment_color(db: float) -> QtGui.QColor:
    if db >= _METER_RED_DB:
        return _RED
    if db >= _METER_YELLOW_DB:
        return _YELLOW
    return _GREEN


class _FocusWheelGuard(QtCore.QObject):
    """Ignores the wheel over an unfocused control so its container scrolls.

    A wheel notch while merely hovering a fader or a device combo must never
    change a live gain or reroute an output; the ignored event propagates to
    the parent (the strip rack or a page) instead.
    """

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        if event.type() == QtCore.QEvent.Type.Wheel and not watched.hasFocus():
            event.ignore()
            return True
        return False


def require_focus_for_wheel(widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """Only let the wheel act on ``widget`` once it has the keyboard focus."""
    widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
    widget.installEventFilter(_FocusWheelGuard(widget))
    return widget


class LevelMeter(QtWidgets.QWidget):
    """Segmented LED meter (-60 .. 0 dBFS) with peak hold.

    A dimmed meter still moves but in grey: the level is real, nothing is
    being sent (a microphone measured while no talk key is held).
    """

    def __init__(
        self,
        orientation: QtCore.Qt.Orientation = QtCore.Qt.Orientation.Vertical,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._orientation = orientation
        self._level = METER_FLOOR_DB
        self._peak = METER_FLOOR_DB
        self._peak_at = 0.0
        self._dimmed = False
        if orientation == QtCore.Qt.Orientation.Vertical:
            self.setMinimumSize(8, 60)
            self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
            self.setFixedWidth(10)
        else:
            self.setMinimumSize(60, 8)
            self.setMaximumHeight(12)
            self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def level(self) -> float:
        return self._level

    def set_dimmed(self, dimmed: bool) -> None:
        if bool(dimmed) != self._dimmed:
            self._dimmed = bool(dimmed)
            self.update()

    def set_level(self, dbfs: float) -> None:
        """Set the level in dBFS (clamped to -60 .. 0)."""
        level = max(METER_FLOOR_DB, min(0.0, float(dbfs)))
        now = time.monotonic()
        peak = self._peak
        if level >= peak:
            peak, self._peak_at = level, now
        elif now - self._peak_at > _PEAK_HOLD_S:
            peak = max(level, peak - _PEAK_FALL_DB)
        if abs(level - self._level) > 0.3 or abs(peak - self._peak) > 0.3:
            self._level, self._peak = level, peak
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        vertical = self._orientation == QtCore.Qt.Orientation.Vertical
        length = self.height() if vertical else self.width()
        thickness = self.width() if vertical else self.height()
        seg, gap = 3, 2
        count = max(4, (length + gap) // (seg + gap))
        used = count * (seg + gap) - gap
        offset = (length - used) // 2
        enabled = self.isEnabled()
        span = -METER_FLOOR_DB
        lit_upto = (self._level - METER_FLOOR_DB) / span * count
        peak_index = int((self._peak - METER_FLOOR_DB) / span * count) - 1
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        for i in range(count):
            db = METER_FLOOR_DB + (i + 0.5) / count * span
            color = QtGui.QColor(TOKENS["muted"] if self._dimmed else _segment_color(db))
            lit = i < lit_upto or (i == peak_index and self._peak > METER_FLOOR_DB + 1)
            color.setAlpha(255 if lit and enabled else 34)
            pos = offset + i * (seg + gap)
            if vertical:
                rect = QtCore.QRect(0, length - pos - seg, thickness, seg)
            else:
                rect = QtCore.QRect(pos, 0, seg, thickness)
            p.fillRect(rect, color)
        p.end()


class Fader(QtWidgets.QSlider):
    """Vertical console fader with a dB scale; a double-click recalls 0 dB."""

    _CAP_W = 28
    _CAP_H = 15
    _SCALE_W = 22

    def __init__(self, minimum: int = -60, maximum: int = 20, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(QtCore.Qt.Orientation.Vertical, parent)
        self.setRange(int(minimum), int(maximum))
        self.setSingleStep(1)
        self.setPageStep(6)
        self._color = QtGui.QColor(TOKENS["accent"])
        self._drag_offset: float | None = None
        self.setMinimumHeight(90)
        self.setFixedWidth(self._SCALE_W + self._CAP_W + 6)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Glisser pour régler, double-clic : 0 dB")
        require_focus_for_wheel(self)

    def set_color(self, color: str | QtGui.QColor) -> None:
        new = QtGui.QColor(color)
        if new != self._color:
            self._color = new
            self.update()

    # -- geometry ------------------------------------------------------------

    def _travel(self) -> tuple[float, float]:
        top = self._CAP_H / 2 + 2
        return top, self.height() - self._CAP_H / 2 - 2

    def _y_for_value(self, value: float) -> float:
        top, bottom = self._travel()
        span = max(1, self.maximum() - self.minimum())
        return bottom - (float(value) - self.minimum()) / span * (bottom - top)

    def _value_for_y(self, y: float) -> int:
        top, bottom = self._travel()
        ratio = (bottom - y) / max(1.0, bottom - top)
        return round(self.minimum() + ratio * (self.maximum() - self.minimum()))

    # -- interaction ---------------------------------------------------------

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() != QtCore.Qt.MouseButton.LeftButton or not self.isEnabled():
            event.ignore()
            return
        y = event.position().y()
        cap_y = self._y_for_value(self.value())
        self._drag_offset = y - cap_y if abs(y - cap_y) <= self._CAP_H else 0.0
        self.setSliderDown(True)
        self.setValue(self._value_for_y(y - self._drag_offset))
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is None:
            event.ignore()
            return
        self.setValue(self._value_for_y(event.position().y() - self._drag_offset))
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            self.setSliderDown(False)
        event.accept()

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if self.isEnabled() and self.minimum() <= 0 <= self.maximum():
            self.setValue(0)
        event.accept()

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        groove_x = self._SCALE_W + 3 + self._CAP_W / 2
        top, bottom = self._travel()

        # scale
        p.setFont(mono_font(9))
        for db in range(self.minimum() - self.minimum() % 10, self.maximum() + 1, 10):
            if db < self.minimum():
                continue
            y = self._y_for_value(db)
            strong = db == 0
            p.setPen(_qcolor(TOKENS["muted"] if strong else TOKENS["faint"], 255 if enabled else 120))
            label = f"+{db}" if db > 0 else str(db)
            p.drawText(
                QtCore.QRectF(0, y - 6, self._SCALE_W - 3, 12),
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            p.drawLine(QtCore.QPointF(groove_x - 9, y), QtCore.QPointF(groove_x - (13 if strong else 11), y))
            p.drawLine(QtCore.QPointF(groove_x + 9, y), QtCore.QPointF(groove_x + (13 if strong else 11), y))

        # slot and level fill
        slot = QtCore.QRectF(groove_x - 2, top, 4, bottom - top)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(_qcolor("#05070a"))
        p.drawRoundedRect(slot, 2, 2)
        cap_y = self._y_for_value(self.value())
        fill = QtCore.QRectF(groove_x - 2, cap_y, 4, bottom - cap_y)
        p.setBrush(_qcolor(self._color, 200 if enabled else 60))
        p.drawRoundedRect(fill, 2, 2)

        # cap
        cap = QtCore.QRectF(groove_x - self._CAP_W / 2, cap_y - self._CAP_H / 2, self._CAP_W, self._CAP_H)
        grad = QtGui.QLinearGradient(cap.topLeft(), cap.bottomLeft())
        grad.setColorAt(0.0, _qcolor("#4a5568" if enabled else "#2a3140"))
        grad.setColorAt(0.5, _qcolor("#2d3542" if enabled else "#202632"))
        grad.setColorAt(1.0, _qcolor("#1a1f28"))
        p.setBrush(grad)
        p.setPen(QtGui.QPen(_qcolor("#0b0e13"), 1))
        p.drawRoundedRect(cap, 3, 3)
        p.setPen(QtGui.QPen(_qcolor(self._color if enabled else TOKENS["faint"]), 2))
        p.drawLine(QtCore.QPointF(cap.left() + 4, cap_y), QtCore.QPointF(cap.right() - 4, cap_y))
        p.end()


class ElidedLabel(QtWidgets.QLabel):
    """Single-line label that elides instead of growing its parent."""

    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setToolTip(text)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(10, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), QtCore.Qt.TextElideMode.ElideRight, rect.width())
        p.drawText(rect, int(self.alignment()), text)
        p.end()


class Lamp(QtWidgets.QWidget):
    """Small labelled lamp (e.g. a client talking on a bus)."""

    def __init__(self, text: str, color: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._color = QtGui.QColor(color)
        self._on = False
        self.setFixedSize(22, 16)

    def set_on(self, on: bool) -> None:
        if bool(on) != self._on:
            self._on = bool(on)
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._on:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(self._color)
            text_color = QtGui.QColor("#06080c")
        else:
            p.setPen(QtGui.QPen(_qcolor(self._color, 90), 1))
            p.setBrush(_qcolor(TOKENS["bg"]))
            text_color = _qcolor(self._color, 140)
        p.drawRoundedRect(rect, 4, 4)
        font = self.font()
        font.setPixelSize(10)
        font.setBold(True)
        p.setFont(font)
        p.setPen(text_color)
        p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, self._text)
        p.end()


class TalkPad(QtWidgets.QAbstractButton):
    """Large talk key: the bus name, its mode, its shortcut and a level bar.

    ``state`` is one of ``disarmed`` (offline), ``ready``, ``off`` (toggle
    released) or ``live`` (transmitting); only the window decides which.
    """

    def __init__(self, title: str, color: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._color = QtGui.QColor(color)
        self._mode_text = "PTT"
        self._key_text = ""
        self._state = "disarmed"
        self._state_text = "DÉSARMÉ"
        self._level = METER_FLOOR_DB
        self.setMinimumSize(150, 150)
        self.setMaximumHeight(400)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(f"Parler {title}")

    def hitButton(self, pos: QtCore.QPoint) -> bool:
        # A press lasts until the button is physically released: sliding off
        # the edge and back must not emit a second press (a toggle would flip
        # twice).
        return self.isDown() or super().hitButton(pos)

    @property
    def state(self) -> str:
        return self._state

    def set_title(self, title: str) -> None:
        self._title = title
        self.setAccessibleName(f"Parler {title}")
        self.update()

    def set_mode_text(self, text: str) -> None:
        self._mode_text = text
        self.update()

    def set_key_text(self, text: str) -> None:
        self._key_text = text
        self.update()

    def set_state(self, state: str, text: str, detail: str = "") -> None:
        if (state, text) != (self._state, self._state_text):
            self._state, self._state_text = state, text
            self.update()
        self.setToolTip(detail or text)
        self.setAccessibleDescription(f"{text}. {detail}".strip())

    def set_level(self, dbfs: float) -> None:
        level = max(METER_FLOOR_DB, min(0.0, float(dbfs)))
        if abs(level - self._level) > 0.5:
            self._level = level
            self.update()

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(190, 190)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self.isDown():
            rect.adjust(1, 1, -1, -1)
        live = self._state == "live"
        disarmed = self._state == "disarmed"
        hover = self.underMouse() and not disarmed
        color = self._color

        # glow behind a live pad
        if live:
            for spread, alpha in ((6, 26), (3, 50)):
                p.setPen(QtCore.Qt.PenStyle.NoPen)
                p.setBrush(_qcolor(color, alpha))
                p.drawRoundedRect(rect.adjusted(-spread + 2, -spread + 2, spread - 2, spread - 2), 18, 18)
            rect = rect.adjusted(2, 2, -2, -2)

        # body
        if live:
            grad = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
            grad.setColorAt(0.0, color.lighter(115))
            grad.setColorAt(1.0, color.darker(130))
            p.setBrush(grad)
            p.setPen(QtGui.QPen(color.lighter(140), 1.5))
        else:
            grad = QtGui.QLinearGradient(rect.topLeft(), rect.bottomLeft())
            grad.setColorAt(0.0, _qcolor("#1c222d" if not disarmed else "#141821"))
            grad.setColorAt(1.0, _qcolor("#12161d" if not disarmed else "#0f1218"))
            p.setBrush(grad)
            border = (
                _qcolor(color, 150) if hover else _qcolor(TOKENS["border_strong"] if not disarmed else TOKENS["border"])
            )
            p.setPen(QtGui.QPen(border, 1.2))
        p.drawRoundedRect(rect, 16, 16)

        # colour stripe on top of an idle pad
        if not live:
            stripe = QtCore.QRectF(rect.left() + 18, rect.top() + 1, rect.width() - 36, 3)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(_qcolor(color, 90 if disarmed else 230))
            p.drawRoundedRect(stripe, 1.5, 1.5)

        ink = QtGui.QColor("#06080c") if live else _qcolor(TOKENS["text"] if not disarmed else TOKENS["faint"])
        sub_ink = _qcolor("#06080c", 190) if live else _qcolor(TOKENS["muted"] if not disarmed else TOKENS["faint"])
        pad = 14.0

        # mode badge (top left)
        badge_font = self.font()
        badge_font.setPixelSize(10)
        badge_font.setBold(True)
        p.setFont(badge_font)
        fm = QtGui.QFontMetricsF(badge_font)
        badge_w = fm.horizontalAdvance(self._mode_text) + 14
        badge = QtCore.QRectF(rect.left() + pad, rect.top() + pad, badge_w, 18)
        p.setPen(QtGui.QPen(_qcolor(sub_ink, 120), 1))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(badge, 5, 5)
        p.setPen(sub_ink)
        p.drawText(badge, QtCore.Qt.AlignmentFlag.AlignCenter, self._mode_text)

        # key cap (top right)
        if self._key_text:
            key_font = mono_font(11, bold=True)
            p.setFont(key_font)
            kfm = QtGui.QFontMetricsF(key_font)
            key_w = max(24.0, kfm.horizontalAdvance(self._key_text) + 14)
            key = QtCore.QRectF(rect.right() - pad - key_w, rect.top() + pad - 1, key_w, 20)
            p.setPen(QtGui.QPen(_qcolor(sub_ink, 160), 1))
            p.setBrush(_qcolor("#000000", 40 if live else 90))
            p.drawRoundedRect(key, 5, 5)
            p.setPen(ink)
            p.drawText(key, QtCore.Qt.AlignmentFlag.AlignCenter, self._key_text)

        # title and state
        title_font = self.font()
        title_font.setPixelSize(max(18, min(30, int(rect.height() * 0.16))))
        title_font.setBold(True)
        p.setFont(title_font)
        p.setPen(ink)
        title_rect = QtCore.QRectF(
            rect.left() + pad,
            rect.center().y() - title_font.pixelSize() * 1.05,
            rect.width() - 2 * pad,
            title_font.pixelSize() * 1.4,
        )
        p.drawText(title_rect, QtCore.Qt.AlignmentFlag.AlignCenter, self._title)

        state_font = self.font()
        state_font.setPixelSize(12)
        state_font.setBold(True)
        state_font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 1.5)
        p.setFont(state_font)
        p.setPen(sub_ink if not (self._state == "ready") else _qcolor(color))
        state_rect = QtCore.QRectF(rect.left() + pad, title_rect.bottom(), rect.width() - 2 * pad, 20)
        p.drawText(state_rect, QtCore.Qt.AlignmentFlag.AlignCenter, self._state_text)

        # level bar (bottom)
        bar = QtCore.QRectF(rect.left() + pad, rect.bottom() - pad - 5, rect.width() - 2 * pad, 5)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(_qcolor("#000000", 70 if live else 120))
        p.drawRoundedRect(bar, 2.5, 2.5)
        ratio = (self._level - METER_FLOOR_DB) / -METER_FLOOR_DB
        if ratio > 0.01 and not disarmed:
            filled = QtCore.QRectF(bar.left(), bar.top(), bar.width() * ratio, bar.height())
            p.setBrush(_qcolor("#06080c", 200) if live else _qcolor(color))
            p.drawRoundedRect(filled, 2.5, 2.5)
        p.end()


class _ColorBar(QtWidgets.QWidget):
    def __init__(self, color: str | None, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QtGui.QColor(color) if color else None
        self.setFixedHeight(3)

    def set_color(self, color: str | None) -> None:
        new = QtGui.QColor(color) if color else None
        if new != self._color:
            self._color = new
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._color is None:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawRoundedRect(QtCore.QRectF(self.rect()), 1.5, 1.5)
        p.end()


class ChannelStrip(QtWidgets.QFrame):
    """Vertical mixer strip: colour bar, name, extras, meter + fader, read-out.

    ``readout`` is a label made by :func:`readout`, which carries its style.
    """

    clicked = QtCore.Signal()

    def __init__(
        self,
        title: str,
        *,
        color: str | None = None,
        caption: str = "",
        fader: QtWidgets.QSlider | None = None,
        meter: LevelMeter | None = None,
        readout: QtWidgets.QLabel | None = None,
        width: int = 104,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "strip")
        self.setFixedWidth(int(width))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Expanding)
        self.fader = fader

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(5)
        self._bar = _ColorBar(color)
        lay.addWidget(self._bar)

        self.header = QtWidgets.QHBoxLayout()
        self.header.setSpacing(4)
        self.name_label = ElidedLabel(title)
        self.name_label.setProperty("role", "stripName")
        self.header.addWidget(self.name_label, 1)
        lay.addLayout(self.header)

        self.caption_label = ElidedLabel(caption)
        self.caption_label.setProperty("role", "stripCaption")
        self.caption_label.setVisible(bool(caption))
        lay.addWidget(self.caption_label)

        self.top_area = QtWidgets.QWidget()
        self.top = QtWidgets.QVBoxLayout(self.top_area)
        self.top.setContentsMargins(0, 0, 0, 0)
        self.top.setSpacing(4)
        self.top.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.top_area)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(4)
        body.addStretch(1)
        if meter is not None:
            body.addWidget(meter)
        if fader is not None:
            if isinstance(fader, Fader) and color:
                fader.set_color(color)
            body.addWidget(fader)
        body.addStretch(1)
        lay.addLayout(body, 1)

        if readout is not None:
            lay.addWidget(readout)

    def reserve_top(self, rows: int) -> None:
        """Keep faders aligned across strips that carry 0, 1 or 2 chips."""
        self.top_area.setFixedHeight(max(0, rows * 22 + (rows - 1) * 4))

    def set_title(self, title: str) -> None:
        if self.name_label.text() != title:
            self.name_label.setText(title)

    def set_caption(self, caption: str) -> None:
        if self.caption_label.text() != caption:
            self.caption_label.setText(caption)
        self.caption_label.setVisible(bool(caption))

    def set_color(self, color: str | None) -> None:
        self._bar.set_color(color)
        if isinstance(self.fader, Fader) and color:
            self.fader.set_color(color)

    def set_flag(self, name: str, value: bool) -> None:
        """Toggle a style-sheet flag such as ``selected`` or ``offline``."""
        if bool(self.property(name)) == bool(value):
            return
        self.setProperty(name, "true" if value else "false")
        repolish(self)
        for child in self.findChildren(QtWidgets.QLabel):
            repolish(child)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class StripRack(QtWidgets.QScrollArea):
    """Horizontal row of strips that scrolls, with an explicit empty state."""

    def __init__(self, empty_text: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        inner = QtWidgets.QWidget()
        self._row = QtWidgets.QHBoxLayout(inner)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(8)
        self._empty = QtWidgets.QLabel(empty_text)
        self._empty.setProperty("role", "emptyState")
        self._empty.setWordWrap(True)
        self._empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._row.addWidget(self._empty, 1)
        self._row.addStretch(0)
        self._strips: list[QtWidgets.QWidget] = []
        self._trailing: list[QtWidgets.QWidget] = []
        self.setWidget(inner)

    def strips(self) -> list[QtWidgets.QWidget]:
        return list(self._strips)

    def add_strip(self, strip: QtWidgets.QWidget) -> None:
        self._row.insertWidget(len(self._strips) + 1, strip)
        self._strips.append(strip)
        self._sync_empty()

    def add_trailing(self, widget: QtWidgets.QWidget) -> None:
        """A widget that always stays after the strips (e.g. an add card)."""
        self._row.insertWidget(self._row.count() - 1, widget)
        self._trailing.append(widget)
        self._sync_empty()

    def clear(self) -> None:
        for strip in self._strips:
            self._row.removeWidget(strip)
            strip.deleteLater()
        self._strips = []
        self._sync_empty()

    def _sync_empty(self) -> None:
        empty = not self._strips
        self._empty.setVisible(empty and not self._trailing)
        self._row.setStretch(self._row.count() - 1, 0 if empty and not self._trailing else 1)


class Section(QtWidgets.QFrame):
    """Rounded panel with an uppercase title and right-aligned actions."""

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "panel")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        self.title_label = QtWidgets.QLabel(title.upper())
        self.title_label.setProperty("role", "sectionTitle")
        font = self.title_label.font()
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 1.5)
        self.title_label.setFont(font)
        head.addWidget(self.title_label)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setProperty("role", "hint")
        head.addWidget(self.count_label)
        head.addStretch(1)
        self.actions = QtWidgets.QHBoxLayout()
        self.actions.setSpacing(6)
        head.addLayout(self.actions)
        lay.addLayout(head)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setSpacing(8)
        lay.addLayout(self.body, 1)

    def set_count(self, text: str) -> None:
        self.count_label.setText(text)


class Card(QtWidgets.QFrame):
    """Settings card: a title, an optional one-line explanation, a body."""

    def __init__(self, title: str, description: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(10)
        title_label = QtWidgets.QLabel(title)
        title_label.setProperty("role", "cardTitle")
        lay.addWidget(title_label)
        if description:
            desc = QtWidgets.QLabel(description)
            desc.setProperty("role", "hint")
            desc.setWordWrap(True)
            lay.addWidget(desc)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setSpacing(10)
        lay.addLayout(self.body)
        lay.addStretch(1)


class SegmentedNav(QtWidgets.QFrame):
    """Page switcher drawn as a segmented control."""

    changed = QtCore.Signal(int)

    def __init__(self, labels: list[str], parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "segmentGroup")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: list[QtWidgets.QPushButton] = []
        for index, label in enumerate(labels):
            btn = QtWidgets.QPushButton(label)
            btn.setProperty("role", "segment")
            btn.setCheckable(True)
            btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self._group.addButton(btn, index)
            lay.addWidget(btn)
            self.buttons.append(btn)
        self.buttons[0].setChecked(True)
        self._group.idClicked.connect(self.changed)

    def current(self) -> int:
        return int(self._group.checkedId())

    def set_current(self, index: int) -> None:
        if self.current() != int(index):
            self.buttons[int(index)].setChecked(True)
            self.changed.emit(int(index))


class TopBar(QtWidgets.QFrame):
    """Window header: logo, brand, title/subtitle on the left, controls right."""

    def __init__(
        self, title: str, subtitle: str, icon: QtGui.QIcon | None = None, parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "topbar")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(18, 10, 14, 10)
        lay.setSpacing(12)
        if icon is not None and not icon.isNull():
            logo = QtWidgets.QLabel()
            logo.setPixmap(icon.pixmap(QtCore.QSize(34, 34)))
            lay.addWidget(logo)
        copy = QtWidgets.QVBoxLayout()
        copy.setSpacing(0)
        brand = QtWidgets.QLabel("PY-INTERCOM")
        brand.setProperty("role", "brand")
        font = brand.font()
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 2.0)
        brand.setFont(font)
        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setProperty("role", "title")
        self.subtitle_label = QtWidgets.QLabel(subtitle)
        self.subtitle_label.setProperty("role", "subtitle")
        copy.addWidget(brand)
        copy.addWidget(self.title_label)
        copy.addWidget(self.subtitle_label)
        lay.addLayout(copy)
        lay.addSpacing(16)
        self.center = QtWidgets.QHBoxLayout()
        self.center.setSpacing(8)
        lay.addLayout(self.center)
        lay.addStretch(1)
        self.right = QtWidgets.QHBoxLayout()
        self.right.setSpacing(8)
        lay.addLayout(self.right)


def compact_combo(combo: QtWidgets.QComboBox) -> QtWidgets.QComboBox:
    """Small combo for strips; long device names are elided in the box."""
    require_focus_for_wheel(combo)
    combo.setProperty("role", "compact")
    combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(4)
    combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
    return combo


class Chip(QtWidgets.QCheckBox):
    """Check box drawn as a full-width toggle chip, for use on strips."""

    def __init__(self, text: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFixedHeight(22)

    def hitButton(self, pos: QtCore.QPoint) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(self.fontMetrics().horizontalAdvance(self.text()) + 20, 22)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        on = self.isChecked()
        enabled = self.isEnabled()
        hover = self.underMouse() and enabled
        if on:
            fill = _qcolor(TOKENS["accent"] if enabled else TOKENS["border_strong"])
            border, ink = fill, _qcolor(TOKENS["accent_text"] if enabled else TOKENS["muted"])
        else:
            fill = _qcolor(TOKENS["bg"])
            border = _qcolor(TOKENS["muted"] if hover else TOKENS["border_strong"] if enabled else TOKENS["border"])
            ink = _qcolor(TOKENS["text"] if hover else TOKENS["muted"] if enabled else TOKENS["faint"])
        p.setPen(QtGui.QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(rect, 6, 6)
        font = self.font()
        font.setPixelSize(10)
        font.setBold(True)
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0.8)
        p.setFont(font)
        p.setPen(ink)
        p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


def chip(text: str, tooltip: str = "") -> Chip:
    box = Chip(text)
    if tooltip:
        box.setToolTip(tooltip)
    return box


def readout(text: str = "0 dB") -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setProperty("role", "readout")
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    return label


def format_db(value: float) -> str:
    v = round(float(value))
    return f"+{v} dB" if v > 0 else f"{v} dB"
