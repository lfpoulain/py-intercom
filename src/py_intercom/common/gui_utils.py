from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .devices import list_devices
from .logging import best_effort
from .single_instance import SingleInstance
from .theme import apply_theme

DEVICE_REFRESH_TIMEOUT_MS = 5000

_Window = TypeVar("_Window", bound=QtWidgets.QWidget)


class DeviceWorker(QtCore.QObject):
    finished = QtCore.Signal(object, str)

    def __init__(self, hostapi_filter: str | None, *, hard_refresh: bool = True):
        super().__init__()
        self._hostapi_filter = hostapi_filter
        self._hard_refresh = bool(hard_refresh)

    @QtCore.Slot()
    def run(self) -> None:
        try:
            devs = list_devices(
                hostapi_substring=self._hostapi_filter,
                hard_refresh=self._hard_refresh,
                validate=True,
            )
            if QtCore.QThread.currentThread().isInterruptionRequested():
                self.finished.emit([], "Device enumeration cancelled.")
                return
            self.finished.emit(devs, "")
        except Exception as e:
            self.finished.emit([], str(e))


class DeviceRefresh(QtCore.QObject):
    """Runs one audio device inventory at a time, off the Qt thread, for a window.

    PortAudio can stay blocked inside a driver. Past DEVICE_REFRESH_TIMEOUT_MS
    the window is told (``timed_out``) and the wait cursor goes away, but the
    inventory keeps running: a slow driver still delivers a usable, late result.

    Closing the window while an inventory runs: before the timeout the close
    waits for it (``close_must_wait``) and is replayed once it ends or times
    out; after the timeout the thread is abandoned (``abandon``) to the
    QApplication, and run_window() ends the process without destroying it,
    because Qt aborts on the destruction of a running QThread.
    """

    # (devices, error), delivered on the Qt thread, never while a close waits.
    inventory = QtCore.Signal(object, str)
    timed_out = QtCore.Signal()
    # The inventory thread ended: ``running`` is already False.
    finished = QtCore.Signal()

    def __init__(self, window: QtWidgets.QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._thread: QtCore.QThread | None = None
        # The only reference to the parentless worker: it keeps it alive.
        # Released by the next start(), long after its thread exited: freed
        # here as the thread finishes, it would race the thread's own exit.
        self._worker: DeviceWorker | None = None
        self._timed_out = False
        self._close_pending = False
        self._abandoned = False
        self._cursor_active = False
        self._timeout = QtCore.QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, hostapi_filter: str | None, *, hard_refresh: bool) -> None:
        if self._thread is not None or self._abandoned:
            return
        self._timed_out = False
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WaitCursor))
        self._cursor_active = True

        thread = QtCore.QThread(self)
        worker = DeviceWorker(hostapi_filter=hostapi_filter, hard_refresh=hard_refresh)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_inventory)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker

        self._timeout.start(DEVICE_REFRESH_TIMEOUT_MS)
        thread.start()

    def close_must_wait(self) -> bool:
        """For closeEvent: True while the close has to wait for the inventory."""
        if self._thread is None or not self._thread.isRunning() or self._timed_out:
            return False
        self._close_pending = True
        self._thread.requestInterruption()
        return True

    def abandon(self) -> bool:
        """For closeEvent: hand a hung inventory thread over to the QApplication.

        Returns True when one was still running: the window must then stay
        away from PortAudio, which that thread may still hold.
        """
        if self._thread is None or not self._thread.isRunning():
            return False
        logger.warning("closing while the audio device enumeration is still blocked")
        self._abandoned = True
        self._timeout.stop()
        self._thread.requestInterruption()
        self._thread.setParent(QtWidgets.QApplication.instance())
        self._restore_cursor()
        return True

    @QtCore.Slot(object, str)
    def _on_inventory(self, devs, error: str) -> None:
        if not self._close_pending and not self._abandoned:
            self.inventory.emit(devs, error)

    @QtCore.Slot()
    def _on_timeout(self) -> None:
        if self._thread is None:
            return
        logger.warning("audio device enumeration exceeds {} ms", DEVICE_REFRESH_TIMEOUT_MS)
        self._timed_out = True
        self._restore_cursor()
        if self._close_pending:
            QtCore.QTimer.singleShot(0, self._window.close)
        else:
            self.timed_out.emit()

    @QtCore.Slot()
    def _on_thread_finished(self) -> None:
        if self._abandoned:
            return
        self._timeout.stop()
        self._thread = None
        self._restore_cursor()
        if not self._close_pending:
            self.finished.emit()
        elif not self._timed_out:  # past the timeout, the close is already replayed
            self._close_pending = False
            QtCore.QTimer.singleShot(0, self._window.close)

    def _restore_cursor(self) -> None:
        if not self._cursor_active:
            return
        self._cursor_active = False
        with best_effort("restore the device refresh cursor"):
            QtWidgets.QApplication.restoreOverrideCursor()


