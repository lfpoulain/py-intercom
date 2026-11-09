import hashlib
import io
import threading

import pytest
from PySide6 import QtWidgets

from py_intercom.common import update_ui, updater
from py_intercom.common.updater import ReleaseInfo, UpdateError


@pytest.fixture(scope="module")
def qt_app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


PAYLOAD = b"installer-bytes" * 5000
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
GOOD_URL = "https://github.com/lfpoulain/py-intercom/releases/download/v3.1.4/py-intercom-setup.exe"


def _release_json(**overrides):
    payload = {
        "tag_name": "v3.1.4",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/lfpoulain/py-intercom/releases/tag/v3.1.4",
        "assets": [
            {
                "name": "py-intercom-setup.exe",
                "browser_download_url": GOOD_URL,
                "size": len(PAYLOAD),
                "digest": f"sha256:{PAYLOAD_SHA}",
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": "https://github.com/lfpoulain/py-intercom/releases/download/v3.1.4/SHA256SUMS.txt",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _release(**overrides) -> ReleaseInfo:
    base = {
        "version": "3.1.4",
        "page_url": "https://github.com/lfpoulain/py-intercom/releases/tag/v3.1.4",
        "installer_url": GOOD_URL,
        "installer_size": len(PAYLOAD),
        "sha256": PAYLOAD_SHA,
        "checksums_url": None,
    }
    base.update(overrides)
    return ReleaseInfo(**base)


class _Response:
    def __init__(self, data: bytes, headers: dict | None = None) -> None:
        self._stream = io.BytesIO(data)
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


# --- Versions ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("3.0.10", "3.0.9", True),
        ("v3.1.0", "3.0.99", True),
        ("3.0.9", "3.0.9", False),
        ("3.0.8", "3.0.9", False),
        ("4.0", "3.9.9", True),
        ("3.0", "3.0.0", False),
        ("3.0.9-beta", "3.0.0", False),
        ("latest", "3.0.0", False),
        ("3.0.9", "dev", False),
    ],
)
def test_is_newer_compares_numeric_versions(candidate, current, expected) -> None:
    assert updater.is_newer(candidate, current) is expected


def test_parse_version_rejects_anything_but_a_plain_version() -> None:
    assert updater.parse_version("v3.0.9") == (3, 0, 9)
    assert updater.parse_version(" 3.1 ") == (3, 1)
    assert updater.parse_version("3.0.9; rm -rf") is None
    assert updater.parse_version("") is None


# --- Release document -------------------------------------------------------


def test_release_from_json_reads_installer_digest_and_page() -> None:
    release = updater.release_from_json(_release_json())

    assert release is not None
    assert release.version == "3.1.4"
    assert release.installer_url == GOOD_URL
    assert release.installer_size == len(PAYLOAD)
    assert release.sha256 == PAYLOAD_SHA
    assert release.checksums_url is not None


def test_release_without_installer_draft_or_prerelease_is_ignored() -> None:
    assert updater.release_from_json(_release_json(assets=[])) is None
    assert updater.release_from_json(_release_json(draft=True)) is None
    assert updater.release_from_json(_release_json(prerelease=True)) is None
    assert updater.release_from_json(_release_json(tag_name="nightly")) is None


def test_release_with_a_download_outside_github_is_ignored() -> None:
    payload = _release_json()
    payload["assets"][0]["browser_download_url"] = "https://evil.example.com/py-intercom-setup.exe"
    assert updater.release_from_json(payload) is None
    payload["assets"][0]["browser_download_url"] = "http://github.com/x/py-intercom-setup.exe"
    assert updater.release_from_json(payload) is None


def test_a_page_url_outside_github_falls_back_to_the_releases_page() -> None:
    release = updater.release_from_json(_release_json(html_url="https://evil.example.com/x"))
    assert release is not None
    assert release.page_url.startswith("https://github.com/lfpoulain/py-intercom")


def test_only_https_github_hosts_are_allowed() -> None:
    assert updater._host_allowed("https://github.com/a/b")
    assert updater._host_allowed("https://objects.githubusercontent.com/x")
    assert not updater._host_allowed("https://notgithub.com/x")
    assert not updater._host_allowed("https://github.com.evil.example/x")
    assert not updater._host_allowed("http://github.com/x")
    assert not updater._host_allowed("file:///C:/x")


def test_redirects_leaving_github_are_refused() -> None:
    handler = updater._CheckedRedirects()
    with pytest.raises(UpdateError):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/setup.exe")


def test_parse_checksums_finds_the_installer_line() -> None:
    text = f"{'a' * 64}  other.zip\n{PAYLOAD_SHA.upper()}  py-intercom-setup.exe\n"
    assert updater.parse_checksums(text, "py-intercom-setup.exe") == PAYLOAD_SHA
    assert updater.parse_checksums("garbage\n", "py-intercom-setup.exe") is None
    assert updater.parse_checksums(f"{'z' * 64}  py-intercom-setup.exe", "py-intercom-setup.exe") is None


def test_fetch_latest_release_reports_errors_in_plain_french(monkeypatch) -> None:
    def refused(*args, **kwargs):
        raise OSError("no route")

    monkeypatch.setattr(updater, "_open", refused)
    with pytest.raises(UpdateError, match="injoignable"):
        updater.fetch_latest_release()

    monkeypatch.setattr(updater, "_open", lambda *a, **k: _Response(b"<html>not json"))
    with pytest.raises(UpdateError, match="illisible"):
        updater.fetch_latest_release()


# --- Download ---------------------------------------------------------------


def test_download_checks_the_hash_then_returns_the_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(updater, "_open", lambda *a, **k: _Response(PAYLOAD))
    seen = []

    path = updater.download_installer(
        _release(), lambda done, total: seen.append((done, total)), directory=tmp_path / "dl"
    )

    assert path.read_bytes() == PAYLOAD
    assert path.name == "py-intercom-setup-3.1.4.exe"
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD))
    assert not list(path.parent.glob("*.part"))


