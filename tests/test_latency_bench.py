from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import latency_bench as bench  # noqa: E402


def _scenario(name: str) -> bench.Scenario:
    return next(sc for sc in bench.SCENARIOS if sc.name == name)


def test_clean_lan_plays_every_frame_after_the_start():
    result = bench.run(replace(_scenario("LAN filaire"), duration_s=5.0), "card")

    # Only the reads before the first frames arrive are silent.
    assert result.silent_reads <= 5
    assert result.plc == result.fec == result.trimmed == 0
    assert result.p99_ms < 50.0


def test_card_clock_playout_does_not_drift_with_the_headphones():
    for name in ("carte +100 ppm", "carte -100 ppm"):
        scenario = _scenario(name)
        card = bench.run(scenario, "card")
        legacy = bench.run(scenario, "legacy")
        assert card.median_ms <= legacy.median_ms + 0.5
        assert card.p99_ms < 50.0


def test_card_clock_playout_is_never_slower_than_the_legacy_model():
    for seed in range(1, 6):
        for name in ("LAN filaire", "prises de parole"):
            scenario = replace(_scenario(name), seed=seed, duration_s=6.0)
            assert bench.run(scenario, "card").median_ms <= bench.run(scenario, "legacy").median_ms + 0.5


def test_a_network_freeze_leaves_no_lasting_delay():
    result = bench.run(_scenario("gel réseau 250 ms"), "card")

    # The 25 delayed frames are dropped at once, then latency is back to normal.
    assert result.trimmed >= 20
    assert result.latencies_ms[-1] < 50.0
