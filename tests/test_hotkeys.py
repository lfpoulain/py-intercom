from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pynput.keyboard import Key, KeyCode
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from py_intercom.client import hotkeys
from py_intercom.client.hotkeys import GlobalPttHotkeys, PttHotkeyState, _parse_qt_shortcut

CTRL = frozenset({"key:ctrl", "key:ctrl_l", "key:ctrl_r"})
SHIFT = frozenset({"key:shift", "key:shift_l", "key:shift_r"})

VK_T = 0x54
VK_1 = 0x31
VK_6 = 0x36
VK_NUMPAD1 = 0x61
VK_SUBTRACT = 0x6D
VK_MULTIPLY = 0x6A
VK_OEM_5 = 0xDC


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _hotkeys(bindings, *, toggles=None, key_is_down=None) -> tuple[GlobalPttHotkeys, list[tuple[int, bool]]]:
    emitted: list[tuple[int, bool]] = []
    listener = GlobalPttHotkeys(bindings, toggles, key_is_down=key_is_down)
    listener.ptt_changed.connect(lambda bus_id, active: emitted.append((bus_id, active)))
    return listener, emitted


# --- Shortcut parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("PgUp", "key:page_up"),
        ("PgDown", "key:page_down"),
        ("Del", "key:delete"),
        ("Ins", "key:insert"),
        ("Pause", "key:pause"),
        ("Print", "key:print_screen"),
        ("ScrollLock", "key:scroll_lock"),
        ("Menu", "key:menu"),
        ("CapsLock", "key:caps_lock"),
        ("NumLock", "key:num_lock"),
        ("F13", "key:f13"),
        ("Space", "key:space"),
        ("Enter", "key:enter"),
        ("Num+Enter", "key:enter"),
        ("T", "char:t"),
    ],
)
def test_qt_key_names_map_to_listener_keys(qt_app, text, token) -> None:
    assert _parse_qt_shortcut(text) == [frozenset({token})]


@pytest.mark.parametrize("text", ["1", "Num+1"])
def test_digit_matches_the_digit_row_and_the_numeric_keypad(qt_app, text) -> None:
    assert _parse_qt_shortcut(text) == [frozenset({"char:1", "vk:97"})]


def test_modifiers_become_groups(qt_app) -> None:
    assert _parse_qt_shortcut("Ctrl+Shift+T") == [CTRL, SHIFT, frozenset({"char:t"})]


def test_plus_key_is_not_lost(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(hotkeys, "_char_vk", lambda char: 0xBB if char == "+" else None)

    assert _parse_qt_shortcut("Ctrl++") == [CTRL, frozenset({"vk:187", "vk:107"})]
    assert _parse_qt_shortcut("Ctrl+Num++") == [CTRL, frozenset({"vk:187", "vk:107"})]


def test_punctuation_is_bound_to_the_key_that_types_it(qt_app, monkeypatch) -> None:
    # AZERTY: "&" is typed by the digit-row key VK_1.
    monkeypatch.setattr(hotkeys, "_char_vk", lambda char: VK_1 if char == "&" else None)

    assert _parse_qt_shortcut("&") == [frozenset({"char:1"})]


@pytest.mark.parametrize("text", ["", "Ctrl", "Shift", "Alt", "Meta", "A, B", "garbage"])
def test_sequences_that_cannot_fire_safely_bind_nothing(qt_app, text) -> None:
    assert _parse_qt_shortcut(text) == []


def test_ctrl_plus_never_fires_on_ctrl_alone(qt_app) -> None:
    state = PttHotkeyState({0: ("ptt", "Ctrl++")})

    assert state.press("key:ctrl_l") == []
    assert state.active_buses() == {}


# --- Listener events ------------------------------------------------------------


def test_ctrl_letter_fires_although_it_types_a_control_character(qt_app) -> None:
    listener, emitted = _hotkeys({0: ("ptt", "Ctrl+T")})

    listener._on_press(Key.ctrl_l)
    listener._on_press(KeyCode(vk=VK_T, char="\x14"))
    listener._on_release(KeyCode(vk=VK_T, char="\x14"))

    assert emitted == [(0, True), (0, False)]


def test_key_released_under_another_modifier_does_not_stay_held(qt_app) -> None:
    listener, emitted = _hotkeys({0: ("ptt", "T")})

    listener._on_press(KeyCode(vk=VK_T, char="t"))
    listener._on_press(Key.ctrl_l)
    listener._on_release(KeyCode(vk=VK_T, char="\x14"))

    assert emitted == [(0, True), (0, False)]
    assert listener.active_buses() == {0: False}


def test_digit_row_released_after_shift_changes_its_character(qt_app) -> None:
    listener, emitted = _hotkeys({1: ("ptt", "Shift+1")})

    listener._on_press(Key.shift)
    listener._on_press(KeyCode(vk=VK_1, char="1"))
    listener._on_release(Key.shift)
    listener._on_release(KeyCode(vk=VK_1, char="&"))

    assert emitted == [(1, True), (1, False)]


def _edited_shortcut(key: QtCore.Qt.Key, text: str) -> str:
    """What the client's shortcut editor saves for a numeric keypad key."""
    edit = QtWidgets.QKeySequenceEdit()
    edit.keyPressEvent(
        QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, QtCore.Qt.KeyboardModifier.KeypadModifier, text)
    )
    return edit.keySequence().toString()


