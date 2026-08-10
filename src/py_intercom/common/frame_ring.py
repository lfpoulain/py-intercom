from __future__ import annotations

from dataclasses import dataclass
import operator
import threading
import time
from typing import Final, Optional

import numpy as np
import numpy.typing as npt


Float32Frame = npt.NDArray[np.float32]

_INT64_MIN: Final[int] = -(1 << 63)
_INT64_MAX: Final[int] = (1 << 63) - 1
_STOP_POLL_SECONDS: Final[float] = 0.01


@dataclass(frozen=True, slots=True)
class FrameRingStats:
    """Atomic snapshot of a frame ring's state and lifetime counters."""

    depth: int
    pushed: int
    popped: int
    dropped: int
    underflows: int


class FrameRingBuffer:
    """Bounded, preallocated ring for fixed-size mono float32 frames.

    The ring owns a fixed NumPy allocation. Producers and consumers never
    transfer ownership of an array: :meth:`push` copies from the producer's
    frame and :meth:`pop_into` copies into storage supplied by the consumer.

    When full, a push evicts the oldest pending frame. This is intentional for
    realtime audio, where preserving recent audio is preferable to accumulating
    latency. Each slot carries one signed 64-bit integer metadata value.

    The implementation is optimized for one producer and one consumer, while
    the short critical sections also make individual operations thread-safe.
    """

    def __init__(self, *, capacity: int, frame_samples: int) -> None:
        self.capacity = self._positive_index(capacity, name="capacity")
        self.frame_samples = self._positive_index(
            frame_samples,
            name="frame_samples",
        )

        self._frames = np.empty(
            (self.capacity, self.frame_samples),
            dtype=np.float32,
        )
        self._metadata = np.empty(self.capacity, dtype=np.int64)

        self._read_index = 0
        self._write_index = 0
        self._depth = 0

        self._pushed = 0
        self._popped = 0
        self._dropped = 0
        self._underflows = 0

        self._condition = threading.Condition(threading.Lock())

    @staticmethod
    def _positive_index(value: object, *, name: str) -> int:
        try:
            result = operator.index(value)
        except TypeError as exc:
            raise TypeError(f"{name} must be an integer") from exc
        if result <= 0:
            raise ValueError(f"{name} must be greater than zero")
        return int(result)

    @staticmethod
    def _metadata_value(metadata: object) -> int:
        try:
            value = operator.index(metadata)
        except TypeError as exc:
            raise TypeError("metadata must be an integer") from exc
        if value < _INT64_MIN or value > _INT64_MAX:
            raise OverflowError("metadata must fit in a signed 64-bit integer")
        return int(value)

    def _validate_input(self, frame: object) -> Float32Frame:
        if not isinstance(frame, np.ndarray):
            raise TypeError("frame must be a numpy.ndarray")
        if frame.dtype != np.float32:
            raise TypeError("frame dtype must be float32")
        if frame.ndim != 1 or frame.shape[0] != self.frame_samples:
            raise ValueError(
                f"frame must have shape ({self.frame_samples},), got {frame.shape}"
            )
        return frame

    def _validate_output(self, output: object) -> Float32Frame:
        frame = self._validate_input(output)
        if not frame.flags.writeable:
            raise ValueError("output frame must be writeable")
        return frame

    @property
    def depth(self) -> int:
        with self._condition:
            return int(self._depth)

    @property
    def stats(self) -> FrameRingStats:
        with self._condition:
            return FrameRingStats(
                depth=int(self._depth),
                pushed=int(self._pushed),
                popped=int(self._popped),
                dropped=int(self._dropped),
                underflows=int(self._underflows),
            )

    def push(self, frame: Float32Frame, metadata: int = 0) -> None:
        """Copy one frame and its metadata into the ring.

        The oldest pending frame is discarded when the ring is already full.
        The producer's array is never retained or exposed to the consumer.
        """

        source = self._validate_input(frame)
        metadata_value = self._metadata_value(metadata)

        with self._condition:
            if self._depth == self.capacity:
                self._read_index = (self._read_index + 1) % self.capacity
                self._depth -= 1
                self._dropped += 1

            slot = self._write_index
            np.copyto(self._frames[slot], source, casting="no")
            self._metadata[slot] = metadata_value

            self._write_index = (slot + 1) % self.capacity
            self._depth += 1
            self._pushed += 1
            self._condition.notify()

    def _pop_locked(self, output: Float32Frame) -> Optional[int]:
        if self._depth == 0:
            return None

        slot = self._read_index
        np.copyto(output, self._frames[slot], casting="no")
        metadata = int(self._metadata[slot])

        self._read_index = (slot + 1) % self.capacity
        self._depth -= 1
        self._popped += 1
        return metadata

    def pop_into(self, output: Float32Frame) -> Optional[int]:
        """Copy the oldest frame into ``output`` and return its metadata.

        ``None`` means that the ring was empty. Empty non-blocking pops count as
        underflows; the caller's output buffer is left unchanged.
        """

        destination = self._validate_output(output)
        with self._condition:
            metadata = self._pop_locked(destination)
            if metadata is None:
                self._underflows += 1
            return metadata

    def wait_pop_into(
        self,
        output: Float32Frame,
        *,
        stop_event: Optional[threading.Event] = None,
        timeout: Optional[float] = None,
    ) -> Optional[int]:
        """Wait for a frame, then copy it into ``output``.

        A set ``stop_event`` interrupts the wait and takes priority over queued
        audio, allowing a worker to stop without consuming stale frames. Event
        waits are polled at a short bounded interval because ``threading.Event``
        cannot directly notify this ring's condition. A stop interruption does
        not increment ``underflows``; an elapsed timeout does.
        """

        destination = self._validate_output(output)
        if timeout is not None:
            timeout = float(timeout)
            if timeout < 0.0:
                raise ValueError("timeout must be non-negative or None")
            deadline = time.monotonic() + timeout
        else:
            deadline = None

        with self._condition:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return None

                metadata = self._pop_locked(destination)
                if metadata is not None:
                    return metadata

                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        self._underflows += 1
                        return None
                else:
                    remaining = None

                if stop_event is None:
                    wait_seconds = remaining
                elif remaining is None:
                    wait_seconds = _STOP_POLL_SECONDS
                else:
                    wait_seconds = min(remaining, _STOP_POLL_SECONDS)
                self._condition.wait(timeout=wait_seconds)

    def clear(self) -> int:
        """Discard pending frames and return how many were removed.

        Lifetime counters are preserved. Explicit clears are not counted as
        overload drops; ``dropped`` exclusively measures full-ring eviction.
        """

        with self._condition:
            cleared = int(self._depth)
            self._read_index = 0
            self._write_index = 0
            self._depth = 0
            return cleared
