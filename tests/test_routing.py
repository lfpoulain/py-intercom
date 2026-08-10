from __future__ import annotations

import math

import numpy as np
import pytest

from py_intercom.common.buses import (
    FIXED_BUS_IDS,
    PLATEAU_BUS_ID,
    REGIE_BUS_ID,
    VMIX_BUS_ID,
)
from py_intercom.common.constants import FRAME_SAMPLES
from py_intercom.common.routing import (
    VALID_BUS_MASK,
    RoutedSource,
    RoutingConfig,
    RoutingError,
    build_destination_mix,
    route_sources,
)


def _frame(value: float) -> np.ndarray:
    return np.full((int(FRAME_SAMPLES),), float(value), dtype=np.float32)


def _assert_frame(frame: np.ndarray, value: float) -> None:
    assert frame.dtype == np.float32
    assert frame.shape == (int(FRAME_SAMPLES),)
    np.testing.assert_allclose(frame, float(value), rtol=1e-6, atol=1e-6)


def test_empty_routing_returns_all_fixed_bus_frames_and_silence() -> None:
    result = route_sources([])

    assert set(result.bus_frames) == set(FIXED_BUS_IDS)
    for frame in result.bus_frames.values():
        _assert_frame(frame, 0.0)
    _assert_frame(result.regie_linear, 0.0)
    assert dict(result.regie_contribution_by_client) == {}


@pytest.mark.parametrize("bus_id", [REGIE_BUS_ID, PLATEAU_BUS_ID, VMIX_BUS_ID])
def test_each_valid_bit_routes_only_to_its_bus(bus_id: int) -> None:
    source = RoutedSource(client_id=7, frame=_frame(0.25), bus_mask=1 << bus_id)
    result = route_sources([source])

    for current_bus_id, frame in result.bus_frames.items():
        _assert_frame(frame, 0.25 if current_bus_id == bus_id else 0.0)


def test_zero_and_unknown_masks_are_fail_closed() -> None:
    unknown_bit = 1 << 12
    zero_mask = RoutedSource(client_id=1, frame=_frame(0.25), bus_mask=0)
    unknown_mask = RoutedSource(client_id=2, frame=_frame(0.50), bus_mask=unknown_bit)
    mixed_mask = RoutedSource(
        client_id=3,
        frame=_frame(0.75),
        bus_mask=unknown_bit | (1 << PLATEAU_BUS_ID),
    )

    assert zero_mask.valid_bus_mask == 0
    assert unknown_mask.valid_bus_mask == 0
    assert mixed_mask.valid_bus_mask == 1 << PLATEAU_BUS_ID
    result = route_sources([zero_mask, unknown_mask, mixed_mask])

    _assert_frame(result.bus_frames[REGIE_BUS_ID], 0.0)
    _assert_frame(result.bus_frames[PLATEAU_BUS_ID], 0.75)
    _assert_frame(result.bus_frames[VMIX_BUS_ID], 0.0)
    _assert_frame(result.regie_linear, 0.0)


def test_bus_gain_is_applied_before_linear_summing_without_clipping() -> None:
    double_db = 20.0 * math.log10(2.0)
    config = RoutingConfig(bus_gain_db={PLATEAU_BUS_ID: double_db})
    sources = [
        RoutedSource(1, _frame(0.75), 1 << PLATEAU_BUS_ID),
        RoutedSource(2, _frame(0.75), 1 << PLATEAU_BUS_ID),
    ]

    result = route_sources(sources, config)

    # Two 0.75 sources at x2 deliberately exceed full scale. A shared bus mix
    # must stay linear; limiting belongs to each physical/network destination.
    _assert_frame(result.bus_frames[PLATEAU_BUS_ID], 3.0)
    _assert_frame(result.regie_linear, 0.0)


