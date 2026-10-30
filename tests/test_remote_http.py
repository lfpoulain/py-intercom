from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import socket
import threading
import time
from http import HTTPStatus

import pytest
from PySide6 import QtCore, QtWidgets

from py_intercom.client import remote
from py_intercom.client.remote import QtDispatcher, RemoteCommand, RemoteControlServer, RemoteReply


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _ok(command: RemoteCommand) -> RemoteReply:
    return RemoteReply(HTTPStatus.OK, {"action": command.action})


# --- Listening socket -------------------------------------------------------------


def test_a_second_server_cannot_share_the_port() -> None:
    first = RemoteControlServer(_ok, port=0)
    try:
        with pytest.raises(OSError):
            RemoteControlServer(_ok, port=first.port)
    finally:
        first.stop()


def test_the_port_can_be_reopened_after_stop() -> None:
    first = RemoteControlServer(_ok, port=0)
    first.start()
    port = first.port
    first.stop()

    second = RemoteControlServer(_ok, port=port)
    second.stop()


def test_an_idle_connection_is_dropped(monkeypatch) -> None:
    monkeypatch.setattr(remote._Handler, "timeout", 0.2)
    server = RemoteControlServer(_ok, port=0)
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=5.0) as idle:
            started = time.monotonic()
            assert idle.recv(1024) == b""  # closed by the server, nothing sent
            assert time.monotonic() - started < 4.0
    finally:
        server.stop()


# --- Qt bridge ----------------------------------------------------------------------


def _call_in_thread(dispatcher: QtDispatcher, command: RemoteCommand) -> tuple[threading.Thread, list[RemoteReply]]:
    replies: list[RemoteReply] = []
    worker = threading.Thread(target=lambda: replies.append(dispatcher(command)))
    worker.start()
    return worker, replies


def test_a_command_answered_503_never_runs_later(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(remote, "_DISPATCH_TIMEOUT_S", 0.05)
    ran: list[RemoteCommand] = []
    dispatcher = QtDispatcher(lambda command: ran.append(command) or _ok(command))

    # The Qt thread is busy: nothing processes the queued job before the timeout.
    worker, replies = _call_in_thread(dispatcher, RemoteCommand("toggle", 1))
    worker.join(timeout=2.0)
    assert replies[0].status == HTTPStatus.SERVICE_UNAVAILABLE

    qt_app.processEvents()  # the Qt thread is free again

    assert ran == []


def test_a_command_already_running_returns_its_own_outcome(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(remote, "_DISPATCH_TIMEOUT_S", 0.05)
    queued = threading.Event()
    dispatcher = QtDispatcher(lambda command: (time.sleep(0.3), _ok(command))[1])
    dispatcher._requested.connect(lambda _job: queued.set(), QtCore.Qt.ConnectionType.DirectConnection)

    worker, replies = _call_in_thread(dispatcher, RemoteCommand("on", 0))
    assert queued.wait(2.0)
    qt_app.processEvents()  # runs the slow handler past the dispatch timeout
    worker.join(timeout=2.0)

    assert replies[0].status == HTTPStatus.OK
    assert replies[0].body == {"action": "on"}
