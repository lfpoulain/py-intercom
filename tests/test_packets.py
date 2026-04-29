import pytest

from py_intercom.common.constants import MAX_UDP_PAYLOAD_BYTES, PACKET_HEADER_BYTES
from py_intercom.common.packets import pack_audio_packet, unpack_audio_packet


def test_pack_unpack_roundtrip():
    payload = b"\x01\x02\x03opus_payload"
    pkt = pack_audio_packet(client_id=42, timestamp_ms=12345, sequence_number=7, payload=payload)
    assert len(pkt) == PACKET_HEADER_BYTES + len(payload)

    out = unpack_audio_packet(pkt)
    assert out.client_id == 42
    assert out.timestamp_ms == 12345
    assert out.sequence_number == 7
    assert out.payload == payload


def test_pack_truncates_high_bits():
    # Header fields are masked to 32 bits, so values > 2**32 wrap.
    pkt = pack_audio_packet(client_id=(1 << 32) + 7, timestamp_ms=0, sequence_number=0, payload=b"")
    out = unpack_audio_packet(pkt)
    assert out.client_id == 7


def test_pack_rejects_oversize_payload():
    too_big = b"\x00" * (MAX_UDP_PAYLOAD_BYTES + 1)
    with pytest.raises(ValueError):
        pack_audio_packet(client_id=0, timestamp_ms=0, sequence_number=0, payload=too_big)


def test_unpack_rejects_too_small():
    with pytest.raises(ValueError):
        unpack_audio_packet(b"\x00" * (PACKET_HEADER_BYTES - 1))


def test_unpack_rejects_too_large():
    big = b"\x00" * (PACKET_HEADER_BYTES + MAX_UDP_PAYLOAD_BYTES + 1)
    with pytest.raises(ValueError):
        unpack_audio_packet(big)


def test_unpack_empty_payload_ok():
    pkt = pack_audio_packet(client_id=1, timestamp_ms=2, sequence_number=3, payload=b"")
    out = unpack_audio_packet(pkt)
    assert out.payload == b""
