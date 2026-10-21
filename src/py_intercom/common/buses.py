from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class BusDefinition:
    bus_id: int
    key: str
    name: str
    color: str
    default_feed_to_regie: bool
    client_can_listen: bool


REGIE_BUS_ID: Final = 0
PLATEAU_BUS_ID: Final = 1
VMIX_BUS_ID: Final = 2

FIXED_BUSES: Final[tuple[BusDefinition, ...]] = (
    BusDefinition(
        bus_id=REGIE_BUS_ID,
        key="regie",
        name="Regie",
        color="#2ecc71",
        default_feed_to_regie=False,
        client_can_listen=True,
    ),
    BusDefinition(
        bus_id=PLATEAU_BUS_ID,
        key="plateau",
        name="Plateau",
        color="#f39c12",
        default_feed_to_regie=False,
        client_can_listen=False,
    ),
    BusDefinition(
        bus_id=VMIX_BUS_ID,
        key="vmix",
        name="VMix",
        color="#3498db",
        default_feed_to_regie=False,
        client_can_listen=False,
    ),
)

BUS_BY_ID: Final[Mapping[int, BusDefinition]] = MappingProxyType(
    {definition.bus_id: definition for definition in FIXED_BUSES}
)
FIXED_BUS_IDS: Final[frozenset[int]] = frozenset(BUS_BY_ID)
# One bit per bus, as carried by the audio packet header.
FIXED_BUS_MASK: Final[int] = sum(1 << bus_id for bus_id in FIXED_BUS_IDS)


def require_bus(bus_id: int) -> BusDefinition:
    try:
        return BUS_BY_ID[int(bus_id)]
    except (KeyError, TypeError, ValueError) as exc:
        valid = ", ".join(definition.name for definition in FIXED_BUSES)
        raise ValueError(f"unknown bus {bus_id!r}; expected one of: {valid}") from exc
