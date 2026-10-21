"""Global push-to-talk keyboard shortcuts (pynput listener bridged to Qt)."""

from __future__ import annotations

import threading

from pynput import keyboard
from PySide6 import QtCore

from ..common.logging import best_effort


def _norm_key(k) -> str | None:
    try:
        if isinstance(k, keyboard.Key):
            return f"key:{k.name}"
        if isinstance(k, keyboard.KeyCode):
            if k.char:
                return f"char:{str(k.char).lower()}"
            if k.vk is not None:
                vk = int(k.vk)
                # Map A-Z VK codes (65-90) to char: so Shift+letter matches on all layouts
                if 65 <= vk <= 90:
                    return f"char:{chr(vk).lower()}"
                # Map 0-9 VK codes (48-57)
                if 48 <= vk <= 57:
                    return f"char:{chr(vk)}"
                return f"vk:{vk}"
    except Exception:
        return None
    return None


def _parse_qt_shortcut(seq_text: str) -> list[set[str]]:
    if not seq_text:
        return []

    tokens = [t.strip() for t in str(seq_text).split("+") if t.strip()]
    groups: list[set[str]] = []

    def _mod_group(name: str) -> set[str]:
        n = str(name).lower()
        if n in ("ctrl", "control"):
            return {"key:ctrl", "key:ctrl_l", "key:ctrl_r"}
        if n == "shift":
            return {"key:shift", "key:shift_l", "key:shift_r"}
        if n in ("alt", "option"):
            return {"key:alt", "key:alt_l", "key:alt_r", "key:alt_gr"}
        if n in ("meta", "win", "super", "cmd", "command"):
            return {"key:cmd", "key:cmd_l", "key:cmd_r"}
        return set()

    special = {
        "space": {"key:space"},
        "tab": {"key:tab"},
        "enter": {"key:enter"},
        "return": {"key:enter"},
        "esc": {"key:esc"},
        "escape": {"key:esc"},
        "backspace": {"key:backspace"},
        "delete": {"key:delete"},
        "insert": {"key:insert"},
        "home": {"key:home"},
        "end": {"key:end"},
        "pageup": {"key:page_up"},
        "pagedown": {"key:page_down"},
        "up": {"key:up"},
        "down": {"key:down"},
        "left": {"key:left"},
        "right": {"key:right"},
    }

    for t in tokens:
        mg = _mod_group(t)
        if mg:
            groups.append(mg)
            continue

        low = str(t).lower()
        if low in special:
            groups.append(set(special[low]))
            continue

        if low.startswith("f") and low[1:].isdigit():
            groups.append({f"key:{low}"})
            continue

        if len(low) == 1:
            groups.append({f"char:{low}"})
            continue

        groups.append({f"char:{low}"})

    return groups


class PttHotkeyState:
    """Thread-safe, Qt-free state machine used by the global key listener."""

    def __init__(
        self,
        bindings: dict[int, tuple[str, str]],
        toggle_states: dict[int, bool] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._bindings: dict[int, tuple[str, tuple[frozenset[str], ...]]] = {}
        for bus_id, (mode, sequence) in bindings.items():
            groups = tuple(frozenset(group) for group in _parse_qt_shortcut(sequence))
            self._bindings[int(bus_id)] = (str(mode), groups)
        self._pressed: set[str] = set()
        self._combo_was_active: dict[int, bool] = {}
        self._active_buses: dict[int, bool] = {}
        self._toggle_states = {
            int(bus_id): bool(state) for bus_id, state in (toggle_states or {}).items()
        }

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


class GlobalPttHotkeys(QtCore.QObject):
    ptt_changed = QtCore.Signal(int, bool)

    def __init__(
        self,
        bindings: dict[int, tuple[str, str]],
        toggle_states: dict[int, bool] | None = None,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._listener: keyboard.Listener | None = None
        self._state = PttHotkeyState(bindings, toggle_states)

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            with best_effort("listener.stop()"):
                listener.stop()
                listener.join(timeout=1.0)
        self.release_all()

    def release_all(self) -> None:
        for bus_id, active in self._state.release_all():
            self.ptt_changed.emit(int(bus_id), bool(active))

    def active_buses(self) -> dict[int, bool]:
        return self._state.active_buses()

    def _emit_changes(self, changes: list[tuple[int, bool]]) -> None:
        for bus_id, active in changes:
            self.ptt_changed.emit(int(bus_id), bool(active))

    def _on_press(self, key) -> None:
        k = _norm_key(key)
        if k:
            self._emit_changes(self._state.press(k))

    def _on_release(self, key) -> None:
        k = _norm_key(key)
        if k:
            self._emit_changes(self._state.release(k))
