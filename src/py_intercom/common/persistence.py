from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .jsonio import atomic_write_json
from .logging import best_effort

_FACTORY_VALUE = object()


class DebouncedJsonWriter:
    """Serialize coalesced JSON writes on one owned, non-daemon thread.

    ``request()`` replaces an older pending value and delays the write.  A
    caller that owns the application lifecycle must call ``close()``; closing
    flushes the latest value before joining the worker.  When ``data_factory``
    is supplied, the factory runs only after the debounce interval, which is
    useful for taking a fresh state snapshot without doing work on every
    slider event.
    """

    def __init__(
        self,
        path: Path,
        *,
        delay_s: float = 0.35,
        data_factory: Callable[[], Any] | None = None,
        write_func: Callable[[Path, Any], None] = atomic_write_json,
        on_error: Callable[[Exception], None] | None = None,
        thread_name: str = "json-preset-writer",
    ) -> None:
        self._path = Path(path)
        self._delay_s = max(0.0, float(delay_s))
        self._data_factory = data_factory
        self._write_func = write_func
        self._on_error = on_error
        self._thread_name = str(thread_name)

        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._pending = False
        self._pending_value: Any = _FACTORY_VALUE
        self._due_at = 0.0
        self._revision = 0
        self._settled_revision = 0
        self._writing = False
        self._failed_revision = 0
        self._last_error: Exception | None = None

    @property
    def last_error(self) -> Exception | None:
        with self._condition:
            return self._last_error

    def request(self, data: Any = _FACTORY_VALUE) -> int:
        if data is _FACTORY_VALUE and self._data_factory is None:
            raise ValueError("request() needs data when no data_factory is configured")

        # Callers often mutate their in-memory preset immediately after this
        # method returns.  The writer must own an immutable snapshot of it.
        value = _FACTORY_VALUE if data is _FACTORY_VALUE else copy.deepcopy(data)
        with self._condition:
            if self._closed:
                raise RuntimeError("JSON writer is closed")
            self._revision += 1
            revision = int(self._revision)
            self._pending = True
            self._pending_value = value
            self._due_at = time.monotonic() + self._delay_s
            self._ensure_thread_locked()
            self._condition.notify_all()
            return revision

    def cancel_pending(self) -> None:
        """Discard a value that has not started writing yet."""

        with self._condition:
            if self._pending:
                self._pending = False
                self._pending_value = _FACTORY_VALUE
                self._settled_revision = max(self._settled_revision, self._revision)
                self._condition.notify_all()

    def flush(self, *, raise_on_error: bool = True) -> None:
        """Write the latest requested revision and wait until it is settled."""

        with self._condition:
            target_revision = int(self._revision)
            if self._pending:
                self._due_at = 0.0
                self._ensure_thread_locked()
                self._condition.notify_all()
            # A cancelled newer revision may be marked settled while an older
            # write is still in flight.  Do not let reset/close race that write
            # and accidentally resurrect the old file afterwards.
            while self._settled_revision < target_revision or self._writing:
                self._condition.wait()
            error = self._last_error if self._failed_revision == target_revision else None

        if error is not None and raise_on_error:
            raise error

    def close(self, *, raise_on_error: bool = False) -> None:
        """Flush pending data, stop the worker, and join it exactly once."""

        error: Exception | None = None
        try:
            self.flush(raise_on_error=True)
        except Exception as exc:
            error = exc

        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            thread.join()

        if error is not None and raise_on_error:
            raise error

    def _ensure_thread_locked(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=self._run,
            name=self._thread_name,
            daemon=False,
        )
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed and not self._pending:
                    return

                delay = self._due_at - time.monotonic()
                if delay > 0.0:
                    self._condition.wait(timeout=delay)
                    continue

                revision = int(self._revision)
                value = self._pending_value
                self._pending = False
                self._pending_value = _FACTORY_VALUE
                self._writing = True

            error: Exception | None = None
            try:
                if value is _FACTORY_VALUE:
                    factory = self._data_factory
                    if factory is None:
                        raise RuntimeError("JSON writer has no data factory")
                    value = factory()
                self._write_func(self._path, value)
            except Exception as exc:
                error = exc

            with self._condition:
                self._writing = False
                self._settled_revision = max(self._settled_revision, revision)
                if error is None:
                    if self._failed_revision <= revision:
                        self._last_error = None
                else:
                    self._failed_revision = revision
                    self._last_error = error
                self._condition.notify_all()

            if error is not None and self._on_error is not None:
                with best_effort("_on_error()"):
                    self._on_error(error)