def test_multi_ptt_routes_every_physical_bus_but_direct_regie_is_monitored_once() -> None:
    config = RoutingConfig(
        bus_gain_db={
            REGIE_BUS_ID: 0.0,
            PLATEAU_BUS_ID: 20.0 * math.log10(2.0),
            VMIX_BUS_ID: 20.0 * math.log10(4.0),
        },
        feed_to_regie={PLATEAU_BUS_ID: True, VMIX_BUS_ID: True},
    )
    source = RoutedSource(client_id=9, frame=_frame(0.25), bus_mask=VALID_BUS_MASK)

    result = route_sources([source], config)

    _assert_frame(result.bus_frames[REGIE_BUS_ID], 0.25)
    _assert_frame(result.bus_frames[PLATEAU_BUS_ID], 0.50)
    _assert_frame(result.bus_frames[VMIX_BUS_ID], 1.00)
    # Direct Régie has priority. Neither fed copy is added to the monitor.
    _assert_frame(result.regie_linear, 0.25)
    _assert_frame(result.regie_contribution_by_client[9], 0.25)


def test_multi_ptt_fed_monitor_uses_first_catalogued_bus_once() -> None:
    config = RoutingConfig(
        bus_gain_db={
            PLATEAU_BUS_ID: 20.0 * math.log10(2.0),
            VMIX_BUS_ID: 20.0 * math.log10(4.0),
        },
        feed_to_regie={PLATEAU_BUS_ID: True, VMIX_BUS_ID: True},
    )
    source = RoutedSource(
        client_id=11,
        frame=_frame(0.25),
        bus_mask=(1 << PLATEAU_BUS_ID) | (1 << VMIX_BUS_ID),
    )

    result = route_sources([source], config)

    _assert_frame(result.bus_frames[PLATEAU_BUS_ID], 0.50)
    _assert_frame(result.bus_frames[VMIX_BUS_ID], 1.00)
    # FIXED_BUSES orders Plateau before VMix. Monitoring the source once avoids
    # the +6 dB jump that pressing two PTT buttons previously risked.
    _assert_frame(result.regie_linear, 0.50)
    _assert_frame(result.regie_contribution_by_client[11], 0.50)


def test_feed_to_regie_collects_distinct_clients_and_respects_bus_gains() -> None:
    config = RoutingConfig(
        bus_gain_db={PLATEAU_BUS_ID: 20.0 * math.log10(2.0)},
        feed_to_regie={PLATEAU_BUS_ID: True, VMIX_BUS_ID: True},
    )
    sources = [
        RoutedSource(1, _frame(0.10), 1 << REGIE_BUS_ID),
        RoutedSource(2, _frame(0.20), 1 << PLATEAU_BUS_ID),
        RoutedSource(3, _frame(0.30), 1 << VMIX_BUS_ID),
    ]

    result = route_sources(sources, config)

    _assert_frame(result.regie_contribution_by_client[1], 0.10)
    _assert_frame(result.regie_contribution_by_client[2], 0.40)
    _assert_frame(result.regie_contribution_by_client[3], 0.30)
    _assert_frame(result.regie_linear, 0.80)


def test_full_scale_mix_minus_subtracts_before_destination_limiting() -> None:
    sources = [
        RoutedSource(1, _frame(1.0), 1 << REGIE_BUS_ID),
        RoutedSource(2, _frame(1.0), 1 << REGIE_BUS_ID),
    ]
    routed = route_sources(sources)

    _assert_frame(routed.regie_linear, 2.0)
    destination = build_destination_mix(
        routed.regie_linear,
        routed.regie_contribution_by_client[1],
        listen_regie=True,
    )

    # Correct mix-minus is the other full-scale source, not -0.01 after
    # subtracting an unlimited self contribution from an already limited mix.
    _assert_frame(destination, 1.0)
    _assert_frame(routed.regie_linear, 2.0)


def test_return_is_added_after_mix_minus_and_then_destination_is_limited() -> None:
    destination = build_destination_mix(
        _frame(2.0),
        _frame(1.0),
        listen_regie=True,
        return_frame=_frame(0.5),
        listen_return=True,
        return_gain_db=20.0 * math.log10(2.0),
    )

    # (2 - 1) + (0.5 * 2) = 2, then the completed destination is limited.
    _assert_frame(destination, 0.99)


