import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

from .constants import (
    AUDIO_PACKET_DIRECTION_FLAGS,
    AUDIO_PACKET_FLAG_CLIENT_TO_SERVER,
    AUDIO_PACKET_FLAG_SERVER_TO_CLIENT,
    AUDIO_PACKET_V2_HEADER_BYTES,
    AUDIO_PACKET_V2_MAGIC,
    AUDIO_PACKET_V2_RESERVED_STREAM_ID,
    AUDIO_PACKET_V2_VERSION,
    MAX_UDP_PAYLOAD_BYTES,
)

_HEADER_V2_STRUCT = struct.Struct("!4sBBHQIIIHH")

if _HEADER_V2_STRUCT.size != AUDIO_PACKET_V2_HEADER_BYTES:
    raise RuntimeError(
        "audio packet V2 header constant does not match its binary schema: "
        f"{AUDIO_PACKET_V2_HEADER_BYTES} != {_HEADER_V2_STRUCT.size}"
    )


class AudioPacketDirection(IntEnum):
    """Exclusive direction flags carried by every V2 audio datagram."""

    CLIENT_TO_SERVER = AUDIO_PACKET_FLAG_CLIENT_TO_SERVER
    SERVER_TO_CLIENT = AUDIO_PACKET_FLAG_SERVER_TO_CLIENT


_VALID_V2_DIRECTION_FLAGS: Final[frozenset[int]] = frozenset(
    int(direction) for direction in AudioPacketDirection
)


