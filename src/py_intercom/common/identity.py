from __future__ import annotations

import zlib


MAX_CLIENT_UUID_LENGTH = 128


class ClientIdentityError(ValueError):
    """Raised when the wire client id and persistent identity disagree."""


def client_id_from_uuid(client_uuid: str) -> int:
    try:
        return int(zlib.crc32(str(client_uuid).encode("utf-8")) & 0xFFFFFFFF)
    except Exception:
        return 0


def validate_client_identity(client_id: object, client_uuid: object) -> tuple[int, str]:
    """Validate the canonical UUID-to-wire-id mapping used by protocol V2.

    The 32-bit id remains a compact routing key; the persistent UUID is the
    canonical identity.  A peer may not choose the id independently because
    doing so could replace another live session before its token is checked.
    """

    if isinstance(client_id, bool) or not isinstance(client_id, int):
        raise ClientIdentityError("client_id must be an unsigned 32-bit integer")
    if not 0 <= client_id <= 0xFFFFFFFF:
        raise ClientIdentityError("client_id must be an unsigned 32-bit integer")
    if not isinstance(client_uuid, str):
        raise ClientIdentityError("client_uuid must be a string")
    canonical_uuid = client_uuid.strip()
    if not canonical_uuid:
        raise ClientIdentityError("client_uuid must not be empty")
    if len(canonical_uuid) > MAX_CLIENT_UUID_LENGTH:
        raise ClientIdentityError(
            f"client_uuid exceeds {MAX_CLIENT_UUID_LENGTH} characters"
        )

    expected = client_id_from_uuid(canonical_uuid)
    if expected == 0:
        raise ClientIdentityError("client_uuid maps to the reserved client id 0")
    if int(client_id) != int(expected):
        raise ClientIdentityError(
            f"client_id does not match client_uuid (expected {expected})"
        )
    return int(expected), canonical_uuid


def find_client_id_collisions(client_uuids: object) -> dict[int, tuple[str, ...]]:
    """Return every 32-bit id shared by several distinct UUID strings."""

    try:
        values = list(client_uuids)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("client_uuids must be iterable") from exc

    by_id: dict[int, set[str]] = {}
    for value in values:
        if not isinstance(value, str):
            raise TypeError("client_uuids must contain strings")
        canonical = value.strip()
        if not canonical:
            continue
        client_id = client_id_from_uuid(canonical)
        if client_id == 0:
            continue
        by_id.setdefault(int(client_id), set()).add(canonical)

    return {
        client_id: tuple(sorted(uuids))
        for client_id, uuids in by_id.items()
        if len(uuids) > 1
    }
