from __future__ import annotations

import zlib


def client_id_from_uuid(client_uuid: str) -> int:
    try:
        return int(zlib.crc32(str(client_uuid).encode("utf-8")) & 0xFFFFFFFF)
    except Exception:
        return 0
