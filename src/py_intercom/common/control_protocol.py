from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


CONTROL_PROTOCOL_VERSION = 2
MAX_CONTROL_LINE_BYTES = 64 * 1024
MAX_CONTROL_BUFFER_BYTES = 128 * 1024
CONTROL_HELLO_TIMEOUT_S = 3.0
MAX_CONTROL_SESSIONS = 64
SESSION_TOKEN_HEX_LENGTH = 16

_SESSION_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")


class ControlProtocolError(ValueError):
    pass


def require_control_protocol_version(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or int(value) != int(CONTROL_PROTOCOL_VERSION)
    ):
        legacy_note = (
            " Protocol V1 is intentionally refused because it has no "
            "authenticated UDP session token."
            if value == 1
            else ""
        )
        raise ControlProtocolError(
            f"unsupported control protocol version {value!r}; "
            f"expected {CONTROL_PROTOCOL_VERSION}.{legacy_note}"
        )
    return int(value)


def format_session_token(token: int) -> str:
    if isinstance(token, bool) or not isinstance(token, int) or not 1 <= token <= 0xFFFFFFFFFFFFFFFF:
        raise ControlProtocolError("session token must be a non-zero unsigned 64-bit integer")
    return f"{int(token):016x}"


def parse_session_token(value: object) -> int:
    if not isinstance(value, str) or _SESSION_TOKEN_RE.fullmatch(value) is None:
        raise ControlProtocolError(
            f"session_token must be exactly {SESSION_TOKEN_HEX_LENGTH} lowercase hexadecimal characters"
        )
    token = int(value, 16)
    if token == 0:
        raise ControlProtocolError("session_token must not be zero")
    return token


def encode_control_message(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict):
        raise TypeError("control message must be an object")
    encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_CONTROL_LINE_BYTES:
        raise ControlProtocolError(f"control message exceeds {MAX_CONTROL_LINE_BYTES} bytes")
    return encoded


@dataclass
class JsonLineBuffer:
    max_line_bytes: int = MAX_CONTROL_LINE_BYTES
    max_buffer_bytes: int = MAX_CONTROL_BUFFER_BYTES
    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("control chunk must be bytes-like")
        self._buffer.extend(bytes(chunk))
        if len(self._buffer) > int(self.max_buffer_bytes):
            self.reset()
            raise ControlProtocolError(f"control buffer exceeds {self.max_buffer_bytes} bytes")

        messages: list[dict[str, Any]] = []
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                if len(self._buffer) > int(self.max_line_bytes):
                    self.reset()
                    raise ControlProtocolError(f"control line exceeds {self.max_line_bytes} bytes")
                break

            line = bytes(self._buffer[:newline_index])
            del self._buffer[: newline_index + 1]
            if not line.strip():
                continue
            if len(line) > int(self.max_line_bytes):
                self.reset()
                raise ControlProtocolError(f"control line exceeds {self.max_line_bytes} bytes")
            try:
                decoded = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ControlProtocolError(f"invalid control JSON: {exc}") from exc
            if not isinstance(decoded, dict):
                raise ControlProtocolError("control message must be a JSON object")
            messages.append(decoded)

        return messages
