import struct
from dataclasses import FrozenInstanceError

import pytest

from py_intercom.common.constants import (
    AUDIO_PACKET_FLAG_CLIENT_TO_SERVER,
    AUDIO_PACKET_FLAG_SERVER_TO_CLIENT,
    AUDIO_PACKET_V2_HEADER_BYTES,
    AUDIO_PACKET_V2_MAGIC,
    AUDIO_PACKET_V2_VERSION,
    MAX_UDP_PAYLOAD_BYTES,
)
from py_intercom.common.packets import (
    AudioPacketDirection,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)


def _v2_packet(**overrides):
    fields = {
        "session_token": 0x0123456789ABCDEF,
        "client_id": 42,
        "timestamp_ms": 0x10203040,
        "sequence_number": 0xFFFFFFFE,
        "stream_id": 0,
        "bus_mask": 0b101,
        "direction": AudioPacketDirection.CLIENT_TO_SERVER,
        "payload": b"opus-v2",
    }
    fields.update(overrides)
    return pack_audio_packet_v2(**fields)


def test_v2_header_schema_and_roundtrip_are_exact():
    encoded = _v2_packet()

    assert len(encoded) == AUDIO_PACKET_V2_HEADER_BYTES + len(b"opus-v2")
    assert struct.unpack("!4sBBHQIIIHH", encoded[:AUDIO_PACKET_V2_HEADER_BYTES]) == (
        AUDIO_PACKET_V2_MAGIC,
        AUDIO_PACKET_V2_VERSION,
        AUDIO_PACKET_V2_HEADER_BYTES,
        AUDIO_PACKET_FLAG_CLIENT_TO_SERVER,
        0x0123456789ABCDEF,
        42,
        0x10203040,
        0xFFFFFFFE,
        0,
        0b101,
    )

    packet = unpack_audio_packet_v2(encoded)
    assert packet.version == AUDIO_PACKET_V2_VERSION
    assert packet.header_size == AUDIO_PACKET_V2_HEADER_BYTES
    assert packet.flags == AUDIO_PACKET_FLAG_CLIENT_TO_SERVER
    assert packet.direction is AudioPacketDirection.CLIENT_TO_SERVER
    assert packet.session_token == 0x0123456789ABCDEF
    assert packet.client_id == 42
    assert packet.timestamp_ms == 0x10203040
    assert packet.sequence_number == 0xFFFFFFFE
    assert packet.stream_id == 0
    assert packet.bus_mask == 0b101
    assert packet.payload == b"opus-v2"

    with pytest.raises(FrozenInstanceError):
        packet.bus_mask = 0


def test_v2_server_to_client_direction_roundtrip():
    packet = unpack_audio_packet_v2(
        _v2_packet(direction=AudioPacketDirection.SERVER_TO_CLIENT, bus_mask=0)
    )
    assert packet.flags == AUDIO_PACKET_FLAG_SERVER_TO_CLIENT
    assert packet.direction is AudioPacketDirection.SERVER_TO_CLIENT
    assert packet.bus_mask == 0


@pytest.mark.parametrize("direction", [0, 3, 0x8001, True, -1])
def test_v2_pack_rejects_invalid_or_ambiguous_direction(direction):
    with pytest.raises(ValueError, match="direction"):
        _v2_packet(direction=direction)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_token", 0),
        ("session_token", 1 << 64),
        ("client_id", -1),
        ("client_id", 1 << 32),
        ("timestamp_ms", 1 << 32),
        ("sequence_number", -1),
        ("sequence_number", 1 << 32),
        ("bus_mask", -1),
        ("bus_mask", 1 << 16),
        ("stream_id", 1),
    ],
)
def test_v2_pack_rejects_invalid_fields(field, value):
    with pytest.raises(ValueError):
        _v2_packet(**{field: value})


@pytest.mark.parametrize("payload", [b"", bytearray(), memoryview(b"")])
def test_v2_pack_rejects_empty_payload(payload):
    with pytest.raises(ValueError, match="empty"):
        _v2_packet(payload=payload)


def test_v2_payload_bounds_and_bytes_like_copy():
    source = bytearray(b"opus")
    encoded = _v2_packet(payload=source)
    source[:] = b"xxxx"
    assert unpack_audio_packet_v2(encoded).payload == b"opus"

    with pytest.raises(ValueError, match="too large"):
        _v2_packet(payload=b"x" * (MAX_UDP_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError, match="bytes-like"):
        _v2_packet(payload=4)


@pytest.mark.parametrize(
    ("offset", "replacement", "message"),
    [
        (0, b"BAD!", "magic"),
        (4, b"\x03", "version"),
        (5, b"\x1f", "header size"),
        (5, b"\x21", "header size"),
        (6, b"\x00\x00", "direction"),
        (6, b"\x00\x03", "direction"),
        (6, b"\x80\x01", "direction"),
        (8, b"\x00" * 8, "session_token"),
        (28, b"\x00\x01", "stream_id"),
    ],
)
def test_v2_unpack_rejects_invalid_header_fields(offset, replacement, message):
    encoded = bytearray(_v2_packet())
    encoded[offset : offset + len(replacement)] = replacement
    with pytest.raises(ValueError, match=message):
        unpack_audio_packet_v2(encoded)


def test_v2_unpack_rejects_invalid_datagram_sizes_and_empty_payload():
    encoded = _v2_packet()
    with pytest.raises(ValueError, match="too small"):
        unpack_audio_packet_v2(encoded[: AUDIO_PACKET_V2_HEADER_BYTES - 1])
    with pytest.raises(ValueError, match="empty"):
        unpack_audio_packet_v2(encoded[:AUDIO_PACKET_V2_HEADER_BYTES])
    with pytest.raises(ValueError, match="too large"):
        unpack_audio_packet_v2(
            encoded[:AUDIO_PACKET_V2_HEADER_BYTES]
            + (b"x" * (MAX_UDP_PAYLOAD_BYTES + 1))
        )