def load_app_icon(role: str) -> QtGui.QIcon | None:
    """Logo of a role ("server", "client"; any other name gives the plain logo).

    The Régie and the Poste each have their own badge on the logo. Read from the
    sources or a PyInstaller bundle; None when no icon file is found.
    """
    names = [f"logo-{role}.ico", "logo.ico"]
    base = getattr(sys, "_MEIPASS", None)
    if base:
        folders = [Path(base) / "py_intercom" / "img", Path(base) / "img", Path(base)]
    else:
        folders = [Path(__file__).resolve().parents[1] / "img"]
    candidates = [folder / name for name in names for folder in folders]
    if not base:
        candidates.append(folders[0] / "logo.png")
    icon_path = next((path for path in candidates if path.is_file()), None)
    return None if icon_path is None else QtGui.QIcon(str(icon_path))


def set_app_icon(window: QtWidgets.QWidget, app_user_model_id: str) -> None:
    """Window and taskbar icon; the role comes from the AppUserModelID suffix ("...server")."""
    with best_effort("set window icon"):
        icon = load_app_icon(app_user_model_id.rsplit(".", 1)[-1])
        if icon is not None:
            window.setWindowIcon(icon)
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.setWindowIcon(icon)
    if sys.platform == "win32":
        with best_effort("set Windows AppUserModelID"):
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_user_model_id)


def confirm(parent: QtWidgets.QWidget | None, title: str, text: str) -> bool:
    """Ask before a destructive or live-cutting action; No is the default."""
    answer = QtWidgets.QMessageBox.question(
        parent,
        title,
        text,
        QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        QtWidgets.QMessageBox.StandardButton.No,
    )
    return answer == QtWidgets.QMessageBox.StandardButton.Yes


def install_french_translations(app: QtWidgets.QApplication) -> None:
    """Qt's own texts (Yes/No/Close on message boxes) in French, once per app."""
    if app.property("pyIntercomTranslated"):
        return
    with best_effort("load Qt French translations"):
        translator = QtCore.QTranslator(app)  # parented: lives as long as the app
        directory = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load(QtCore.QLocale(QtCore.QLocale.Language.French), "qtbase", "_", directory):
            app.installTranslator(translator)
    app.setProperty("pyIntercomTranslated", True)


def minimize_on_user_close(window: QtWidgets.QWidget, event: QtGui.QCloseEvent) -> bool:
    """For closeEvent: the operator's own close (X, Alt+F4) only minimizes.

    The intercom must not drop on a stray click on the X. Closes made by the
    program (the Quit button, the updater, Windows ending the session) are not
    spontaneous events and go through, so quitting stays possible.
    Returns True when the close was turned into a minimize: closeEvent returns.
    """
    if not event.spontaneous():
        return False
    event.ignore()
    window.showMinimized()
    return True


def make_quit_button(window: QtWidgets.QWidget) -> QtWidgets.QToolButton:
    """Header button (and Ctrl+Q) that really closes the window, since X only minimizes."""
    button = QtWidgets.QToolButton()
    button.setText("⏻")
    button.setProperty("class", "ghost")
    button.setFixedSize(34, 34)
    button.setToolTip("Quitter py-intercom (Ctrl+Q). La croix de la fenêtre ne fait que la réduire.")
    button.setAccessibleName("Quitter py-intercom")
    button.clicked.connect(window.close)
    shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Q"), window)
    shortcut.activated.connect(window.close)
    return button


def bring_to_front(window: QtWidgets.QWidget) -> None:
    """Restore a minimized window and give it the focus."""
    window.setWindowState(window.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
    window.show()
    window.raise_()
    window.activateWindow()


def run_window(
    make_window: Callable[[], _Window],
    start_minimized: Callable[[_Window], bool],
    single_instance_role: str | None = None,
) -> int:
    """Runs a py-intercom window until it closes and returns the exit code.

    ``single_instance_role``: only one window of that role per PC; a second
    launch brings the first one to the front and returns 0.
    """
    # The role picker may already have created the application.
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    guard = None
    if single_instance_role is not None:
        guard = SingleInstance(single_instance_role)
        if not guard.acquire():
            logger.info("a {} is already running on this PC: leaving", single_instance_role)
            return 0
    install_french_translations(app)
    apply_theme(app)
    window = make_window()
    if guard is not None:
        guard.activated.connect(lambda: bring_to_front(window))
    if start_minimized(window):
        window.showMinimized()
    else:
        window.show()
    code = int(app.exec())
    if any(thread.isRunning() for thread in app.findChildren(QtCore.QThread)):
        # A device inventory abandoned by DeviceRefresh is still blocked in a
        # driver: destroying its QThread would abort the process. The window
        # already stopped the engine and flushed its preset.
        logger.warning("exiting with an audio driver call still blocked")
        with best_effort("flush logs"):
            logger.complete()
        os._exit(code)
    return code


def is_checked(state) -> bool:
    try:
        state_val = int(state.value) if hasattr(state, "value") else int(state)
    except Exception:
        return False

    try:
        checked_val = int(QtCore.Qt.CheckState.Checked.value)
    except Exception:
        checked_val = 2

    return int(state_val) == int(checked_val)
