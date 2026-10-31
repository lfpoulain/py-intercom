"""One executable for both roles: the Régie (server) or a Poste (client).

Each role still runs in its own process: the server and a client on the same
PC never share an interpreter, a GIL or a crash.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROLES = ("server", "client")
LOCAL_SERVER_HOST = "127.0.0.1"

_SETTINGS_SCOPE = ("py-intercom", "launcher")
_ROLE_KEY = "role"
# Running from source: the entry point that prepares opus.dll before the imports.
_SOURCE_ENTRYPOINT = Path(__file__).resolve().parents[2] / "run_intercom.py"

# Names the crash report: server_crash.txt or client_crash.txt once a role runs.
active_role = "intercom"


def split_role(argv: list[str]) -> tuple[str | None, list[str]]:
    """The --role option, if any, and the arguments left for that role."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--role", choices=ROLES)
    args, rest = parser.parse_known_args(argv)
    return args.role, rest


def role_command(role: str, *extra: str) -> list[str]:
    """Command line that starts this same program in the given role."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--role", role, *extra]
    return [sys.executable, str(_SOURCE_ENTRYPOINT), "--role", role, *extra]


def open_local_client() -> subprocess.Popen:
    """Start a Poste on this PC, already pointed at the local Régie."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    env = None
    if getattr(sys, "frozen", False):
        # Without this, PyInstaller runs the child from the Régie's own unpack
        # folder, which is deleted when the Régie closes: the Poste must
        # outlive it, so it unpacks its own copy.
        env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    return subprocess.Popen(
        role_command("client", "--server", LOCAL_SERVER_HOST),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        env=env,
    )


def choose_role() -> str | None:
    """The remembered role, or the operator's pick; None when they cancel.

    Holding Shift at launch asks again even when a role is remembered.
    """
    from PySide6 import QtCore, QtGui, QtWidgets

    from .common.gui_utils import install_french_translations
    from .common.theme import apply_theme

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    settings = QtCore.QSettings(*_SETTINGS_SCOPE)
    remembered = str(settings.value(_ROLE_KEY, "") or "")
    shift_held = bool(QtGui.QGuiApplication.queryKeyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
    if remembered in ROLES and not shift_held:
        return remembered

    install_french_translations(app)
    apply_theme(app)
    from .common.role_dialog import RoleDialog

    dialog = RoleDialog(remembered if remembered in ROLES else None)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted or dialog.role is None:
        return None
    if dialog.remember:
        settings.setValue(_ROLE_KEY, dialog.role)
    else:
        settings.remove(_ROLE_KEY)
    return dialog.role


def main() -> int:
    global active_role
    role, rest = split_role(sys.argv[1:])
    if role is None:
        role = choose_role()
        if role is None:
            return 0
    active_role = role
    sys.argv = [f"py-intercom-{role}", *rest]
    if role == "server":
        from .server.main import main as role_main
    else:
        from .client.main import main as role_main
    return role_main()
