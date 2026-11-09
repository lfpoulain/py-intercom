"""Device inventory lifecycle and window helpers shared by both windows."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtTest, QtWidgets

import py_intercom.common.gui_utils as gui_utils_mod
from py_intercom.common.devices import DeviceInfo
from py_intercom.common.gui_utils import DeviceRefresh, load_app_icon, set_app_icon

_SPEAKERS = DeviceInfo(1, "Speakers", "Windows WASAPI", 0, 2, 48000)


def _wait_until(condition, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        QtTest.QTest.qWait(10)


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def inventory(monkeypatch):
    """list_devices blocks until released; the timeout is short."""
    release = threading.Event()
    calls: list[dict] = []

    def list_devices(**kwargs):
        calls.append(kwargs)
        release.wait(5.0)
        return [_SPEAKERS]

    monkeypatch.setattr(gui_utils_mod, "list_devices", list_devices)
    monkeypatch.setattr(gui_utils_mod, "DEVICE_REFRESH_TIMEOUT_MS", 30)
    yield SimpleNamespace(set=release.set, calls=calls)
    release.set()


class _Window(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.refresh = DeviceRefresh(self)
        self.events: list[tuple] = []
        self.refresh.inventory.connect(lambda devs, error: self.events.append(("inventory", list(devs), error)))
        self.refresh.timed_out.connect(lambda: self.events.append(("timed_out",)))
        self.refresh.finished.connect(lambda: self.events.append(("finished", self.refresh.running)))
        self.hung_at_close: bool | None = None

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.refresh.close_must_wait():
            event.ignore()
            return
        self.hung_at_close = self.refresh.abandon()
        super().closeEvent(event)


@pytest.fixture
def window(qt_app):
    win = _Window()
    yield win
    win.deleteLater()
    qt_app.processEvents()


def test_inventory_is_delivered_then_finished(window, inventory, monkeypatch) -> None:
    monkeypatch.setattr(gui_utils_mod, "DEVICE_REFRESH_TIMEOUT_MS", 5000)
    inventory.set()
    window.refresh.start("WASAPI", hard_refresh=False)
    assert window.refresh.running
    assert QtWidgets.QApplication.overrideCursor() is not None

    _wait_until(lambda: ("finished", False) in window.events)

    assert window.events == [("inventory", [_SPEAKERS], ""), ("finished", False)]
    assert inventory.calls == [{"hostapi_substring": "WASAPI", "hard_refresh": False, "validate": True}]
    assert QtWidgets.QApplication.overrideCursor() is None


def test_a_second_start_is_ignored_while_running(window, inventory) -> None:
    window.refresh.start(None, hard_refresh=True)
    window.refresh.start(None, hard_refresh=True)
    inventory.set()
    _wait_until(lambda: not window.refresh.running)

    assert len(inventory.calls) == 1


def test_late_inventory_after_the_timeout_is_still_delivered(window, inventory) -> None:
    window.refresh.start(None, hard_refresh=True)
    _wait_until(lambda: ("timed_out",) in window.events)
    assert window.refresh.running
    assert QtWidgets.QApplication.overrideCursor() is None  # the operator gets the window back

    inventory.set()
    _wait_until(lambda: not window.refresh.running)

    assert window.events[1:] == [("inventory", [_SPEAKERS], ""), ("finished", False)]


def test_close_waits_for_the_inventory_and_is_replayed(window, inventory, monkeypatch) -> None:
    monkeypatch.setattr(gui_utils_mod, "DEVICE_REFRESH_TIMEOUT_MS", 5000)
    window.show()
    window.refresh.start(None, hard_refresh=True)

    assert not window.close()
    assert window.isVisible()

    inventory.set()
    _wait_until(lambda: not window.isVisible())

    assert window.hung_at_close is False
    assert window.events == []  # nothing is handed to a closing window


def test_close_requested_before_the_timeout_completes_at_the_timeout(qt_app, window, inventory) -> None:
    window.show()
    window.refresh.start(None, hard_refresh=True)
    thread = window.refresh._thread

    assert not window.close()
    _wait_until(lambda: not window.isVisible())

    assert window.hung_at_close is True
    assert thread.parent() is qt_app and thread.isRunning()
    assert QtWidgets.QApplication.overrideCursor() is None
    ended: list[bool] = []
    thread.finished.connect(lambda: ended.append(True))  # the thread then deletes itself
    inventory.set()
    _wait_until(lambda: ended)
    QtTest.QTest.qWait(20)
    assert window.events == []


def test_close_without_inventory_is_immediate(window) -> None:
    window.show()

    assert window.close()
    assert window.hung_at_close is False


def test_set_app_icon_finds_the_bundled_logo(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(gui_utils_mod.sys, "platform", "test")  # leave the process AppUserModelID alone
    previous = qt_app.windowIcon()
    win = QtWidgets.QWidget()
    try:
        set_app_icon(win, "py-intercom.test")
        assert not win.windowIcon().isNull()
        assert not qt_app.windowIcon().isNull()
    finally:
        qt_app.setWindowIcon(previous)
        win.deleteLater()


def test_each_role_has_its_own_icon_and_any_other_name_gets_the_plain_logo(qt_app) -> None:
    server, client, plain, launcher = (load_app_icon(name) for name in ("server", "client", "plain", "launcher"))

    assert all(icon is not None and not icon.isNull() for icon in (server, client, plain, launcher))
    # Distinct images: the role badge is what tells the two windows apart in the taskbar.
    size = QtCore.QSize(32, 32)
    server_image, client_image, plain_image = (icon.pixmap(size).toImage() for icon in (server, client, plain))
    assert server_image != client_image
    assert server_image != plain_image
    assert launcher.pixmap(size).toImage() == plain_image
