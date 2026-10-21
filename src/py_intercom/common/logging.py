from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger


@contextmanager
def best_effort(action: str) -> Iterator[None]:
    """Run a non-critical operation whose failure must not propagate.

    Use it for cleanup and cosmetic work (closing a socket, refreshing a
    label). The failure is still logged so nothing disappears silently. Never
    use it inside a PortAudio callback: count errors there instead.
    """
    try:
        yield
    except Exception as exc:
        logger.debug("{} failed: {}: {}", action, type(exc).__name__, exc)


def _log_directory() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA", "")).strip()
    if local_app_data:
        return Path(local_app_data) / "py-intercom" / "logs"
    return Path.home() / "py-intercom" / "logs"


def setup_logging(debug: bool, component: str = "app") -> Path:
    logger.remove()

    level = "DEBUG" if debug else "WARNING"
    sink = sys.stderr if getattr(sys, "stderr", None) is not None else None
    if sink is None:
        sink = sys.stdout if getattr(sys, "stdout", None) is not None else None
    if sink is not None:
        logger.add(sink, level=level, backtrace=bool(debug), diagnose=False)

    safe_component = "".join(ch for ch in str(component) if ch.isalnum() or ch in "-_") or "app"
    log_dir = _log_directory()
    log_path = log_dir / f"{safe_component}.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_path),
            level=level,
            rotation="10 MB",
            retention="14 days",
            compression="zip",
            enqueue=True,
            backtrace=bool(debug),
            diagnose=False,
            encoding="utf-8",
        )
        logger.info("logging initialized: component={} path={}", safe_component, log_path)
    except Exception as exc:
        if sink is not None:
            logger.warning("persistent logging unavailable at {}: {}", log_path, exc)
    return log_path
