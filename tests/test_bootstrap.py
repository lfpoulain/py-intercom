from __future__ import annotations

import os
from pathlib import Path

import pytest

from py_intercom import __version__
from py_intercom.common import bootstrap


def test_package_exposes_version() -> None:
    assert __version__ == "2.0.0.dev0"


def test_prepare_windows_runtime_is_noop_off_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: False)

    result = bootstrap.prepare_windows_runtime(tmp_path, environ={"PATH": "original"})

    assert result == bootstrap.WindowsRuntime()


def test_prepare_windows_runtime_registers_and_loads_first_available_opus(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "application"
    meipass = tmp_path / "bundle"
    root_bin = root / "bin"
    bundle_bin = meipass / "bin"
    root_bin.mkdir(parents=True)
    bundle_bin.mkdir(parents=True)
    bundled_opus = bundle_bin / "opus.dll"
    bundled_opus.write_bytes(b"test")
    (root_bin / "opus.dll").write_bytes(b"fallback")

    handles: list[object] = []
    registered: list[str] = []
    loaded: list[str] = []

    def add_dll_directory(path: str) -> object:
        registered.append(path)
        handle = object()
        handles.append(handle)
        return handle

    def load_library(path: str) -> object:
        loaded.append(path)
        return object()

    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap.os, "add_dll_directory", add_dll_directory, raising=False)
    monkeypatch.setattr(bootstrap.ctypes, "CDLL", load_library)
    monkeypatch.setattr(bootstrap, "_DLL_DIRECTORY_HANDLES", [])
    monkeypatch.setattr(bootstrap, "_LOADED_LIBRARIES", [])
    environ = {"PATH": os.fspath(root_bin) + os.pathsep + "original"}

    result = bootstrap.prepare_windows_runtime(root, meipass=meipass, environ=environ)

    expected_directories = (meipass.resolve(), root.resolve(), root_bin.resolve())
    assert result.dll_directories == expected_directories
    assert result.opus_path == bundled_opus.resolve()
    assert registered == [os.fspath(path) for path in expected_directories]
    assert loaded == [os.fspath(bundled_opus.resolve())]
    assert bootstrap._DLL_DIRECTORY_HANDLES == handles
    assert len(bootstrap._LOADED_LIBRARIES) == 1
    assert environ["PATH"].split(os.pathsep) == [
        *(os.fspath(path) for path in expected_directories),
        "original",
    ]


def test_write_crash_report_sanitizes_component_and_appends(tmp_path) -> None:
    first = bootstrap.write_crash_report("../client!", "first", directory=tmp_path)
    second = bootstrap.write_crash_report("../client!", "second\n", directory=tmp_path)

    assert first == second == tmp_path / "client_crash.txt"
    assert first.read_text(encoding="utf-8") == "first\n\nsecond\n\n"


def test_run_application_returns_entrypoint_code_as_system_exit(tmp_path) -> None:
    with pytest.raises(SystemExit) as raised:
        bootstrap.run_application(
            lambda: 7,
            component="client",
            frozen=False,
            crash_directory=tmp_path,
        )

    assert raised.value.code == 7
    assert list(tmp_path.iterdir()) == []


def test_run_application_reports_and_reraises_original_exception(tmp_path) -> None:
    error = RuntimeError("boom")

    def fail() -> int:
        raise error

    with pytest.raises(RuntimeError) as raised:
        bootstrap.run_application(
            fail,
            component="server",
            frozen=False,
            crash_directory=tmp_path,
        )

    assert raised.value is error
    report = tmp_path / "server_crash.txt"
    contents = report.read_text(encoding="utf-8")
    assert "RuntimeError: boom" in contents
    assert "in fail" in contents


def test_crash_report_failure_does_not_mask_original_exception(tmp_path) -> None:
    occupied_path = tmp_path / "not-a-directory"
    occupied_path.write_text("occupied", encoding="utf-8")
    error = RuntimeError("original")

    def fail() -> int:
        raise error

    with pytest.raises(RuntimeError) as raised:
        bootstrap.run_application(
            fail,
            component="client",
            frozen=False,
            crash_directory=occupied_path,
        )

    assert raised.value is error
    assert occupied_path.read_text(encoding="utf-8") == "occupied"


def test_run_application_preserves_explicit_system_exit(tmp_path) -> None:
    def stop() -> int:
        raise SystemExit(12)

    with pytest.raises(SystemExit) as raised:
        bootstrap.run_application(
            stop,
            component="client",
            frozen=False,
            crash_directory=tmp_path,
        )

    assert raised.value.code == 12
    assert list(tmp_path.iterdir()) == []
