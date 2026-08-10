from __future__ import annotations

import pytest

from py_intercom.common import identity
from py_intercom.common.identity import (
    ClientIdentityError,
    client_id_from_uuid,
    find_client_id_collisions,
    validate_client_identity,
)


def test_validate_client_identity_accepts_only_derived_id() -> None:
    client_uuid = "8dfe8709-cdf5-412c-9294-01c742eb98d0"
    expected = client_id_from_uuid(client_uuid)

    assert validate_client_identity(expected, client_uuid) == (expected, client_uuid)
    with pytest.raises(ClientIdentityError, match="does not match"):
        validate_client_identity((expected + 1) & 0xFFFFFFFF, client_uuid)


@pytest.mark.parametrize("client_id", [True, -1, 1 << 32, "12"])
def test_validate_client_identity_rejects_invalid_wire_id(client_id: object) -> None:
    with pytest.raises(ClientIdentityError, match="client_id"):
        validate_client_identity(client_id, "uuid")


@pytest.mark.parametrize("client_uuid", [None, "", "   ", "x" * 129])
def test_validate_client_identity_rejects_invalid_uuid(client_uuid: object) -> None:
    with pytest.raises(ClientIdentityError, match="client_uuid"):
        validate_client_identity(1, client_uuid)


def test_reserved_zero_mapping_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(identity, "client_id_from_uuid", lambda _value: 0)
    with pytest.raises(ClientIdentityError, match="reserved"):
        validate_client_identity(0, "uuid")


def test_collision_report_is_deterministic_and_ignores_duplicates(monkeypatch) -> None:
    monkeypatch.setattr(
        identity,
        "client_id_from_uuid",
        lambda value: 42 if value in {"alpha", "bravo"} else 7,
    )

    collisions = find_client_id_collisions(["bravo", "alpha", "alpha", "charlie"])

    assert collisions == {42: ("alpha", "bravo")}


def test_collision_report_rejects_non_string_values() -> None:
    with pytest.raises(TypeError, match="strings"):
        find_client_id_collisions(["ok", 3])
