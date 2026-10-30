from __future__ import annotations

import json
import os
import threading

import pytest

from py_intercom.common import jsonio
from py_intercom.common.jsonio import (
    JsonFileStatus,
    atomic_write_json,
    read_json_file_result,
)


def test_read_json_file_result_distinguishes_missing_and_invalid(tmp_path) -> None:
    path = tmp_path / "preset.json"

    assert read_json_file_result(path).status is JsonFileStatus.MISSING

    path.write_text("{invalid", encoding="utf-8")
    result = read_json_file_result(path)

    assert result.status is JsonFileStatus.INVALID
    assert result.error


def test_atomic_write_json_keeps_previous_version_as_backup(tmp_path) -> None:
    path = tmp_path / "preset.json"
    atomic_write_json(path, {"revision": 1})
    atomic_write_json(path, {"revision": 2})

    assert read_json_file_result(path).value == {"revision": 2}
    assert json.loads((tmp_path / "preset.json.bak").read_text(encoding="utf-8")) == {"revision": 1}


def test_concurrent_atomic_writes_never_leave_partial_json(tmp_path) -> None:
    path = tmp_path / "preset.json"
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def writer(revision: int) -> None:
        try:
            barrier.wait()
            for iteration in range(20):
                atomic_write_json(
                    path,
                    {"revision": revision, "iteration": iteration, "payload": "x" * 1024},
                )
                loaded = read_json_file_result(path)
                assert loaded.status is JsonFileStatus.OK
        except BaseException as exc:  # captured and asserted in the parent thread
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(idx,)) for idx in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert read_json_file_result(path).status is JsonFileStatus.OK
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_json_retries_while_the_file_is_locked(tmp_path, monkeypatch) -> None:
    path = tmp_path / "preset.json"
    atomic_write_json(path, {"revision": 1})
    real_replace = os.replace
    attempts: list[int] = []

    def locked_twice(src, dst) -> None:
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(13, "file in use by another process")
        real_replace(src, dst)

    monkeypatch.setattr(jsonio, "_LOCKED_FILE_RETRY_S", 0.0)
    monkeypatch.setattr(jsonio.os, "replace", locked_twice)

    atomic_write_json(path, {"revision": 2})

    assert len(attempts) == 3
    assert read_json_file_result(path).value == {"revision": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_json_gives_up_on_a_lasting_lock(tmp_path, monkeypatch) -> None:
    path = tmp_path / "preset.json"
    atomic_write_json(path, {"revision": 1})

    def always_locked(src, dst) -> None:
        raise PermissionError(13, "file in use by another process")

    monkeypatch.setattr(jsonio, "_LOCKED_FILE_RETRY_S", 0.0)
    monkeypatch.setattr(jsonio.os, "replace", always_locked)

    with pytest.raises(PermissionError):
        atomic_write_json(path, {"revision": 2})

    assert read_json_file_result(path).value == {"revision": 1}
    assert not list(tmp_path.glob("*.tmp"))