def test_a_corrupted_download_is_refused_and_leaves_nothing(monkeypatch, tmp_path) -> None:
    corrupted = bytearray(PAYLOAD)
    corrupted[10] ^= 0xFF
    monkeypatch.setattr(updater, "_open", lambda *a, **k: _Response(bytes(corrupted)))

    with pytest.raises(UpdateError, match="corrompu"):
        updater.download_installer(_release(), directory=tmp_path / "dl")

    assert list((tmp_path / "dl").iterdir()) == []


def test_a_truncated_download_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(updater, "_open", lambda *a, **k: _Response(PAYLOAD[:-100]))

    with pytest.raises(UpdateError, match="incomplet"):
        updater.download_installer(_release(), directory=tmp_path / "dl")

    assert list((tmp_path / "dl").iterdir()) == []


def test_an_oversized_download_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(updater, "MAX_INSTALLER_BYTES", 1000)
    monkeypatch.setattr(updater, "_open", lambda *a, **k: _Response(PAYLOAD))

    with pytest.raises(UpdateError, match="gros"):
        updater.download_installer(_release(installer_size=0), directory=tmp_path / "dl")


def test_cancelling_stops_the_download(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(updater, "_open", lambda *a, **k: _Response(PAYLOAD))
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(UpdateError, match="annulé"):
        updater.download_installer(_release(), cancel=cancel, directory=tmp_path / "dl")

    assert list((tmp_path / "dl").iterdir()) == []


def test_no_checksum_means_no_install(tmp_path) -> None:
    with pytest.raises(UpdateError, match="somme de contrôle"):
        updater.download_installer(_release(sha256=None, checksums_url=None), directory=tmp_path / "dl")


def test_the_checksum_can_come_from_the_release_listing(monkeypatch, tmp_path) -> None:
    listing = f"{PAYLOAD_SHA}  py-intercom-setup.exe\n".encode()

    def fake_open(url, *args, **kwargs):
        return _Response(listing if url.endswith("SHA256SUMS.txt") else PAYLOAD)

    monkeypatch.setattr(updater, "_open", fake_open)
    release = _release(sha256=None, checksums_url="https://github.com/x/y/releases/download/v3.1.4/SHA256SUMS.txt")

    assert updater.download_installer(release, directory=tmp_path / "dl").read_bytes() == PAYLOAD


# --- Switches and installer command ----------------------------------------


def test_checks_run_from_the_installed_build_unless_disabled(monkeypatch) -> None:
    monkeypatch.delenv(updater.ENV_DISABLE, raising=False)
    monkeypatch.delenv(updater.ENV_FORCE, raising=False)
    monkeypatch.setattr(updater, "is_installed_build", lambda: False)
    assert updater.checks_enabled() is False  # running from the sources

    monkeypatch.setenv(updater.ENV_FORCE, "1")
    assert updater.checks_enabled() is True

    monkeypatch.setattr(updater, "is_installed_build", lambda: True)
    monkeypatch.delenv(updater.ENV_FORCE)
    assert updater.checks_enabled() is True
    monkeypatch.setenv(updater.ENV_DISABLE, "1")
    assert updater.checks_enabled() is False


def test_installer_runs_silently_and_relaunches_the_same_role() -> None:
    assert "/SILENT" in updater.installer_arguments(None)
    assert "/RELAUNCH" not in updater.installer_arguments(None)
    assert updater.installer_arguments("server").endswith("/RELAUNCH=server")
    assert updater.installer_arguments("client").endswith("/RELAUNCH=client")
    assert "/RELAUNCH" not in updater.installer_arguments("server && calc")


# --- Button -----------------------------------------------------------------


def test_the_button_appears_only_for_a_newer_release(qt_app, monkeypatch) -> None:
    monkeypatch.setattr(update_ui, "__version__", "3.0.9")
    window = QtWidgets.QWidget()
    controller = update_ui.UpdateController(window, "client")

    controller._on_checked(_release(version="3.0.9"), None)
    assert controller.button.isHidden()
    controller._on_checked(None, "GitHub est injoignable")
    assert controller.button.isHidden()
    controller._on_checked(_release(version="3.0.10"), None)
    assert not controller.button.isHidden()
    assert "3.0.10" in controller.button.text()
    window.deleteLater()


class _Window(QtWidgets.QWidget):
    closes = True

    def close(self) -> bool:
        return self.closes


def _controller(monkeypatch, tmp_path, *, role="server", closes=True, others=0, content=b"installer"):
    """A controller holding a downloaded installer, with the Windows side faked."""
    window = _Window()
    window.closes = closes
    controller = update_ui.UpdateController(window, role)
    setup = tmp_path / "setup.exe"
    setup.write_bytes(content)
    controller._installer = setup
    controller._installer_sha = hashlib.sha256(b"installer").hexdigest()
    monkeypatch.setattr(updater, "other_instances", lambda: others)
    monkeypatch.setattr(QtWidgets.QApplication, "quit", staticmethod(lambda: None))
    return window, controller, setup


def test_a_refused_close_postpones_the_install(qt_app, monkeypatch, tmp_path) -> None:
    launched = []
    monkeypatch.setattr(updater, "launch_installer", lambda *args: launched.append(args))
    window, controller, setup = _controller(monkeypatch, tmp_path, closes=False)

    controller._install_and_quit()

    assert launched == []
    assert controller._installer == setup  # kept for a new click
    window.deleteLater()


def test_the_installer_starts_after_the_window_closed_with_its_role(qt_app, monkeypatch, tmp_path) -> None:
    launched = []
    monkeypatch.setattr(updater, "launch_installer", lambda *args: launched.append(args))
    window, controller, setup = _controller(monkeypatch, tmp_path)

    controller._install_and_quit()

    assert launched == [(setup, "server")]
    window.deleteLater()


def _failure_run(monkeypatch, tmp_path, *, tamper=False, refuse=False):
    from py_intercom import launcher

    events = []

    def launch(*args):
        if refuse:
            raise UpdateError("Windows n'a pas lancé l'installateur : l'autorisation administrateur a été refusée.")
        events.append("launched")

    monkeypatch.setattr(updater, "launch_installer", launch)
    monkeypatch.setattr(launcher, "restart_role", lambda role, delay_s=0.0: events.append(("restart", role, delay_s)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *args: events.append("warning")))
    window, controller, _setup = _controller(
        monkeypatch, tmp_path, role="client", content=b"tampered" if tamper else b"installer"
    )
    controller._install_and_quit()
    window.deleteLater()
    return events, controller


def test_a_declined_administrator_consent_brings_the_role_back(qt_app, monkeypatch, tmp_path) -> None:
    events, controller = _failure_run(monkeypatch, tmp_path, refuse=True)

    # The operator is told first; the role restarts after a delay, once the
    # exiting process has released the Poste's single-instance lock.
    assert events == ["warning", ("restart", "client", 2.0)]
    assert controller._installer is None


def test_a_swapped_installer_is_never_run(qt_app, monkeypatch, tmp_path) -> None:
    events, controller = _failure_run(monkeypatch, tmp_path, tamper=True)

    assert "launched" not in events
    assert events == ["warning", ("restart", "client", 2.0)]
    assert controller._installer is None


def test_another_py_intercom_on_this_pc_blocks_the_update(qt_app, monkeypatch, tmp_path) -> None:
    launched, infos = [], []
    monkeypatch.setattr(updater, "launch_installer", lambda *args: launched.append(args))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *args: infos.append(args)))
    window, controller, _setup = _controller(monkeypatch, tmp_path, others=1)

    controller._install_and_quit()

    assert launched == []
    assert len(infos) == 1  # the Régie and its local Poste are never closed by one update
    window.deleteLater()


