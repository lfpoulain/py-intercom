from __future__ import annotations

import pytest

from py_intercom.common.buses import (
    BUS_BY_ID,
    FIXED_BUSES,
    PLATEAU_BUS_ID,
    REGIE_BUS_ID,
    VMIX_BUS_ID,
    require_bus,
)


def test_fixed_bus_catalog_has_stable_ids_and_unique_keys() -> None:
    assert [bus.bus_id for bus in FIXED_BUSES] == [
        REGIE_BUS_ID,
        PLATEAU_BUS_ID,
        VMIX_BUS_ID,
    ]
    assert len({bus.key for bus in FIXED_BUSES}) == len(FIXED_BUSES)
    assert set(BUS_BY_ID) == {0, 1, 2}


def test_require_bus_rejects_unknown_ids() -> None:
    assert require_bus(0).key == "regie"
    with pytest.raises(ValueError, match="unknown bus"):
        require_bus(99)
