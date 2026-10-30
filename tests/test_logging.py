from __future__ import annotations

from loguru import logger

from py_intercom.common.logging import setup_logging


def test_setup_logging_creates_component_log_in_local_app_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    log_path = setup_logging(debug=True, component="client:test")
    logger.info("test marker")
    logger.remove()

    assert log_path == tmp_path / "py-intercom" / "logs" / "clienttest.log"
    assert log_path.exists()
    assert "test marker" in log_path.read_text(encoding="utf-8")


def test_production_log_file_keeps_session_events_but_console_stays_quiet(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    log_path = setup_logging(debug=False, component="client")
    logger.debug("debug noise")
    logger.info("control liveness timeout")
    logger.remove()

    text = log_path.read_text(encoding="utf-8")
    assert "control liveness timeout" in text
    assert "debug noise" not in text
    assert "control liveness timeout" not in capsys.readouterr().err
