"""One window per role on a PC: a second launch wakes the first one up and ends.

Two Régies would fight over the UDP port, two Postes over the preset, the
identity (one evicts the other's session) and the HTTP control port. A local
socket named after the role and the Windows user is the lock: it is released by
the system when the process ends, however it ends, so a crash never leaves a
stale lock behind.
"""

from __future__ import annotations

import getpass
import sys

from PySide6 import QtCore, QtNetwork

from .logging import best_effort

_PING_TIMEOUT_MS = 500


def _socket_name(role: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"py-intercom-{role}-{user}"


def _allow_foreground_for_anyone() -> None:
    """Let the window of the first instance come to the front.

    The second process was just started by the operator, so it holds the right
    to take the foreground; Windows only lets it pass that right on explicitly.
    """
    if sys.platform != "win32":
        return
    with best_effort("allow the running instance to take the foreground"):
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY


class SingleInstance(QtCore.QObject):
    """Holds the role's socket; ``activated`` fires when another launch pings it."""

    activated = QtCore.Signal()

    def __init__(self, role: str) -> None:
        super().__init__()
        self._name = _socket_name(role)
        self._server: QtNetwork.QLocalServer | None = None

    def acquire(self) -> bool:
        """True when this process is the only one of its role, False when another runs.

        In the second case the running one has been asked to show its window.
        """
        if self._ping_running_instance():
            return False
        QtNetwork.QLocalServer.removeServer(self._name)  # leftover file of a crash (POSIX only)
        server = QtNetwork.QLocalServer(self)
        server.setSocketOptions(QtNetwork.QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._name):
            # Two launches at the same moment: the other one listened first.
            if self._ping_running_instance():
                return False
            # Nothing answers and nothing can be bound: run unguarded rather
            # than leave the operator without an intercom.
            return True
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def _ping_running_instance(self) -> bool:
        socket = QtNetwork.QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(_PING_TIMEOUT_MS):
            return False
        _allow_foreground_for_anyone()
        socket.write(b"show\n")
        socket.waitForBytesWritten(_PING_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def _on_connection(self) -> None:
        server = self._server
        while server is not None and server.hasPendingConnections():
            peer = server.nextPendingConnection()
            peer.disconnected.connect(peer.deleteLater)
            self.activated.emit()
