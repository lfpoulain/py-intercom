from __future__ import annotations

import subprocess
import sys
from types import ModuleType

import pytest
from PySide6 import QtWidgets

from py_intercom import launcher
from py_intercom.common.role_dialog import RoleDialog


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_role_option_is_split_from_the_role_arguments() -> None:
    assert launcher.split_role(["--role", "client", "--minimized"]) == ("client", ["--minimized"])
    assert launcher.split_role(["--debug"]) == (None, ["--debug"])


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc_info:
        launcher.split_role(["--role", "studio"])
    assert exc_info.value.code == 2


def test_role_command_relaunches_the_frozen_executable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/apps/py-intercom.exe")

    assert launcher.role_command("client", "--server", "127.0.0.1") == [
        "C:/apps/py-intercom.exe",
        "--role",
        "client",
        "--server",
        "127.0.0.1",
    ]


def test_role_command_from_source_goes_through_the_entry_script(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    command = launcher.role_command("server")

    assert command[0] == sys.executable
    assert command[1].endswith("run_intercom.py")
    assert command[2:] == ["--role", "server"]


def test_local_client_points_at_this_pc(monkeypatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **_kwargs: started.append(command))

    launcher.open_local_client()

    assert started[0][-4:] == ["--role", "client", "--server", "127.0.0.1"]


@pytest.mark.parametrize("role", launcher.ROLES)
def test_main_runs_the_requested_role_with_its_own_arguments(monkeypatch, role: str) -> None:
    seen: list[list[str]] = []
    module = ModuleType(f"py_intercom.{role}.main")
    module.main = lambda: seen.append(list(sys.argv)) or 7  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"py_intercom.{role}.main", module)
    monkeypatch.setattr(launcher, "choose_role", lambda: pytest.fail("a role was given"))
    monkeypatch.setattr(sys, "argv", ["py-intercom", "--role", role, "--minimized"])

    assert launcher.main() == 7
    assert seen == [[f"py-intercom-{role}", "--minimized"]]


def test_main_asks_for_the_role_and_quits_when_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "choose_role", lambda: None)
    monkeypatch.setattr(sys, "argv", ["py-intercom"])

    assert launcher.main() == 0


def test_role_dialog_returns_the_clicked_role(app) -> None:
    dialog = RoleDialog("client")
    assert dialog.remember

    dialog.buttons["server"].click()

    assert dialog.role == "server"
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    dialog.deleteLater()


def test_role_dialog_does_not_remember_by_default(app) -> None:
    dialog = RoleDialog()
    assert not dialog.remember
    dialog.deleteLater()


def test_local_client_of_the_frozen_exe_unpacks_its_own_copy(monkeypatch) -> None:
    seen: list[dict | None] = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda _command, **kwargs: seen.append(kwargs["env"]))

    launcher.open_local_client()

    assert seen[0]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_local_client_from_source_inherits_the_environment(monkeypatch) -> None:
    seen: list[dict | None] = []
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda _command, **kwargs: seen.append(kwargs["env"]))

    launcher.open_local_client()

    assert seen == [None]


def test_crash_report_is_named_after_the_running_role(monkeypatch) -> None:
    module = ModuleType("py_intercom.client.main")
    module.main = lambda: launcher.active_role  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "py_intercom.client.main", module)
    monkeypatch.setattr(launcher, "active_role", "intercom")
    monkeypatch.setattr(sys, "argv", ["py-intercom", "--role", "client"])

    assert launcher.main() == "client"
