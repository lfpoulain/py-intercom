from __future__ import annotations

import pytest

from py_intercom.common.control_protocol import (
    CONTROL_PROTOCOL_VERSION,
    ControlProtocolError,
    JsonLineBuffer,
    encode_control_message,
    format_session_token,
    parse_session_token,
    require_control_protocol_version,
)


def test_json_line_buffer_handles_fragmented_and_batched_messages() -> None:
    parser = JsonLineBuffer()

    assert parser.feed(b'{"type":"pi') == []
    assert parser.feed(b'ng"}\n{"type":"pong"}\n') == [
        {"type": "ping"},
        {"type": "pong"},
    ]
    assert parser.buffered_bytes == 0


def test_json_line_buffer_rejects_unbounded_line_and_resets() -> None:
    parser = JsonLineBuffer(max_line_bytes=8, max_buffer_bytes=16)

    with pytest.raises(ControlProtocolError, match="line exceeds"):
        parser.feed(b"x" * 9)

    assert parser.buffered_bytes == 0


@pytest.mark.parametrize("payload", [b"[]\n", b"not-json\n", b"\xff\n"])
def test_json_line_buffer_rejects_invalid_messages(payload: bytes) -> None:
    parser = JsonLineBuffer()
    with pytest.raises(ControlProtocolError):
        parser.feed(payload)


def test_encode_control_message_enforces_limit() -> None:
    assert encode_control_message({"type": "ping"}).endswith(b"\n")
    with pytest.raises(ControlProtocolError):
        encode_control_message({"payload": "x" * (64 * 1024)})


def test_control_protocol_v2_explicitly_refuses_legacy_v1() -> None:
    assert require_control_protocol_version(CONTROL_PROTOCOL_VERSION) == 2
    with pytest.raises(ControlProtocolError, match="V1 is intentionally refused"):
        require_control_protocol_version(1)


def test_session_token_has_strict_lowercase_64_bit_wire_format() -> None:
    assert format_session_token(1) == "0000000000000001"
    assert format_session_token(0xFEDCBA9876543210) == "fedcba9876543210"
    assert parse_session_token("fedcba9876543210") == 0xFEDCBA9876543210


@pytest.mark.parametrize(
    "value",
    [None, 1, "1", "0000000000000000", "000000000000000A", "g000000000000001", "00000000000000001"],
)
def test_session_token_parser_rejects_ambiguous_or_zero_values(value) -> None:
    with pytest.raises(ControlProtocolError, match="session_token"):
        parse_session_token(value)


@pytest.mark.parametrize("token", [0, -1, 1 << 64, True, "1"])
def test_session_token_formatter_rejects_out_of_range_values(token) -> None:
    with pytest.raises(ControlProtocolError, match="session token"):
        format_session_token(token)
