from __future__ import annotations

import math
import operator
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import numpy as np

from .buses import FIXED_BUS_IDS, FIXED_BUS_MASK, FIXED_BUSES, REGIE_BUS_ID
from .constants import FRAME_SAMPLES

VALID_BUS_MASK = FIXED_BUS_MASK


class RoutingError(ValueError):
    """Raised when a routing value would make the audio result ambiguous or unsafe."""


def _integer(value: object, *, name: str) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{name} must be an integer, not bool")
    try:
        return int(operator.index(value))
    except TypeError as exc:
        raise TypeError(f"{name} must be an integer") from exc


def _strict_frame(frame: object, *, name: str, copy: bool) -> np.ndarray:
    if not isinstance(frame, np.ndarray):
        raise TypeError(f"{name} must be a numpy.ndarray")
    if frame.dtype != np.dtype(np.float32):
        raise TypeError(f"{name} must use float32 samples, got {frame.dtype}")
    if frame.shape != (int(FRAME_SAMPLES),):
        raise RoutingError(
            f"{name} must have shape ({int(FRAME_SAMPLES)},), got {frame.shape}"
        )
    if not bool(np.isfinite(frame).all()):
        raise RoutingError(f"{name} contains NaN or infinite samples")
    return np.array(frame, dtype=np.float32, copy=True) if copy else frame


