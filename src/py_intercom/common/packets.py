import struct
from dataclasses import dataclass

from .constants import MAX_UDP_PAYLOAD_BYTES, PACKET_HEADER_BYTES


_HEADER_STRUCT = struct.Struct("!III")


@dataclass(frozen=True)
class AudioPacket:
    client_id: int
    timestamp_ms: int
    sequence_number: int
    payload: bytes


def pack_audio_packet(client_id: int, timestamp_ms: int, sequence_number: int, payload: bytes) -> bytes:
    if len(payload) > MAX_UDP_PAYLOAD_BYTES:
        raise ValueError(f"payload too large: {len(payload)} bytes")
    header = _HEADER_STRUCT.pack(client_id & 0xFFFFFFFF, timestamp_ms & 0xFFFFFFFF, sequence_number & 0xFFFFFFFF)
    return header + payload


def unpack_audio_packet(data: bytes) -> AudioPacket:
    if len(data) < PACKET_HEADER_BYTES:
        raise ValueError("packet too small")
    client_id, timestamp_ms, sequence_number = _HEADER_STRUCT.unpack_from(data, 0)
    payload = data[PACKET_HEADER_BYTES:]
    return AudioPacket(client_id=client_id, timestamp_ms=timestamp_ms, sequence_number=sequence_number, payload=payload)
