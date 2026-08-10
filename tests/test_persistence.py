from __future__ import annotations

import threading

from py_intercom.common.persistence import DebouncedJsonWriter


def test_debounced_writer_coalesces_and_flushes_latest_value(tmp_path) -> None:
    writes = []
    writer = DebouncedJsonWriter(
        tmp_path / "preset.json",
        delay_s=30.0,
        write_func=lambda path, data: writes.append((path, data)),
    )

    writer.request({"value": 1})
    mutable = {"value": 2}
    writer.request(mutable)
    mutable["value"] = 99
    writer.flush()
    worker = writer._thread
    writer.close()

    assert writes == [(tmp_path / "preset.json", {"value": 2})]
    assert worker is not None
    assert worker.daemon is False
    assert not worker.is_alive()


def test_debounced_writer_factory_runs_once_after_coalescing(tmp_path) -> None:
    calls = []
    writes = []

    def snapshot():
        calls.append(threading.current_thread().name)
        return {"revision": len(calls)}

    writer = DebouncedJsonWriter(
        tmp_path / "preset.json",
        delay_s=30.0,
        data_factory=snapshot,
        write_func=lambda path, data: writes.append(data),
    )
    writer.request()
    writer.request()
    writer.request()
    writer.close(raise_on_error=True)

    assert calls == ["json-preset-writer"]
    assert writes == [{"revision": 1}]


def test_cancel_then_flush_waits_for_an_older_inflight_write(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()
    flushed = threading.Event()

    def write(_path, _data):
        started.set()
        assert release.wait(timeout=2.0)

    writer = DebouncedJsonWriter(
        tmp_path / "preset.json",
        delay_s=0.0,
        write_func=write,
    )
    writer.request({"old": True})
    assert started.wait(timeout=2.0)
    writer.request({"cancelled": True})
    writer.cancel_pending()

    flush_thread = threading.Thread(target=lambda: (writer.flush(), flushed.set()))
    flush_thread.start()
    assert not flushed.wait(timeout=0.05)
    release.set()
    flush_thread.join(timeout=2.0)
    writer.close()

    assert flushed.is_set()
