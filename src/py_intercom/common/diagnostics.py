"""Turn the engines' lifetime counters into figures an operator can read."""

from __future__ import annotations

from collections.abc import Mapping


class CounterRates:
    """Per-second rate of monotonic counters between two GUI refreshes."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._last_at: float | None = None

    def update(self, counters: Mapping[str, float], now: float) -> dict[str, float]:
        """Rates since the previous call; zero on the first one or after a reset."""
        elapsed = None if self._last_at is None else now - self._last_at
        rates = {}
        for key, value in counters.items():
            previous = self._last.get(key)
            delta = 0.0 if previous is None else float(value) - previous
            # A counter that went down was reset (new session): no rate yet.
            rates[key] = delta / elapsed if elapsed and elapsed > 0.0 and delta > 0.0 else 0.0
        self._last = {key: float(value) for key, value in counters.items()}
        self._last_at = now
        return rates

    def reset(self) -> None:
        self._last.clear()
        self._last_at = None


def format_rate(per_second: float) -> str:
    return f"{per_second:.0f}/s" if per_second >= 1.0 or per_second == 0.0 else f"{per_second:.1f}/s"


def format_ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f} ms" if value < 10.0 else f"{value:.0f} ms"
