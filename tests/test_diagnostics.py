from __future__ import annotations

from py_intercom.common.diagnostics import CounterRates, format_ms, format_rate


def test_rates_are_per_second_and_ignore_resets() -> None:
    rates = CounterRates()

    assert rates.update({"plc": 5}, 10.0) == {"plc": 0.0}
    assert rates.update({"plc": 9}, 12.0) == {"plc": 2.0}
    # A new session restarts the counter: no negative rate.
    assert rates.update({"plc": 1}, 13.0) == {"plc": 0.0}


def test_readouts_stay_short() -> None:
    assert format_rate(0.0) == "0/s"
    assert format_rate(0.5) == "0.5/s"
    assert format_rate(12.4) == "12/s"
    assert format_ms(None) == "—"
    assert format_ms(1.234) == "1.2 ms"
    assert format_ms(42.6) == "43 ms"