def test_cancelling_as_the_download_ends_installs_nothing(qt_app, monkeypatch, tmp_path) -> None:
    launched = []
    monkeypatch.setattr(updater, "launch_installer", lambda *args: launched.append(args))
    window, controller, setup = _controller(monkeypatch, tmp_path)
    controller._cancel = threading.Event()
    controller._cancel.set()

    controller._on_downloaded((setup, controller._installer_sha), None)

    assert launched == []
    assert controller._installer == setup
    window.deleteLater()


# --- Elevation, integrity and local errors ----------------------------------


def test_a_protected_folder_needs_elevation_and_a_writable_one_does_not(tmp_path) -> None:
    assert updater.needs_elevation(tmp_path) is False
    assert list(tmp_path.iterdir()) == []  # the probe file is removed
    assert updater.needs_elevation(tmp_path / "missing") is True


def test_the_installer_is_started_elevated_only_for_a_protected_install(monkeypatch, tmp_path) -> None:
    import ctypes
    from types import SimpleNamespace

    calls = []

    def shell_execute(hwnd, verb, path, args, cwd, show):
        calls.append(verb)
        return 42

    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=shell_execute)), raising=False
    )
    monkeypatch.setattr(updater.sys, "platform", "win32")

    updater.launch_installer(tmp_path / "setup.exe", "client", elevate=True)
    updater.launch_installer(tmp_path / "setup.exe", "client", elevate=False)

    assert calls == ["runas", "open"]


