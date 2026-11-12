"""Update button of both windows: offers the latest GitHub release and installs it.

Nothing happens on its own during a show: the check runs in the background a
little after start-up (then every few hours), a button appears in the header
when a newer release exists, and the install only starts when the operator
clicks it and confirms.
"""

from __future__ import annotations

import threading
from pathlib import Path

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .. import __version__, launcher
from . import updater
from .logging import best_effort

_FIRST_CHECK_MS = 20_000
_RECHECK_MS = 6 * 60 * 60 * 1000


class UpdateController(QtCore.QObject):
    """Owns the update button of one window; ``role`` is "server" or "client"."""

    _checked = QtCore.Signal(object, object)  # ReleaseInfo | None, error text | None
    _progress = QtCore.Signal(int, int)
    _downloaded = QtCore.Signal(object, object)  # (Path, sha256) | None, error text | None

    def __init__(self, window: QtWidgets.QWidget, role: str) -> None:
        super().__init__(window)
        self._window = window
        self._role = role
        self._release: updater.ReleaseInfo | None = None
        self._checking = False
        self._cancel: threading.Event | None = None
        self._dialog: QtWidgets.QProgressDialog | None = None
        self._installer: Path | None = None
        self._installer_sha = ""
        self._installer_version = ""
        self._cleaned = False

        self.button = QtWidgets.QPushButton("⬆  Mise à jour")
        self.button.setProperty("class", "primary")
        self.button.setVisible(False)
        self.button.clicked.connect(self._offer)

        self._checked.connect(self._on_checked)
        self._progress.connect(self._on_progress)
        self._downloaded.connect(self._on_downloaded)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_RECHECK_MS)
        self._timer.timeout.connect(self.check)

    def start(self) -> None:
        """Schedule the checks, unless this build or the environment opts out."""
        if not updater.checks_enabled():
            return
        QtCore.QTimer.singleShot(_FIRST_CHECK_MS, self.check)
        self._timer.start()

    def check(self) -> None:
        if self._checking or self._cancel is not None:
            return
        self._checking = True
        threading.Thread(target=self._check_worker, name="update-check", daemon=True).start()

    def _check_worker(self) -> None:
        release, error = None, None
        if not self._cleaned:
            # The first check runs before any download of this session: what is
            # left in the update folder comes from an earlier one.
            self._cleaned = True
            updater.cleanup_update_dir()
        try:
            release = updater.fetch_latest_release()
        except updater.UpdateError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        with best_effort("emit update check"):
            self._checked.emit(release, error)

    def _on_checked(self, release, error) -> None:
        self._checking = False
        if error:
            logger.debug("update check failed: {}", error)
            return
        if release is None or not updater.is_newer(release.version, __version__):
            return
        self._release = release
        self.button.setText(f"⬆  Version {release.version}")
        self.button.setToolTip(f"Une nouvelle version de py-intercom est disponible (vous avez la {__version__}).")
        self.button.setVisible(True)

    # -- Offer, download, install ---------------------------------------------

    def _offer(self) -> None:
        release = self._release
        if release is None or self._cancel is not None:
            return
        if not updater.is_installed_build():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(release.page_url))
            return
        if self._other_instance_running():
            return
        box = QtWidgets.QMessageBox(self._window)
        box.setWindowTitle("Mise à jour de py-intercom")
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        box.setText(f"La version {release.version} est disponible (vous avez la {__version__}).")
        closing = "La Régie s'arrêtera" if self._role == "server" else "Ce poste se déconnectera"
        box.setInformativeText(
            f"py-intercom télécharge la mise à jour, vérifie son intégrité, puis se ferme et s'installe seul. "
            f"{closing} pendant quelques secondes ; Windows demandera une autorisation. "
            "Mieux vaut le faire hors direct."
        )
        install = box.addButton("Télécharger et installer", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        notes = box.addButton("Nouveautés", QtWidgets.QMessageBox.ButtonRole.HelpRole)
        box.addButton("Plus tard", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(install)
        box.exec()
        clicked = box.clickedButton()
        if clicked is notes:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(release.page_url))
        elif clicked is install:
            self._download(release)

    def _other_instance_running(self) -> bool:
        """Tell the operator to close the other py-intercom of this PC first.

        The installer replaces files that every py-intercom program of the PC
        holds, and it closes them: a Régie and its local Poste are two programs,
        and updating from one would shut the other down without its own prompt.
        """
        if not updater.other_instances():
            return False
        QtWidgets.QMessageBox.information(
            self._window,
            "Mise à jour de py-intercom",
            "Un autre py-intercom (la Régie ou un poste) tourne sur ce PC.\n\n"
            "Quittez-le d'abord (bouton ⏻ ou Ctrl+Q : la croix ne fait que réduire la fenêtre), puis relancez la mise à jour depuis cette fenêtre : "
            "l'installation arrêterait sinon les deux programmes.",
        )
        return True

    def _download(self, release: updater.ReleaseInfo) -> None:
        if self._installer is not None and self._installer_version == release.version and self._installer.is_file():
            self._install_and_quit()  # downloaded and verified earlier, the install was postponed
            return
        self._cancel = threading.Event()
        self.button.setEnabled(False)
        dialog = QtWidgets.QProgressDialog("Téléchargement de la mise à jour…", "Annuler", 0, 100, self._window)
        dialog.setWindowTitle("Mise à jour de py-intercom")
        dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.canceled.connect(self._cancel_download)
        self._dialog = dialog
        threading.Thread(
            target=self._download_worker, args=(release, self._cancel), name="update-download", daemon=True
        ).start()

    def _cancel_download(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _download_worker(self, release: updater.ReleaseInfo, cancel: threading.Event) -> None:
        result, error = None, None
        try:
            path = updater.download_installer(release, lambda done, total: self._emit_progress(done, total), cancel)
            result = (path, updater.file_sha256(path))
        except updater.UpdateError as exc:
            error = str(exc)
        except Exception as exc:
            logger.exception("update download failed")
            error = f"Erreur inattendue : {type(exc).__name__}"
        with best_effort("emit update download"):
            self._downloaded.emit(result, error)

    def _emit_progress(self, done: int, total: int) -> None:
        with best_effort("emit update progress"):
            self._progress.emit(int(done), int(total))

    def _on_progress(self, done: int, total: int) -> None:
        if self._dialog is not None and total > 0:
            self._dialog.setValue(min(99, int(done * 100 / total)))
            self._dialog.setLabelText(
                f"Téléchargement de la mise à jour… {done // (1024 * 1024)} / {total // (1024 * 1024)} Mo"
            )

    def _end_download(self) -> None:
        self._cancel = None
        self.button.setEnabled(True)
        if self._dialog is not None:
            dialog, self._dialog = self._dialog, None
            dialog.canceled.disconnect(self._cancel_download)
            dialog.close()
            dialog.deleteLater()

    def _on_downloaded(self, result, error) -> None:
        cancelled = self._cancel is not None and self._cancel.is_set()
        self._end_download()
        if result is None:
            if not cancelled:
                QtWidgets.QMessageBox.warning(
                    self._window,
                    "Mise à jour de py-intercom",
                    f"{error or 'La mise à jour a échoué.'}\n\nVotre version actuelle n'a pas été modifiée.",
                )
            return
        path, sha256 = result
        self._installer = Path(path)
        self._installer_sha = str(sha256)
        self._installer_version = self._release.version if self._release is not None else ""
        if cancelled:
            # "Annuler" was clicked as the download ended: nothing is installed
            # (the file is kept, a new click on the button installs it).
            logger.info("update install cancelled by the operator: {}", self._installer)
            return
        self._install_and_quit()

    def _install_and_quit(self) -> None:
        """Close this window (its own safety prompt applies), then hand over to the installer."""
        if self._installer is None or not self._installer.is_file():
            self._installer = None
            return
        if self._other_instance_running():
            return
        # close() returns False when the operator keeps the window open, or
        # while an audio inventory runs: the installer then is not started.
        if not self._window.close():
            logger.info("update downloaded, install postponed: {}", self._installer)
            return
        try:
            # Last moment before running it elevated: still the verified file?
            updater.verify_installer(self._installer, self._installer_sha)
            updater.launch_installer(self._installer, self._role)
        except updater.UpdateError as exc:
            logger.error("update installer not started: {}", exc)
            self._installer = None
            # The window is gone and its engine stopped: say why, then bring the
            # same role back rather than leave the PC without an intercom. The
            # new process starts after a short delay, once this one has released
            # the Poste's single-instance lock.
            QtWidgets.QMessageBox.warning(
                None,
                "Mise à jour de py-intercom",
                f"{exc}\n\nRien n'a été modifié : py-intercom se rouvre dans la version actuelle.",
            )
            with best_effort("restart after a refused update"):
                launcher.restart_role(self._role, delay_s=2.0)
        QtWidgets.QApplication.quit()
