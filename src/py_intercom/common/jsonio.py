from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .logging import best_effort

# Antivirus scanners and sync clients (Defender, OneDrive) briefly lock a file
# while they inspect it; Windows then refuses to replace or copy it (WinError
# 5 or 32). Each retried operation (backup copy, replace) blocks its caller
# for up to ~200 ms while the file stays locked: never call this from the
# audio path or the Qt thread (both GUIs go through DebouncedJsonWriter).
_LOCKED_FILE_ATTEMPTS = 5
_LOCKED_FILE_RETRY_S = 0.05


class JsonFileStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    IO_ERROR = "io_error"


@dataclass(frozen=True)
class JsonReadResult:
    status: JsonFileStatus
    value: Any = None
    error: str = ""


_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _path_locks[key] = lock
        return lock


def read_json_file_result(path: Path) -> JsonReadResult:
    path = Path(path)
    lock = _path_lock(path)
    with lock:
        if not path.exists():
            return JsonReadResult(JsonFileStatus.MISSING)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return JsonReadResult(JsonFileStatus.IO_ERROR, error=str(exc))

    try:
        return JsonReadResult(JsonFileStatus.OK, value=json.loads(text))
    except (json.JSONDecodeError, UnicodeError) as exc:
        return JsonReadResult(JsonFileStatus.INVALID, error=str(exc))


def atomic_write_json(path: Path, data: Any, *, backup: bool = True) -> None:
    path = Path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    lock = _path_lock(path)

    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            if backup and path.exists():
                backup_path = path.with_suffix(path.suffix + ".bak")
                _retry_while_locked(lambda: shutil.copy2(path, backup_path))

            _retry_while_locked(lambda: os.replace(tmp_path, path))
        finally:
            with best_effort("remove temporary JSON file"):
                tmp_path.unlink(missing_ok=True)


def _retry_while_locked(operation: Callable[[], object]) -> None:
    for attempt in range(1, _LOCKED_FILE_ATTEMPTS + 1):
        try:
            operation()
            return
        except PermissionError:
            if attempt == _LOCKED_FILE_ATTEMPTS:
                raise
            time.sleep(_LOCKED_FILE_RETRY_S)