def test_a_refusal_comes_back_as_an_error(monkeypatch, tmp_path) -> None:
    import ctypes
    from types import SimpleNamespace

    denied = SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=lambda *args: 5))
    monkeypatch.setattr(ctypes, "windll", denied, raising=False)
    monkeypatch.setattr(updater.sys, "platform", "win32")

    with pytest.raises(UpdateError, match="refusée"):
        updater.launch_installer(tmp_path / "setup.exe", "server", elevate=True)


def test_verify_installer_catches_a_swapped_or_missing_file(tmp_path) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(PAYLOAD)
    updater.verify_installer(setup, PAYLOAD_SHA)

    setup.write_bytes(PAYLOAD + b"x")
    with pytest.raises(UpdateError, match="modifié"):
        updater.verify_installer(setup, PAYLOAD_SHA)
    setup.unlink()
    with pytest.raises(UpdateError, match="disparu"):
        updater.verify_installer(setup, PAYLOAD_SHA)


def test_a_local_write_failure_is_not_blamed_on_the_network(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(updater, "_open", lambda *a, **k: _Response(PAYLOAD))
    blocker = tmp_path / "a-file"
    blocker.write_text("x")

    with pytest.raises(UpdateError, match="écrire"):
        updater.download_installer(_release(), directory=blocker / "dl")


def test_counting_other_instances_does_not_fail(monkeypatch) -> None:
    assert updater.other_instances() >= 0
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater.other_instances() == 0


def test_restart_role_can_wait_for_the_exiting_process(monkeypatch) -> None:
    from py_intercom import launcher

    seen = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kwargs: seen.append((cmd, kwargs)))

    launcher.restart_role("client")
    launcher.restart_role("client", delay_s=2.0)

    assert launcher.START_DELAY_ENV not in (seen[0][1].get("env") or {})
    assert seen[1][1]["env"][launcher.START_DELAY_ENV] == "2.0"
    assert seen[1][0][-2:] == ["--role", "client"]


def test_the_start_delay_is_consumed_once_and_bounded(monkeypatch) -> None:
    from py_intercom import launcher

    slept = []
    monkeypatch.setattr(launcher.time, "sleep", slept.append)
    monkeypatch.setenv(launcher.START_DELAY_ENV, "2.0")
    launcher._honour_start_delay()
    assert slept == [2.0]
    assert launcher.START_DELAY_ENV not in launcher.os.environ  # children do not wait again

    monkeypatch.setenv(launcher.START_DELAY_ENV, "9999")
    launcher._honour_start_delay()
    monkeypatch.setenv(launcher.START_DELAY_ENV, "garbage")
    launcher._honour_start_delay()
    assert slept == [2.0, 10.0]