def test_numpad_digit_saved_by_the_editor_fires_on_both_digit_keys(qt_app) -> None:
    shortcut = _edited_shortcut(QtCore.Qt.Key.Key_1, "1")
    listener, emitted = _hotkeys({0: ("ptt", shortcut)})

    listener._on_press(KeyCode(vk=VK_NUMPAD1, char="1"))
    listener._on_release(KeyCode(vk=VK_NUMPAD1, char="1"))
    listener._on_press(KeyCode(vk=VK_1, char="&"))  # AZERTY digit row
    listener._on_release(KeyCode(vk=VK_1, char="&"))

    assert emitted == [(0, True), (0, False), (0, True), (0, False)]


@pytest.mark.parametrize(
    ("key", "text", "keypad_vk"),
    [(QtCore.Qt.Key.Key_Minus, "-", VK_SUBTRACT), (QtCore.Qt.Key.Key_Asterisk, "*", VK_MULTIPLY)],
)
def test_numpad_operator_saved_by_the_editor_fires_on_the_keypad(qt_app, monkeypatch, key, text, keypad_vk) -> None:
    # AZERTY: "-" is typed by the digit-row key VK_6, "*" by VK_OEM_5.
    monkeypatch.setattr(hotkeys, "_char_vk", {"-": VK_6, "*": VK_OEM_5}.get)
    listener, emitted = _hotkeys({0: ("ptt", _edited_shortcut(key, text))})

    listener._on_press(KeyCode(vk=keypad_vk, char=text))
    listener._on_release(KeyCode(vk=keypad_vk, char=text))

    assert emitted == [(0, True), (0, False)]


# --- Lost key-up watchdog -------------------------------------------------------


def test_watchdog_releases_a_key_whose_key_up_was_lost(qt_app) -> None:
    down = {VK_T: True}
    listener, emitted = _hotkeys({0: ("ptt", "T")}, key_is_down=lambda vk: down.get(vk, False))
    listener._on_press(KeyCode(vk=VK_T, char="t"))

    listener._check_held_keys()
    down[VK_T] = False  # e.g. released on the lock screen: no key-up event
    listener._check_held_keys()
    assert emitted == [(0, True)]  # one reading is not enough

    listener._check_held_keys()
    assert emitted == [(0, True), (0, False)]


