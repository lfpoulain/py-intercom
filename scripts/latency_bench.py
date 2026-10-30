"""Deterministic bench of the client receive path: network -> jitter buffer -> headphones.

Runs the real jitter buffer and PCM ring against simulated clocks, so a
scenario always gives the same figures. Two playout models are compared:

- ``card``: the engine's model. Each headphone callback takes a frame and
  asks the decoder for the next one, read from the jitter buffer
  PLAYOUT_DECODE_AHEAD_MS before the card needs it.
- ``legacy``: the previous model, kept here as the reference. A worker pops
  the jitter buffer on the system clock and feeds a ring that the card drains
  on its own clock, with a one-frame margin and a latency cap.

Latency is measured from the moment a frame is sent to the moment the card
plays it (sound card buffers excluded: they are the same in both models).

Usage: python scripts/latency_bench.py
"""

from __future__ import annotations

import heapq
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from py_intercom.common.constants import (  # noqa: E402
    LATENCY_TRIM_AFTER_FRAMES,
    PCM_RING_TRIM_ABOVE_FRAMES,
    PLAYOUT_DECODE_AHEAD_MS,
    PLAYOUT_LEAD_FRAMES,
)
from py_intercom.common.frame_ring import FrameRingBuffer  # noqa: E402
from py_intercom.common.jitter_buffer import new_realtime_jitter_buffer  # noqa: E402

PERIOD_MS = 10.0
_SAMPLES = 4  # PCM content is irrelevant here: a tiny frame keeps the bench fast


@dataclass(frozen=True)
class Scenario:
    name: str
    duration_s: float = 20.0
    base_delay_ms: float = 1.0
    jitter_ms: float = 0.0
    loss: float = 0.0
    freeze_at_s: float | None = None  # the receiver gets nothing for freeze_ms...
    freeze_ms: float = 0.0  # ...then every delayed packet at once
    card_ppm: float = 0.0  # headphone clock against the sender's
    talk_s: float | None = None  # talk spurts of talk_s separated by pause_s
    pause_s: float = 0.0
    worker_late_ms: float = 0.3  # decoder thread wake-up latency
    seed: int = 1


@dataclass
class Result:
    latencies_ms: list[float] = field(default_factory=list)
    silent_reads: int = 0
    fec: int = 0
    plc: int = 0
    trimmed: int = 0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else float("nan")

    @property
    def p99_ms(self) -> float:
        return float(np.percentile(self.latencies_ms, 99)) if self.latencies_ms else float("nan")


SCENARIOS = (
    Scenario("LAN filaire"),
    Scenario("gigue 8 ms", jitter_ms=8.0),
    Scenario("2 % de pertes", jitter_ms=2.0, loss=0.02),
    Scenario("gel réseau 250 ms", jitter_ms=2.0, freeze_at_s=5.0, freeze_ms=250.0),
    Scenario("carte +100 ppm", jitter_ms=2.0, card_ppm=100.0, duration_s=60.0),
    Scenario("carte -100 ppm", jitter_ms=2.0, card_ppm=-100.0, duration_s=60.0),
    Scenario("prises de parole", jitter_ms=4.0, talk_s=1.5, pause_s=0.5),
)


def _arrivals(sc: Scenario) -> list[tuple[float, int, float]]:
    """(arrival_ms, seq, send_ms) for every packet that reaches the receiver."""
    rng = random.Random(sc.seed)
    out = []
    seq = 0
    total = int(sc.duration_s * 1000.0 / PERIOD_MS)
    for k in range(total):
        send = k * PERIOD_MS
        if sc.talk_s is not None and (send / 1000.0) % (sc.talk_s + sc.pause_s) >= sc.talk_s:
            continue  # PTT released: nothing is sent and the sequence stays put
        if rng.random() < sc.loss:
            seq += 1
            continue
        arrival = send + sc.base_delay_ms + rng.uniform(0.0, sc.jitter_ms)
        if sc.freeze_at_s is not None:
            start = sc.freeze_at_s * 1000.0
            if start <= arrival < start + sc.freeze_ms:
                arrival = start + sc.freeze_ms
        out.append((arrival, seq, send))
        seq += 1
    out.sort()
    return out


def run(sc: Scenario, model: str) -> Result:
    jb = new_realtime_jitter_buffer()
    if model == "card":
        ring = FrameRingBuffer(capacity=16, frame_samples=_SAMPLES)
    else:
        ring = FrameRingBuffer(
            capacity=16,
            frame_samples=_SAMPLES,
            trim_above=PCM_RING_TRIM_ABOVE_FRAMES,
            trim_after=LATENCY_TRIM_AFTER_FRAMES,
        )
    sent_at: dict[int, float] = {}
    frame = np.zeros(_SAMPLES, dtype=np.float32)
    result = Result()
    card_period = PERIOD_MS * (1.0 + sc.card_ppm * 1e-6)
    end = sc.duration_s * 1000.0

    events: list[tuple[float, int, str, object]] = []
    for order, (arrival, seq, send) in enumerate(_arrivals(sc)):
        heapq.heappush(events, (arrival, order, "packet", (seq, send)))
    order = 1 << 30

    def schedule(at: float, kind: str) -> None:
        nonlocal order
        order += 1
        heapq.heappush(events, (at, order, kind, None))

    def produce() -> None:
        jitter_frame = jb.pop_frame()
        if jitter_frame is None:
            return
        if jitter_frame.fec_payload:
            result.fec += 1
        elif jitter_frame.concealed:
            result.plc += 1
        ring.push(frame, metadata=-1 if jitter_frame.concealed else int(jitter_frame.sequence_number))

    # The card's phase against the system clock is arbitrary; it only
    # matters to the legacy worker, which runs on the system clock.
    schedule(random.Random(sc.seed + 1).uniform(0.0, PERIOD_MS), "card")
    if model == "legacy":
        schedule(0.0, "worker")
    while events:
        now, _order, kind, payload = heapq.heappop(events)
        if now > end:
            break
        if kind == "packet":
            seq, send = payload
            sent_at[seq] = send
            jb.push(seq, b"opus")
        elif kind == "card":
            metadata = ring.pop_into(frame)
            if metadata is None:
                result.silent_reads += 1
            elif metadata >= 0:
                result.latencies_ms.append(now - sent_at[metadata])
            schedule(now + card_period, "card")
            if model == "card":
                schedule(now + PERIOD_MS - PLAYOUT_DECODE_AHEAD_MS + sc.worker_late_ms, "refill")
        elif kind == "refill":
            while ring.depth < PLAYOUT_LEAD_FRAMES:
                before = ring.depth
                produce()
                if ring.depth == before:
                    break
        elif kind == "worker":
            produce()
            schedule(now + PERIOD_MS, "worker")
    result.trimmed = jb.stats.trimmed + ring.stats.trimmed
    return result


def main() -> int:
    header = f"{'scénario':<20} {'modèle':<7} {'médiane':>8} {'p99':>8} {'silences':>9} {'FEC':>5} {'PLC':>5} {'retraits':>9}"
    print(header)
    print("-" * len(header))
    for sc in SCENARIOS:
        for model in ("legacy", "card"):
            r = run(sc, model)
            print(
                f"{sc.name:<20} {model:<7} {r.median_ms:>6.1f}ms {r.p99_ms:>6.1f}ms "
                f"{r.silent_reads:>9} {r.fec:>5} {r.plc:>5} {r.trimmed:>9}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