def test_destination_can_listen_to_return_without_regie() -> None:
    destination = build_destination_mix(
        _frame(0.75),
        _frame(0.25),
        listen_regie=False,
        return_frame=_frame(0.20),
        listen_return=True,
        return_gain_db=0.0,
    )

    _assert_frame(destination, 0.20)


@pytest.mark.parametrize(
    "bad_frame, error_type, message",
    [
        (np.zeros((FRAME_SAMPLES, 1), dtype=np.float32), RoutingError, "shape"),
        (np.zeros((FRAME_SAMPLES - 1,), dtype=np.float32), RoutingError, "shape"),
        (np.zeros((FRAME_SAMPLES,), dtype=np.float64), TypeError, "float32"),
        (np.full((FRAME_SAMPLES,), np.nan, dtype=np.float32), RoutingError, "NaN"),
        (np.full((FRAME_SAMPLES,), np.inf, dtype=np.float32), RoutingError, "infinite"),
    ],
)
def test_routed_source_rejects_malformed_or_nonfinite_frames(
    bad_frame: np.ndarray,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        RoutedSource(client_id=1, frame=bad_frame, bus_mask=1)


def test_routed_source_snapshots_and_freezes_decoder_frame() -> None:
    decoder_frame = _frame(0.25)
    source = RoutedSource(1, decoder_frame, 1 << REGIE_BUS_ID)
    decoder_frame[:] = 0.75

    _assert_frame(source.frame, 0.25)
    assert not source.frame.flags.writeable
    with pytest.raises(ValueError):
        source.frame[0] = 0.0


def test_routing_result_frames_are_read_only_snapshots() -> None:
    result = route_sources([RoutedSource(1, _frame(0.25), 1 << REGIE_BUS_ID)])

    assert not result.bus_frames[REGIE_BUS_ID].flags.writeable
    assert not result.regie_linear.flags.writeable
    assert not result.regie_contribution_by_client[1].flags.writeable
    with pytest.raises(TypeError):
        result.bus_frames[99] = _frame(0.0)


def test_duplicate_client_frames_are_rejected_as_ambiguous() -> None:
    with pytest.raises(RoutingError, match="one frame per client_id"):
        route_sources(
            [
                RoutedSource(1, _frame(0.1), 1 << REGIE_BUS_ID),
                RoutedSource(1, _frame(0.2), 1 << PLATEAU_BUS_ID),
            ]
        )


@pytest.mark.parametrize(
    "config, error_type, message",
    [
        (RoutingConfig, TypeError, "config"),
        ({"bus_gain_db": {}}, TypeError, "config"),
    ],
)
def test_route_sources_requires_a_config_instance(
    config: object,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        route_sources([], config)  # type: ignore[arg-type]


def test_config_rejects_unknown_nonfinite_or_ambiguous_values() -> None:
    with pytest.raises(RoutingError, match="unknown gain bus"):
        RoutingConfig(bus_gain_db={99: 0.0})
    with pytest.raises(RoutingError, match="must be finite"):
        RoutingConfig(bus_gain_db={REGIE_BUS_ID: float("nan")})
    with pytest.raises(TypeError, match="must be bool"):
        RoutingConfig(feed_to_regie={PLATEAU_BUS_ID: 1})
    with pytest.raises(RoutingError, match="cannot feed itself"):
        RoutingConfig(feed_to_regie={REGIE_BUS_ID: True})


def test_destination_validation_rejects_missing_or_nonfinite_return() -> None:
    with pytest.raises(RoutingError, match="return_frame is required"):
        build_destination_mix(
            _frame(0.0),
            None,
            listen_regie=False,
            listen_return=True,
        )
    with pytest.raises(RoutingError, match="NaN"):
        build_destination_mix(
            _frame(0.0),
            None,
            listen_regie=False,
            listen_return=True,
            return_frame=np.full((FRAME_SAMPLES,), np.nan, dtype=np.float32),
        )