def test_watchdog_leaves_held_keys_and_toggles_alone(qt_app) -> None:
    down = {VK_T: True, 0x70: False}
    listener, emitted = _hotkeys(
        {0: ("ptt", "T"), 1: ("toggle", "F1")},
        key_is_down=lambda vk: down.get(vk, False),
    )
    listener._on_press(Key.f1)
    listener._on_release(Key.f1)
    listener._on_press(KeyCode(vk=VK_T, char="t"))

    for _ in range(4):
        listener._check_held_keys()

    assert emitted == [(1, True), (0, True)]


def test_one_up_reading_followed_by_a_down_reading_keeps_the_key(qt_app) -> None:
    readings = iter([True, False, True, False])
    listener, emitted = _hotkeys({0: ("ptt", "T")}, key_is_down=lambda vk: next(readings))
    listener._on_press(KeyCode(vk=VK_T, char="t"))

    for _ in range(4):
        listener._check_held_keys()

    assert emitted == [(0, True)]


def test_watchdog_never_releases_a_key_it_has_not_seen_down(qt_app) -> None:
    # Another low-level hook blocks the key further down the chain: Windows
    # never updates its asynchronous state, only the real key-up ends the PTT.
    listener, emitted = _hotkeys({0: ("ptt", "F24")}, key_is_down=lambda vk: False)
    listener._on_press(Key.f24)

    for _ in range(8):
        listener._check_held_keys()
        listener._on_press(Key.f24)  # auto-repeat
    assert emitted == [(0, True)]
    assert listener.active_buses() == {0: True}

    listener._on_release(Key.f24)
    assert emitted == [(0, True), (0, False)]


def test_watchdog_needs_a_new_down_reading_after_each_press(qt_app) -> None:
    down = {VK_T: True}
    listener, emitted = _hotkeys({0: ("ptt", "T")}, key_is_down=lambda vk: down.get(vk, False))
    listener._on_press(KeyCode(vk=VK_T, char="t"))
    listener._check_held_keys()  # seen down
    listener._on_release(KeyCode(vk=VK_T, char="t"))

    down[VK_T] = False  # pressed again, but now blocked by another hook
    listener._on_press(KeyCode(vk=VK_T, char="t"))
    for _ in range(4):
        listener._check_held_keys()

    assert emitted == [(0, True), (0, False), (0, True)]


def test_watchdog_release_and_state_change_are_one_step(qt_app) -> None:
    # The hook thread must not slip a new press between the watchdog's
    # decision and its state change.
    down = {VK_T: True}
    listener, emitted = _hotkeys({0: ("ptt", "T")}, key_is_down=lambda vk: down.get(vk, False))
    listener._on_press(KeyCode(vk=VK_T, char="t"))
    listener._check_held_keys()
    down[VK_T] = False
    listener._check_held_keys()

    original_release = listener._state.release

    def release_holding_the_lock(token):
        assert listener._held_lock.locked()
        return original_release(token)

    listener._state.release = release_holding_the_lock
    listener._check_held_keys()

    assert emitted == [(0, True), (0, False)]


class _NoListener:
    """Stands in for the pynput hook: no real keyboard hook in tests."""

    daemon = False

    def __init__(self, **_callbacks) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def test_ptt_keys_never_reach_the_window_while_the_hook_runs(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(hotkeys.keyboard, "Listener", _NoListener)
    listener = GlobalPttHotkeys({0: ("ptt", "Space"), 1: ("always_on", "F2")}, key_is_down=None)
    button = QtWidgets.QPushButton("Tout relâcher")
    clicks: list[bool] = []
    button.clicked.connect(lambda: clicks.append(True))
    button.show()
    button.setFocus()

    listener.start()
    try:
        QtTest.QTest.keyClick(button, QtCore.Qt.Key.Key_Space)
        assert clicks == []
        # Other keys and an always-on bus's key still reach the window.
        assert not listener._is_ptt_key(
            QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F2, QtCore.Qt.KeyboardModifier.NoModifier)
        )
    finally:
        listener.stop()
    QtTest.QTest.keyClick(button, QtCore.Qt.Key.Key_Space)
    assert clicks == [True]
    button.close()
