from __future__ import annotations

import sys
from types import ModuleType

import pytest


def _fake_gui_module(module_name: str, calls: list[bool], return_code: int) -> ModuleType:
    module = ModuleType(module_name)

    def run_gui(*, minimized: bool = False) -> int:
        calls.append(bool(minimized))
        return return_code

    module.run_gui = run_gui  # type: ignore[attr-defined]
    return module


def test_client_entrypoint_always_launches_gui(monkeypatch) -> None:
    from py_intercom.client import main as client_main

    calls: list[bool] = []
    monkeypatch.setitem(
        sys.modules,
        "py_intercom.client.gui",
        _fake_gui_module("py_intercom.client.gui", calls, 17),
    )
    monkeypatch.setattr(client_main, "setup_logging", lambda _debug, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["py-intercom-client", "--minimized"])

    assert client_main.main() == 17
    assert calls == [True]


def test_server_entrypoint_always_launches_gui(monkeypatch) -> None:
    from py_intercom.server import main as server_main

    calls: list[bool] = []
    monkeypatch.setitem(
        sys.modules,
        "py_intercom.server.gui",
        _fake_gui_module("py_intercom.server.gui", calls, 23),
    )
    monkeypatch.setattr(server_main, "setup_logging", lambda _debug, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["py-intercom-server"])

    assert server_main.main() == 23
    assert calls == [False]


@pytest.mark.parametrize(
    ("module_name", "legacy_args"),
    [
        ("py_intercom.client.main", ["--gui"]),
        ("py_intercom.client.main", ["--server-ip", "127.0.0.1"]),
        ("py_intercom.server.main", ["--gui"]),
        ("py_intercom.server.main", ["--bind-ip", "127.0.0.1"]),
    ],
)
def test_legacy_headless_arguments_are_rejected(
    monkeypatch,
    module_name: str,
    legacy_args: list[str],
) -> None:
    module = __import__(module_name, fromlist=["main"])
    monkeypatch.setattr(sys, "argv", [module_name, *legacy_args])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