def _finite_float(value: object, *, name: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{name} must be a finite number, not bool")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise RoutingError(f"{name} must be finite")
    return result


def _immutable_frame(frame: np.ndarray) -> np.ndarray:
    result = np.array(frame, dtype=np.float32, copy=True)
    result.setflags(write=False)
    return result


def _immutable_frame_map(frames: Mapping[int, np.ndarray]) -> Mapping[int, np.ndarray]:
    return MappingProxyType(
        {int(key): _immutable_frame(value) for key, value in frames.items()}
    )


@dataclass(frozen=True, slots=True)
class RoutedSource:
    """One validated mono source and its explicit, fail-closed bus routes."""

    client_id: int
    frame: np.ndarray = field(repr=False, compare=False)
    bus_mask: int

    def __post_init__(self) -> None:
        client_id = _integer(self.client_id, name="client_id")
        if client_id < 0:
            raise RoutingError("client_id must be non-negative")

        bus_mask = _integer(self.bus_mask, name="bus_mask")
        if bus_mask < 0:
            raise RoutingError("bus_mask must be non-negative")

        # Copying here makes a source a stable snapshot even if the decoder
        # reuses or mutates its working buffer after this object is created.
        frame = _strict_frame(self.frame, name="frame", copy=True)
        frame.setflags(write=False)
        object.__setattr__(self, "client_id", client_id)
        object.__setattr__(self, "bus_mask", bus_mask)
        object.__setattr__(self, "frame", frame)

    @property
    def valid_bus_mask(self) -> int:
        """Return only catalogued routes; unknown bits are deliberately closed."""

        return int(self.bus_mask) & int(VALID_BUS_MASK)


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Immutable gain and Régie-monitor policy for the fixed bus catalogue."""

    bus_gain_db: Mapping[int, float] = field(default_factory=dict)
    feed_to_regie: Mapping[int, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.bus_gain_db, Mapping):
            raise TypeError("bus_gain_db must be a mapping")
        if not isinstance(self.feed_to_regie, Mapping):
            raise TypeError("feed_to_regie must be a mapping")

        unknown_gain_ids = set(self.bus_gain_db) - set(FIXED_BUS_IDS)
        unknown_feed_ids = set(self.feed_to_regie) - set(FIXED_BUS_IDS)
        if unknown_gain_ids:
            unknown = sorted(repr(value) for value in unknown_gain_ids)
            raise RoutingError(f"unknown gain bus ids: {unknown!r}")
        if unknown_feed_ids:
            unknown = sorted(repr(value) for value in unknown_feed_ids)
            raise RoutingError(f"unknown feed bus ids: {unknown!r}")

        gains: dict[int, float] = {}
        feeds: dict[int, bool] = {}
        for definition in FIXED_BUSES:
            bus_id = int(definition.bus_id)
            gain_db = _finite_float(
                self.bus_gain_db.get(bus_id, 0.0),
                name=f"bus_gain_db[{bus_id}]",
            )
            try:
                gain_linear = math.pow(10.0, gain_db / 20.0)
            except OverflowError as exc:
                raise RoutingError(f"bus_gain_db[{bus_id}] is too large") from exc
            if not math.isfinite(gain_linear) or gain_linear > np.finfo(np.float32).max:
                raise RoutingError(f"bus_gain_db[{bus_id}] is too large")
            gains[bus_id] = gain_db

            feed = self.feed_to_regie.get(
                bus_id,
                bool(definition.default_feed_to_regie),
            )
            if not isinstance(feed, (bool, np.bool_)):
                raise TypeError(f"feed_to_regie[{bus_id}] must be bool")
            feeds[bus_id] = bool(feed)

        if feeds[REGIE_BUS_ID]:
            raise RoutingError("Régie cannot feed itself")

        object.__setattr__(self, "bus_gain_db", MappingProxyType(gains))
        object.__setattr__(self, "feed_to_regie", MappingProxyType(feeds))


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """Unclipped linear bus mixes and the deduplicated Régie monitor mix."""

    bus_frames: Mapping[int, np.ndarray]
    regie_linear: np.ndarray = field(repr=False, compare=False)
    regie_contribution_by_client: Mapping[int, np.ndarray] = field(
        repr=False,
        compare=False,
    )


def _gain_frame(frame: np.ndarray, gain_db: float, *, context: str) -> np.ndarray:
    gain_linear = math.pow(10.0, float(gain_db) / 20.0)
    try:
        with np.errstate(over="raise", invalid="raise"):
            result = np.multiply(frame, gain_linear, dtype=np.float32)
    except FloatingPointError as exc:
        raise RoutingError(f"{context} overflowed while applying gain") from exc
    if not bool(np.isfinite(result).all()):
        raise RoutingError(f"{context} produced non-finite samples")
    return result


def _add_frame(target: np.ndarray, source: np.ndarray, *, context: str) -> None:
    try:
        with np.errstate(over="raise", invalid="raise"):
            np.add(target, source, out=target)
    except FloatingPointError as exc:
        raise RoutingError(f"{context} overflowed while summing sources") from exc


def route_sources(
    sources: Iterable[RoutedSource],
    config: RoutingConfig | None = None,
) -> RoutingResult:
    """Route sources to all fixed buses without clipping or peak limiting.

    Unknown mask bits never open a route. A source selected on several buses is
    present on every corresponding physical bus, but appears only once in the
    Régie monitor. Direct Régie routing wins; otherwise the first fed bus in
    ``FIXED_BUSES`` order wins. This preserves current PTT loudness and makes the
    formerly order-dependent rule explicit until a true additive matrix exists.
    """

    routing = RoutingConfig() if config is None else config
    if not isinstance(routing, RoutingConfig):
        raise TypeError("config must be a RoutingConfig")

    source_list = list(sources)
    if not all(isinstance(source, RoutedSource) for source in source_list):
        raise TypeError("sources must contain only RoutedSource values")
    client_ids = [int(source.client_id) for source in source_list]
    if len(client_ids) != len(set(client_ids)):
        raise RoutingError("sources must contain at most one frame per client_id")

    bus_frames: dict[int, np.ndarray] = {
        int(definition.bus_id): np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)
        for definition in FIXED_BUSES
    }
    contribution_by_bus: dict[int, dict[int, np.ndarray]] = {
        int(definition.bus_id): {} for definition in FIXED_BUSES
    }

    for source in source_list:
        valid_mask = int(source.valid_bus_mask)
        for definition in FIXED_BUSES:
            bus_id = int(definition.bus_id)
            if not (valid_mask & (1 << bus_id)):
                continue
            contribution = _gain_frame(
                source.frame,
                routing.bus_gain_db[bus_id],
                context=f"client {source.client_id} on bus {bus_id}",
            )
            contribution_by_bus[bus_id][int(source.client_id)] = contribution
            _add_frame(
                bus_frames[bus_id],
                contribution,
                context=f"bus {bus_id}",
            )

    regie_contributions: dict[int, np.ndarray] = {}
    for source in source_list:
        client_id = int(source.client_id)
        direct = contribution_by_bus[REGIE_BUS_ID].get(client_id)
        if direct is not None:
            regie_contributions[client_id] = direct
            continue

        for definition in FIXED_BUSES:
            bus_id = int(definition.bus_id)
            if bus_id == int(REGIE_BUS_ID):
                continue
            if not routing.feed_to_regie[bus_id]:
                continue
            fed = contribution_by_bus[bus_id].get(client_id)
            if fed is not None:
                regie_contributions[client_id] = fed
                break

    regie_linear = np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)
    for contribution in regie_contributions.values():
        _add_frame(regie_linear, contribution, context="Régie monitor")

    return RoutingResult(
        bus_frames=_immutable_frame_map(bus_frames),
        regie_linear=_immutable_frame(regie_linear),
        regie_contribution_by_client=_immutable_frame_map(regie_contributions),
    )


def build_destination_mix(
    regie_linear: np.ndarray,
    own_regie_contribution: np.ndarray | None,
    *,
    listen_regie: bool,
    return_frame: np.ndarray | None = None,
    listen_return: bool = False,
    return_gain_db: float = 0.0,
    limit: float = 0.99,
) -> np.ndarray:
    """Build one destination mix-minus, then apply its final peak limiter.

    The function never limits the shared Régie mix. It first subtracts the
    destination's exact linear contribution, then adds Return, and only then
    limits the completed destination. Inputs are never mutated.
    """

    linear = _strict_frame(regie_linear, name="regie_linear", copy=False)
    if own_regie_contribution is not None:
        own = _strict_frame(
            own_regie_contribution,
            name="own_regie_contribution",
            copy=False,
        )
    else:
        own = None

    if not isinstance(listen_regie, (bool, np.bool_)):
        raise TypeError("listen_regie must be bool")
    if not isinstance(listen_return, (bool, np.bool_)):
        raise TypeError("listen_return must be bool")

    mix = (
        np.array(linear, dtype=np.float32, copy=True)
        if bool(listen_regie)
        else np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)
    )
    if bool(listen_regie) and own is not None:
        try:
            with np.errstate(over="raise", invalid="raise"):
                np.subtract(mix, own, out=mix)
        except FloatingPointError as exc:
            raise RoutingError(
                "mix-minus overflowed while subtracting contribution"
            ) from exc

    gain_db = _finite_float(return_gain_db, name="return_gain_db")
    if bool(listen_return):
        if return_frame is None:
            raise RoutingError("return_frame is required when listen_return is true")
        return_audio = _strict_frame(return_frame, name="return_frame", copy=False)
        gained_return = _gain_frame(
            return_audio,
            gain_db,
            context="Return destination",
        )
        _add_frame(mix, gained_return, context="destination mix")
    elif return_frame is not None:
        _strict_frame(return_frame, name="return_frame", copy=False)

    peak_limit = _finite_float(limit, name="limit")
    if not 0.0 < peak_limit <= 1.0:
        raise RoutingError("limit must be greater than 0 and at most 1")

    peak = float(np.max(np.abs(mix)))
    if peak > 1.0:
        mix *= float(peak_limit) / peak
    np.clip(mix, -1.0, 1.0, out=mix)
    if not bool(np.isfinite(mix).all()):
        raise RoutingError("destination mix produced non-finite samples")
    return mix.astype(np.float32, copy=False)
