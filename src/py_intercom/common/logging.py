from __future__ import annotations

import os
import sys
import threading
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

    # The console stays quiet in production, but the file keeps the session
    # history (connections, reconnections, liveness losses) needed to explain
    # an incident after the show.
    console_level = "DEBUG" if debug else "WARNING"
    file_level = "DEBUG" if debug else "INFO"
    sink = sys.stderr if getattr(sys, "stderr", None) is not None else None
    if sink is None:
        sink = sys.stdout if getattr(sys, "stdout", None) is not None else None
    if sink is not None:
        logger.add(sink, level=console_level, backtrace=bool(debug), diagnose=False)

    safe_component = "".join(ch for ch in str(component) if ch.isalnum() or ch in "-_") or "app"
    log_dir = _log_directory()
    log_path = log_dir / f"{safe_component}.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_path),
            level=file_level,
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
    _install_exception_hooks()
    return log_path


_hooks_installed = False


def _install_exception_hooks() -> None:
    """Log what would otherwise vanish in the windowed exe (no stderr there).

    An exception in a Qt slot goes to sys.excepthook, one that ends a thread
    to threading.excepthook; neither reaches run_application's crash report.
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def log_uncaught(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.opt(exception=(exc_type, exc, tb)).error("uncaught exception")
        previous_hook(exc_type, exc, tb)

    def log_thread_crash(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).error(
                "uncaught exception in thread {}", name
            )
        previous_thread_hook(args)

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread_crash