class AudioPacketValidationError(ValueError):
    """Structured wire rejection used by transport-level statistics."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = str(reason)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AudioPacketV2:
    version: int
    header_size: int
    flags: int
    session_token: int
    client_id: int
    timestamp_ms: int
    sequence_number: int
    stream_id: int
    bus_mask: int
    payload: bytes

    @property
    def direction(self) -> AudioPacketDirection:
        return AudioPacketDirection(self.flags)


def _require_uint(name: str, value: int, bits: int, *, nonzero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    maximum = (1 << int(bits)) - 1
    if value < 0 or value > maximum or (nonzero and value == 0):
        suffix = " and non-zero" if nonzero else ""
        raise ValueError(f"{name} must be between {1 if nonzero else 0} and {maximum}{suffix}")
    return int(value)


def _require_v2_direction(direction: AudioPacketDirection | int) -> int:
    try:
        flags = int(direction)
    except (TypeError, ValueError) as exc:
        raise ValueError("direction must be a valid AudioPacketDirection") from exc
    if (
        isinstance(direction, bool)
        or flags & ~AUDIO_PACKET_DIRECTION_FLAGS
        or flags not in _VALID_V2_DIRECTION_FLAGS
    ):
        raise ValueError(
            "direction must contain exactly one supported direction flag "
            f"(0x{AUDIO_PACKET_FLAG_CLIENT_TO_SERVER:04x} or "
            f"0x{AUDIO_PACKET_FLAG_SERVER_TO_CLIENT:04x})"
        )
    return flags


def pack_audio_packet_v2(
    *,
    session_token: int,
    client_id: int,
    timestamp_ms: int,
    sequence_number: int,
    stream_id: int = AUDIO_PACKET_V2_RESERVED_STREAM_ID,
    bus_mask: int,
    direction: AudioPacketDirection | int,
    payload: bytes,
) -> bytes:
    """Build a strict, self-identifying V2 audio datagram.

    V2 intentionally rejects empty Opus payloads. Packet-loss concealment is a
    local jitter-buffer concern and must never be represented by a wire packet.
    """

    try:
        encoded_payload = memoryview(payload).tobytes()
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be bytes-like") from exc
    if not encoded_payload:
        raise ValueError("payload must not be empty")
    if len(encoded_payload) > MAX_UDP_PAYLOAD_BYTES:
        raise ValueError(f"payload too large: {len(encoded_payload)} bytes")

    token = _require_uint("session_token", session_token, 64, nonzero=True)
    cid = _require_uint("client_id", client_id, 32)
    timestamp = _require_uint("timestamp_ms", timestamp_ms, 32)
    sequence = _require_uint("sequence_number", sequence_number, 32)
    stream = _require_uint("stream_id", stream_id, 16)
    if stream != AUDIO_PACKET_V2_RESERVED_STREAM_ID:
        raise ValueError(
            f"stream_id is reserved in protocol V2 and must be {AUDIO_PACKET_V2_RESERVED_STREAM_ID}"
        )
    routing_mask = _require_uint("bus_mask", bus_mask, 16)
    flags = _require_v2_direction(direction)

    header = _HEADER_V2_STRUCT.pack(
        AUDIO_PACKET_V2_MAGIC,
        AUDIO_PACKET_V2_VERSION,
        AUDIO_PACKET_V2_HEADER_BYTES,
        flags,
        token,
        cid,
        timestamp,
        sequence,
        stream,
        routing_mask,
    )
    return header + encoded_payload


def unpack_audio_packet_v2(data: bytes) -> AudioPacketV2:
    """Parse and validate one V2 audio datagram without legacy fallback."""

    try:
        packet_size = len(data)
    except TypeError as exc:
        raise AudioPacketValidationError("invalid_packet", "packet must be bytes-like") from exc
    if packet_size < AUDIO_PACKET_V2_HEADER_BYTES:
        raise AudioPacketValidationError(
            "invalid_packet", f"packet too small for V2 header: {packet_size} bytes"
        )
    if packet_size > AUDIO_PACKET_V2_HEADER_BYTES + MAX_UDP_PAYLOAD_BYTES:
        raise AudioPacketValidationError("invalid_packet", f"packet too large: {packet_size} bytes")

    try:
        (
            magic,
            version,
            header_size,
            flags,
            session_token,
            client_id,
            timestamp_ms,
            sequence_number,
            stream_id,
            bus_mask,
        ) = _HEADER_V2_STRUCT.unpack_from(data, 0)
    except (struct.error, TypeError) as exc:
        raise AudioPacketValidationError(
            "invalid_packet", "packet does not contain a valid V2 header"
        ) from exc

    if magic != AUDIO_PACKET_V2_MAGIC:
        raise AudioPacketValidationError("invalid_packet", f"invalid audio packet magic: {magic!r}")
    if version != AUDIO_PACKET_V2_VERSION:
        raise AudioPacketValidationError(
            "invalid_packet", f"unsupported audio packet version: {version}"
        )
    if header_size != AUDIO_PACKET_V2_HEADER_BYTES:
        raise AudioPacketValidationError("invalid_packet", f"invalid V2 header size: {header_size}")
    try:
        _require_v2_direction(flags)
    except ValueError as exc:
        raise AudioPacketValidationError("invalid_direction", str(exc)) from exc
    try:
        _require_uint("session_token", session_token, 64, nonzero=True)
    except ValueError as exc:
        raise AudioPacketValidationError("invalid_token", str(exc)) from exc
    if stream_id != AUDIO_PACKET_V2_RESERVED_STREAM_ID:
        raise AudioPacketValidationError(
            "invalid_route",
            f"stream_id is reserved in protocol V2 and must be {AUDIO_PACKET_V2_RESERVED_STREAM_ID}",
        )

    payload = bytes(data[AUDIO_PACKET_V2_HEADER_BYTES:])
    if not payload:
        raise AudioPacketValidationError("invalid_packet", "payload must not be empty")

    return AudioPacketV2(
        version=int(version),
        header_size=int(header_size),
        flags=int(flags),
        session_token=int(session_token),
        client_id=int(client_id),
        timestamp_ms=int(timestamp_ms),
        sequence_number=int(sequence_number),
        stream_id=int(stream_id),
        bus_mask=int(bus_mask),
        payload=payload,
    )
