"""Global push-to-talk keyboard shortcuts (pynput listener bridged to Qt).

Keys are identified by their Windows virtual-key code, never by the character
they type: that character depends on the modifiers held at the time of each
event (Ctrl+T types "\\x14", Shift changes the digit row on AZERTY), so a key
pressed with one set of modifiers and released with another would otherwise
stay held forever and leave a microphone open.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Final

from loguru import logger
from pynput import keyboard
from PySide6 import QtCore, QtGui

from ..common.logging import best_effort

_QtKey = QtCore.Qt.Key
_QtMod = QtCore.Qt.KeyboardModifier

# A key whose key-up event never reached the hook (session lock, UAC prompt,
# secure desktop) is released once it has been seen up on two consecutive
# checks after having been seen down. A key-up that does arrive is always
# handled immediately.
_HELD_KEY_CHECK_MS = 250

_MODIFIER_GROUPS = (
    (_QtMod.ControlModifier, frozenset({"key:ctrl", "key:ctrl_l", "key:ctrl_r"})),
    (_QtMod.ShiftModifier, frozenset({"key:shift", "key:shift_l", "key:shift_r"})),
    (_QtMod.AltModifier, frozenset({"key:alt", "key:alt_l", "key:alt_r", "key:alt_gr"})),
    (_QtMod.MetaModifier, frozenset({"key:cmd", "key:cmd_l", "key:cmd_r"})),
)

# Qt keys that cannot be the key of a shortcut on their own.
_QT_NOT_A_SHORTCUT_KEY = frozenset(
    {_QtKey.Key_unknown, _QtKey.Key_Control, _QtKey.Key_Shift, _QtKey.Key_Alt, _QtKey.Key_AltGr, _QtKey.Key_Meta}
)

# Qt key -> name of the matching pynput ``keyboard.Key``.
_QT_NAMED_KEYS = {
    _QtKey.Key_Space: "key:space",
    _QtKey.Key_Tab: "key:tab",
    _QtKey.Key_Return: "key:enter",
    _QtKey.Key_Enter: "key:enter",
    _QtKey.Key_Escape: "key:esc",
    _QtKey.Key_Backspace: "key:backspace",
    _QtKey.Key_Delete: "key:delete",
    _QtKey.Key_Insert: "key:insert",
    _QtKey.Key_Home: "key:home",
    _QtKey.Key_End: "key:end",
    _QtKey.Key_PageUp: "key:page_up",
    _QtKey.Key_PageDown: "key:page_down",
    _QtKey.Key_Up: "key:up",
    _QtKey.Key_Down: "key:down",
    _QtKey.Key_Left: "key:left",
    _QtKey.Key_Right: "key:right",
    _QtKey.Key_Pause: "key:pause",
    _QtKey.Key_Print: "key:print_screen",
    _QtKey.Key_ScrollLock: "key:scroll_lock",
    _QtKey.Key_Menu: "key:menu",
    _QtKey.Key_CapsLock: "key:caps_lock",
    _QtKey.Key_NumLock: "key:num_lock",
}

# The shortcut editor (QKeySequenceEdit) drops KeypadModifier, so a saved
# shortcut cannot tell a numeric keypad key from the main-keyboard key typing
# the same character: such a shortcut fires on either key.
_VK_NUMPAD0 = 0x60
_QT_KEYPAD_OPERATOR_VKS = {
    _QtKey.Key_Asterisk: 0x6A,  # VK_MULTIPLY
    _QtKey.Key_Plus: 0x6B,  # VK_ADD
    _QtKey.Key_Minus: 0x6D,  # VK_SUBTRACT
    _QtKey.Key_Period: 0x6E,  # VK_DECIMAL
    _QtKey.Key_Comma: 0x6E,  # VK_DECIMAL on layouts with a decimal comma
    _QtKey.Key_Slash: 0x6F,  # VK_DIVIDE
}


if sys.platform == "win32":
    import ctypes

    # A private handle so the prototypes below never leak into other users of
    # ctypes.windll.user32.
    _user32 = ctypes.WinDLL("user32")
    _user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    _user32.VkKeyScanW.restype = ctypes.c_short
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short

    def _char_vk(char: str) -> int | None:
        """VK code of the key typing ``char`` on the current keyboard layout."""
        for candidate in (char.lower(), char):
            if len(candidate) == 1:
                scan = int(_user32.VkKeyScanW(candidate))
                if scan != -1:
                    return scan & 0xFF
        return None

    def _physical_key_is_down(vk: int) -> bool:
        return bool(_user32.GetAsyncKeyState(int(vk)) & 0x8000)

    _DEFAULT_KEY_IS_DOWN: Callable[[int], bool] | None = _physical_key_is_down
else:

    def _char_vk(char: str) -> int | None:
        return None

    _DEFAULT_KEY_IS_DOWN = None


def _vk_token(vk: int) -> str:
    if 0x41 <= vk <= 0x5A:  # VK_A..VK_Z
        return f"char:{chr(vk).lower()}"
    if 0x30 <= vk <= 0x39:  # VK_0..VK_9: the digit row, whatever it types
        return f"char:{chr(vk)}"
    return f"vk:{vk}"


def _key_vk(k) -> int | None:
    code = k.value if isinstance(k, keyboard.Key) else k
    vk = getattr(code, "vk", None)
    return int(vk) if vk is not None else None


def _norm_key(k) -> str | None:
    if isinstance(k, keyboard.Key):
        return f"key:{k.name}"
    if isinstance(k, keyboard.KeyCode):
        if k.vk is not None:
            return _vk_token(int(k.vk))
        if k.char:  # injected Unicode character (VK_PACKET): no key code
            return f"char:{k.char.lower()}"
    return None


def _qt_key_tokens(key: QtCore.Qt.Key) -> frozenset[str] | None:
    """Listener keys that type the Qt key; holding any one of them matches."""
    value = int(key.value)
    named = _QT_NAMED_KEYS.get(key)
    if named is not None:
        return frozenset({named})
    if _QtKey.Key_F1.value <= value <= _QtKey.Key_F24.value:
        return frozenset({f"key:f{value - _QtKey.Key_F1.value + 1}"})
    if _QtKey.Key_A.value <= value <= _QtKey.Key_Z.value:
        return frozenset({_vk_token(value)})  # Qt and Windows share the ASCII codes here
    if _QtKey.Key_0.value <= value <= _QtKey.Key_9.value:
        return frozenset({_vk_token(value), f"vk:{_VK_NUMPAD0 + value - _QtKey.Key_0.value}"})
    if value < 0x01000000:  # any other printable character
        char = chr(value)
        vk = _char_vk(char)
        tokens = {_vk_token(vk) if vk is not None else f"char:{char.lower()}"}
        keypad_vk = _QT_KEYPAD_OPERATOR_VKS.get(key)
        if keypad_vk is not None:
            tokens.add(f"vk:{keypad_vk}")
        return frozenset(tokens)
    return None


def _parse_qt_shortcut(seq_text: str) -> list[frozenset[str]]:
    """Map a Qt portable shortcut ("Ctrl+T", "1", "PgUp") to key groups.

    The shortcut is active while one key of every group is held. A sequence
    that cannot fire safely (empty, several chords, modifiers only) maps to
    no group at all, so it never fires.
    """
    sequence = QtGui.QKeySequence.fromString(str(seq_text or ""), QtGui.QKeySequence.SequenceFormat.PortableText)
    if sequence.count() != 1:
        return []
    combo = sequence[0]
    key = combo.key()
    if key in _QT_NOT_A_SHORTCUT_KEY:
        return []
    tokens = _qt_key_tokens(key)
    if tokens is None:
        return []
    modifiers = combo.keyboardModifiers()
    groups = [names for flag, names in _MODIFIER_GROUPS if modifiers & flag]
    groups.append(tokens)
    return groups


class PttHotkeyState:
    """Thread-safe, Qt-free state machine used by the global key listener."""

    def __init__(
        self,
        bindings: dict[int, tuple[str, str]],
        toggle_states: dict[int, bool] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._bindings: dict[int, tuple[str, tuple[frozenset[str], ...]]] = {
            int(bus_id): (str(mode), tuple(_parse_qt_shortcut(sequence)))
            for bus_id, (mode, sequence) in bindings.items()
        }
        self._pressed: set[str] = set()
        self._combo_was_active: dict[int, bool] = {}
        self._active_buses: dict[int, bool] = {}
        self._toggle_states = {int(bus_id): bool(state) for bus_id, state in (toggle_states or {}).items()}

    def _combo_active(self, groups: tuple[frozenset[str], ...]) -> bool:
        return bool(groups) and all(any(key in self._pressed for key in group) for group in groups)

    def _update(self) -> list[tuple[int, bool]]:
        changes: list[tuple[int, bool]] = []
        for bus_id, (mode, groups) in self._bindings.items():
            if mode == "always_on":
                continue

            active = self._combo_active(groups)
            was_active = bool(self._combo_was_active.get(int(bus_id), False))
            self._combo_was_active[int(bus_id)] = bool(active)

            if mode == "toggle":
                # Toggle only on the inactive -> active edge of the configured
                # chord. Pressing an unrelated key while the chord is held no
                # longer toggles the bus a second time.
                if active and not was_active:
                    new_state = not bool(self._toggle_states.get(int(bus_id), False))
                    self._toggle_states[int(bus_id)] = bool(new_state)
                    changes.append((int(bus_id), bool(new_state)))
                continue

            previous = bool(self._active_buses.get(int(bus_id), False))
            if bool(active) != previous:
                self._active_buses[int(bus_id)] = bool(active)
                changes.append((int(bus_id), bool(active)))
        return changes

    def press(self, key: str) -> list[tuple[int, bool]]:
        with self._lock:
            if key in self._pressed:
                return []
            self._pressed.add(str(key))
            return self._update()

    def release(self, key: str) -> list[tuple[int, bool]]:
        with self._lock:
            self._pressed.discard(str(key))
            return self._update()

    def release_all(self) -> list[tuple[int, bool]]:
        with self._lock:
            changes: list[tuple[int, bool]] = []
            self._pressed.clear()
            self._combo_was_active.clear()
            for bus_id, active in list(self._active_buses.items()):
                if active:
                    changes.append((int(bus_id), False))
            self._active_buses.clear()
            for bus_id, active in list(self._toggle_states.items()):
                if active:
                    changes.append((int(bus_id), False))
            self._toggle_states = {bus_id: False for bus_id in self._toggle_states}
            return changes

    def active_buses(self) -> dict[int, bool]:
        with self._lock:
            return dict(self._active_buses)

    def set_toggle_state(self, bus_id: int, active: bool) -> None:
        """Align a Toggle bus with a change made elsewhere (e.g. HTTP remote)."""
        with self._lock:
            self._toggle_states[int(bus_id)] = bool(active)


_SWALLOWED_KEY_EVENTS: Final = frozenset(
    {QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.KeyRelease, QtCore.QEvent.Type.ShortcutOverride}
)


class GlobalPttHotkeys(QtCore.QObject):
    """System-wide hook: it keeps working while the window is in the background.

    ``key_is_down`` reads the physical key state; it lets a watchdog release a
    key whose key-up event was never delivered. Toggle states are never
    touched by the watchdog: it only clears held keys.

    The watchdog only releases a key it has itself read as down since its
    press: a key that another low-level hook (macro software, pedal driver,
    remapper) blocks further down the chain never reaches the asynchronous
    key state, and is left to its real key-up.

    While the hook runs, the window ignores the PTT keys: the hook does not
    suppress them, and a focused button, fader or dialog would otherwise act
    on them too (Space pressing "Tout relâcher", PgDn lowering a fader).

    ``_held_lock`` covers both the held keys and the state machine, so the
    hook thread and the watchdog never act on a stale decision. Signals are
    emitted after the lock is released: their receivers live on the Qt
    thread, which also runs the watchdog, so they still arrive in order.
    """

    ptt_changed = QtCore.Signal(int, bool)

    def __init__(
        self,
        bindings: dict[int, tuple[str, str]],
        toggle_states: dict[int, bool] | None = None,
        parent: QtCore.QObject | None = None,
        *,
        key_is_down: Callable[[int], bool] | None = _DEFAULT_KEY_IS_DOWN,
    ) -> None:
        super().__init__(parent)
        self._listener: keyboard.Listener | None = None
        self._state = PttHotkeyState(bindings, toggle_states)
        self._key_is_down = key_is_down
        self._held_lock = threading.Lock()
        self._held_vks: dict[str, int] = {}
        self._confirmed_down: set[str] = set()
        self._seen_up_once: set[str] = set()
        self._watchdog = QtCore.QTimer(self)
        self._watchdog.setInterval(_HELD_KEY_CHECK_MS)
        self._watchdog.timeout.connect(self._check_held_keys)
        self._ptt_key_tokens = frozenset(
            token
            for mode, sequence in bindings.values()
            if mode != "always_on"
            for groups in [_parse_qt_shortcut(sequence)]
            if groups
            for token in groups[-1]
        )

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()
        if self._key_is_down is not None:
            self._watchdog.start()
        app = QtCore.QCoreApplication.instance()
        if app is not None and self._ptt_key_tokens:
            app.installEventFilter(self)

    def stop(self) -> None:
        self._watchdog.stop()
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        listener = self._listener
        self._listener = None
        if listener is not None:
            with best_effort("listener.stop()"):
                listener.stop()
        self.release_all()

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() in _SWALLOWED_KEY_EVENTS and self._is_ptt_key(event):
            # An accepted override also keeps the key from firing a shortcut.
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _is_ptt_key(self, event: QtCore.QEvent) -> bool:
        try:
            tokens = _qt_key_tokens(QtCore.Qt.Key(event.key()))
        except ValueError:  # a key code Qt has no enum member for
            return False
        return bool(tokens and tokens & self._ptt_key_tokens)

    def release_all(self) -> None:
        with self._held_lock:
            self._held_vks.clear()
            self._confirmed_down.clear()
            self._seen_up_once.clear()
            changes = self._state.release_all()
        self._emit_changes(changes)

    def active_buses(self) -> dict[int, bool]:
        return self._state.active_buses()

    def set_toggle_state(self, bus_id: int, active: bool) -> None:
        self._state.set_toggle_state(bus_id, active)

    def _emit_changes(self, changes: list[tuple[int, bool]]) -> None:
        for bus_id, active in changes:
            self.ptt_changed.emit(int(bus_id), bool(active))

    def _on_press(self, key) -> None:
        token = _norm_key(key)
        if token is None:
            return
        vk = _key_vk(key)
        with self._held_lock:
            if vk is not None:
                self._held_vks[token] = vk
            self._seen_up_once.discard(token)
            changes = self._state.press(token)
        self._emit_changes(changes)

    def _on_release(self, key) -> None:
        token = _norm_key(key)
        if token is None:
            return
        with self._held_lock:
            self._forget_held(token)
            changes = self._state.release(token)
        self._emit_changes(changes)

    def _forget_held(self, token: str) -> None:
        self._held_vks.pop(token, None)
        self._confirmed_down.discard(token)
        self._seen_up_once.discard(token)

    @QtCore.Slot()
    def _check_held_keys(self) -> None:
        key_is_down = self._key_is_down
        if key_is_down is None:
            return
        lost: list[str] = []
        changes: list[tuple[int, bool]] = []
        with self._held_lock:
            for token, vk in list(self._held_vks.items()):
                if key_is_down(vk):
                    self._confirmed_down.add(token)
                    self._seen_up_once.discard(token)
                elif token not in self._confirmed_down:
                    # Never read as down: the key is blocked by another hook
                    # (or not yet visible); only its key-up releases it.
                    continue
                elif token in self._seen_up_once:
                    # Up on two consecutive checks: the key-up was lost.
                    self._forget_held(token)
                    lost.append(token)
                    changes.extend(self._state.release(token))
                else:
                    # A single "up" reading can race the hook, which runs
                    # before Windows updates the asynchronous key state.
                    self._seen_up_once.add(token)
        for token in lost:
            logger.warning("global PTT key {} released without a key-up event", token)
        self._emit_changes(changes)
