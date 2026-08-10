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
