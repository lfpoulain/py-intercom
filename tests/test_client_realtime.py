from __future__ import annotations

import socket
import struct
import threading
import time

import numpy as np
import pytest

from py_intercom.client import client as client_mod
from py_intercom.client.client import ClientConfig, IntercomClient
from py_intercom.common.constants import FRAME_SAMPLES
from py_intercom.common.control_protocol import (
    CONTROL_PROTOCOL_VERSION,
    ControlProtocolError,
    encode_control_message,
    format_session_token,
)
from py_intercom.common.jitter_buffer import JitterFrame, JitterFrameMetadata
from py_intercom.common.packets import (
    AudioPacketDirection,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)


SESSION_TOKEN = 0x0123456789ABCDEF


def _legacy_audio_packet(
    client_id: int,
    timestamp_ms: int,
    sequence_number: int,
    payload: bytes,
) -> bytes:
    return struct.pack(
        "!III",
        int(client_id) & 0xFFFFFFFF,
        int(timestamp_ms) & 0xFFFFFFFF,
        int(sequence_number) & 0xFFFFFFFF,
    ) + bytes(payload)


class _FakeEncoder:
    def encode(self, frame: np.ndarray) -> bytes:
        assert int(frame.shape[0]) == int(FRAME_SAMPLES)
        return b"opus"


class _FakeDecoder:
    def decode(self, payload: bytes) -> np.ndarray:
        return np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)


@pytest.fixture
def client_factory(monkeypatch):
    monkeypatch.setattr(client_mod, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(client_mod, "OpusDecoder", _FakeDecoder)
    clients: list[IntercomClient] = []

    def make(*, server_port: int, control_port: int = 1) -> IntercomClient:
        client = IntercomClient(
            client_id=123,
            config=ClientConfig(
                server_ip="127.0.0.1",
                server_port=int(server_port),
                control_port=int(control_port),
            ),
        )
        clients.append(client)
        return client

    yield make

    for client in clients:
        try:
            client.stop()
        except Exception:
            pass


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_network_restart_uses_a_new_generation_and_stops_backoff(client_factory):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1], control_port=1)

    client._start_network_threads()
    first_event = client._stop
    first_generation = client._network_generation
    first_tx_thread = client._tx_thread
    first_playout_thread = client._playout_thread
    assert first_tx_thread is not None and first_tx_thread.is_alive()
    assert first_playout_thread is not None and first_playout_thread.is_alive()
    client._session_token = SESSION_TOKEN

    started = time.monotonic()
    assert client._stop_network() is True
    assert time.monotonic() - started < 1.5
    assert first_event.is_set()
    assert client._ctrl_thread is None
    assert client._rx_thread is None
    assert client._tx_thread is None
    assert client._playout_thread is None
    assert not first_tx_thread.is_alive()
    assert not first_playout_thread.is_alive()
    assert client._session_token == 0

    client._start_network_threads()
    assert client._network_generation == first_generation + 1
    assert client._stop is not first_event
    assert not client._stop.is_set()
    assert client._stop_network() is True
    udp_server.close()


def test_control_send_uses_bounded_protocol_encoder(client_factory):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1])

    class _RecordingControlSocket:
        def __init__(self) -> None:
            self.sent = b""

        def sendall(self, data: bytes) -> None:
            self.sent += bytes(data)

    control_socket = _RecordingControlSocket()
    message = {"type": "state", "name": "Régie"}
    client._control_send(control_socket, message)

    assert control_socket.sent == encode_control_message(message)
    udp_server.close()


def test_audio_gate_requires_welcome_before_initial_update(client_factory):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()

    with pytest.raises(ControlProtocolError, match="expected welcome"):
        client._control_handle_msg({"type": "update", "config": {"buses": []}})

    assert client._ctrl_received_welcome is False
    assert client._ctrl_received_config is False
    assert client._udp_ready.is_set() is False

    with pytest.raises(ControlProtocolError, match="version"):
        client._control_handle_msg({"type": "welcome", "server_time_ms": 1})
    assert client._ctrl_received_welcome is False
    assert client._session_token == 0

    client._control_handle_msg(
        {
            "type": "welcome",
            "version": CONTROL_PROTOCOL_VERSION,
            "session_token": format_session_token(SESSION_TOKEN),
            "server_time_ms": 1,
        }
    )
    assert client._ctrl_received_welcome is True
    assert client._session_token == SESSION_TOKEN
    assert client._ctrl_received_config is False
    assert client._udp_ready.is_set() is False

    with pytest.raises(ControlProtocolError, match="missing buses"):
        client._control_handle_msg({"type": "update", "config": {}})
    assert client._ctrl_received_config is False
    assert client._udp_ready.is_set() is False

    client._control_handle_msg({"type": "update", "config": {"buses": []}})
    assert client._ctrl_received_config is True
    assert client._udp_ready.is_set() is True
    udp_server.close()


def test_control_protocol_error_closes_session_before_backoff(client_factory, monkeypatch):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1])

    class _StopOnBackoffEvent(threading.Event):
        def wait(self, timeout=None) -> bool:
            self.set()
            return True

    class _MalformedControlSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            pass

        def connect(self, addr: tuple) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            self.sent.append(bytes(data))

        def recv(self, size: int) -> bytes:
            return b"[]\n"

        def close(self) -> None:
            self.closed = True

    control_socket = _MalformedControlSocket()
    monkeypatch.setattr(client_mod.socket, "socket", lambda *args, **kwargs: control_socket)
    stop_event = _StopOnBackoffEvent()
    udp_ready = threading.Event()
    with client._network_state_lock:
        client._network_generation = 7
        client._stop = stop_event
        client._udp_ready = udp_ready

    client._control_loop(stop_event, 7, udp_ready)

    assert control_socket.closed is True
    assert stop_event.is_set() is True
    assert udp_ready.is_set() is False
    assert client._ctrl_connected is False
    assert client._ctrl_received_welcome is False
    assert len(control_socket.sent) == 1
    udp_server.close()


def test_udp_rx_rejects_wrong_source_and_pre_session_backlog(client_factory):
    expected_sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    expected_sender.bind(("127.0.0.1", 0))
    wrong_sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wrong_sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=expected_sender.getsockname()[1])

    received: list[tuple[int, bytes, int]] = []

    class _RecordingJitterBuffer:
        def push(self, seq: int, payload: bytes, metadata) -> None:
            received.append((int(seq), bytes(payload), int(metadata.bus_mask)))

    client._jb = _RecordingJitterBuffer()
    stop_event = threading.Event()
    udp_ready = threading.Event()
    with client._network_state_lock:
        client._network_generation = 1
        client._stop = stop_event
        client._udp_ready = udp_ready
    client._session_token = SESSION_TOKEN

    thread = threading.Thread(target=client._rx_loop, args=(stop_event, 1, udp_ready), daemon=True)
    thread.start()
    destination = ("127.0.0.1", int(client._sock.getsockname()[1]))

    stale = _legacy_audio_packet(0, 0, 10, b"stale")
    expected_sender.sendto(stale, destination)
    assert _wait_until(lambda: client._rx_not_ready_dropped == 1)

    udp_ready.set()
    forged = _legacy_audio_packet(0, 0, 11, b"forged")
    wrong_sender.sendto(forged, destination)
    assert _wait_until(lambda: client._rx_source_dropped == 1)

    fresh = pack_audio_packet_v2(
        session_token=SESSION_TOKEN,
        client_id=client.client_id,
        timestamp_ms=0,
        sequence_number=12,
        stream_id=0,
        bus_mask=0,
        direction=AudioPacketDirection.SERVER_TO_CLIENT,
        payload=b"fresh",
    )
    expected_sender.sendto(fresh, destination)
    assert _wait_until(lambda: received == [(12, b"fresh", 0)])

    legacy = _legacy_audio_packet(client.client_id, 0, 13, b"legacy")
    expected_sender.sendto(legacy, destination)
    assert _wait_until(lambda: client._rx_invalid_packets == 1)

    stop_event.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    expected_sender.close()
    wrong_sender.close()


def _server_packet(client, *, token=SESSION_TOKEN, direction=AudioPacketDirection.SERVER_TO_CLIENT, bus_mask=0):
    return unpack_audio_packet_v2(
        pack_audio_packet_v2(
            session_token=token,
            client_id=client.client_id,
            timestamp_ms=10,
            sequence_number=20,
            stream_id=0,
            bus_mask=bus_mask,
            direction=direction,
            payload=b"opus",
        )
    )


def test_client_rejects_old_token_wrong_direction_and_unknown_route(client_factory):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1])
    client._session_token = SESSION_TOKEN

    assert not client._accept_server_udp_packet(_server_packet(client, token=SESSION_TOKEN + 1))
    assert client._rx_invalid_tokens == 1
    assert client._jb.buffered_frames == 0

    assert not client._accept_server_udp_packet(
        _server_packet(client, direction=AudioPacketDirection.CLIENT_TO_SERVER)
    )
    assert client._rx_invalid_directions == 1

    assert not client._accept_server_udp_packet(_server_packet(client, bus_mask=1 << 15))
    assert client._rx_invalid_route_masks == 1

    assert client._accept_server_udp_packet(_server_packet(client, bus_mask=0))
    assert client._rx_packets == 1
    assert client._last_udp_rx_monotonic > 0.0
    udp_server.close()


@pytest.mark.parametrize(
    "welcome",
    [
        {"type": "welcome", "version": 1, "session_token": format_session_token(SESSION_TOKEN)},
        {"type": "welcome", "version": CONTROL_PROTOCOL_VERSION, "session_token": "0000000000000000"},
        {"type": "welcome", "version": CONTROL_PROTOCOL_VERSION, "session_token": "ABCDEF0123456789"},
    ],
)
def test_invalid_welcome_never_opens_audio_gate(client_factory, welcome):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()

    with pytest.raises(ControlProtocolError):
        client._control_handle_msg(welcome)

    assert client._session_token == 0
    assert client._ctrl_received_welcome is False
    assert client._udp_ready.is_set() is False
    udp_server.close()


def test_client_stats_expose_audio_and_network_health(client_factory):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1])

    class _AliveThread:
        name = "health-test"

        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    class _ActiveStream:
        active = True

    client._ctrl_thread = _AliveThread()
    client._rx_thread = _AliveThread()
    client._tx_thread = _AliveThread()
    client._playout_thread = _AliveThread()
    client._in_stream = _ActiveStream()
    client._out_stream = _ActiveStream()
    client._last_udp_rx_monotonic = time.monotonic()

    stats = client.get_stats_snapshot()

    assert stats["network_workers_alive"] is True
    assert stats["in_stream_active"] is True
    assert stats["out_stream_active"] is True
    assert stats["udp_rx_age_s"] is not None
    assert stats["tx_worker_alive"] is True
    assert stats["playout_worker_alive"] is True
    assert stats["tx_ring_depth"] == 0
    assert stats["playout_ring_depth"] == 0
    client._ctrl_thread = None
    client._rx_thread = None
    client._tx_thread = None
    client._playout_thread = None
    udp_server.close()


def test_udp_drain_removes_kernel_backlog(client_factory):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    sender.sendto(b"old-session", ("127.0.0.1", int(client._sock.getsockname()[1])))
    assert _wait_until(lambda: client._drain_udp_socket() == 1)
    assert client._drain_udp_socket() == 0
    sender.close()


def test_tx_worker_exclusively_owns_encoder_probe_sequence_and_socket(
    client_factory,
    monkeypatch,
):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _ConcurrencyDetectingEncoder:
        def __init__(self) -> None:
            self._guard = threading.Lock()
            self.active = False
            self.concurrent_use = False
            self.thread_ids: list[int] = []

        def encode(self, frame: np.ndarray) -> bytes:
            with self._guard:
                if self.active:
                    self.concurrent_use = True
                self.active = True
                self.thread_ids.append(threading.get_ident())
            time.sleep(0.03)
            with self._guard:
                self.active = False
            return b"opus"

    class _RecordingSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.thread_ids: list[int] = []
            self._lock = threading.Lock()

        def sendto(self, data: bytes, addr: tuple) -> None:
            with self._lock:
                self.sent.append(bytes(data))
                self.thread_ids.append(threading.get_ident())

        def close(self) -> None:
            pass

    client._sock.close()
    recording_socket = _RecordingSocket()
    encoder = _ConcurrencyDetectingEncoder()
    monkeypatch.setattr(client_mod, "OpusEncoder", lambda: encoder)
    client._sock = recording_socket
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    tx_thread = threading.Thread(
        target=client._tx_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-audio-tx",
    )
    client._tx_thread = tx_thread
    tx_thread.start()

    start_barrier = threading.Barrier(3)

    def capture() -> None:
        start_barrier.wait()
        client._in_callback(
            np.zeros((int(FRAME_SAMPLES), 1), dtype=np.float32),
            int(FRAME_SAMPLES),
            None,
            None,
        )

    def probe() -> None:
        start_barrier.wait()
        client._send_silence_probe()

    capture_thread = threading.Thread(target=capture)
    probe_thread = threading.Thread(target=probe)
    capture_thread.start()
    probe_thread.start()
    start_barrier.wait()
    capture_thread.join(timeout=1.0)
    probe_thread.join(timeout=1.0)

    assert not capture_thread.is_alive()
    assert not probe_thread.is_alive()
    assert _wait_until(lambda: len(recording_socket.sent) == 2)
    client._stop.set()
    tx_thread.join(timeout=1.0)
    assert not tx_thread.is_alive()
    assert encoder.concurrent_use is False
    assert len(recording_socket.sent) == 2
    assert len(set(encoder.thread_ids)) == 1
    assert set(encoder.thread_ids) == set(recording_socket.thread_ids)
    assert encoder.thread_ids[0] == tx_thread.ident
    packets = [unpack_audio_packet_v2(data) for data in recording_socket.sent]
    assert {packet.sequence_number for packet in packets} == {1, 2}
    assert {packet.session_token for packet in packets} == {SESSION_TOKEN}
    assert {packet.direction for packet in packets} == {AudioPacketDirection.CLIENT_TO_SERVER}
    assert {packet.bus_mask for packet in packets} == {0, 1}
    assert client._opus_encoder_resets == 1
    assert client._opus_encoder_reset_errors == 0
    sender.close()


def test_audio_frame_keeps_ptt_mask_captured_before_encoding(
    client_factory,
    monkeypatch,
):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _PttMutatingEncoder:
        def encode(self, frame: np.ndarray) -> bytes:
            with client._state_lock:
                client._ptt_buses = {2: True}
            return b"opus"

    class _RecordingSocket:
        def __init__(self) -> None:
            self.sent = []

        def sendto(self, data: bytes, addr: tuple) -> None:
            self.sent.append(bytes(data))

        def close(self) -> None:
            pass

    client._sock.close()
    client._sock = _RecordingSocket()
    monkeypatch.setattr(client_mod, "OpusEncoder", _PttMutatingEncoder)
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    tx_thread = threading.Thread(
        target=client._tx_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-ptt-tx",
    )
    client._tx_thread = tx_thread
    tx_thread.start()

    client._in_callback(
        np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32),
        int(FRAME_SAMPLES),
        None,
        None,
    )

    assert _wait_until(lambda: len(client._sock.sent) == 1)
    client._stop.set()
    tx_thread.join(timeout=1.0)
    assert not tx_thread.is_alive()
    assert len(client._sock.sent) == 1
    packet = unpack_audio_packet_v2(client._sock.sent[0])
    assert packet.bus_mask == 1 << 0
    assert client._ptt_buses == {2: True}
    sender.close()


def test_audio_callback_is_fail_closed_until_control_config_arrives(client_factory):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._ptt_buses = {0: True}

    before = client._tx_packets
    client._in_callback(
        np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32),
        int(FRAME_SAMPLES),
        None,
        None,
    )

    assert client._tx_packets == before
    sender.close()


@pytest.mark.parametrize(
    "decoded",
    [
        np.zeros((120,), dtype=np.float32),
        np.zeros((int(FRAME_SAMPLES), 1), dtype=np.float32),
        np.full((int(FRAME_SAMPLES),), np.nan, dtype=np.float32),
    ],
)
def test_unexpected_decoded_opus_frame_fails_to_exact_silence(client_factory, decoded):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _UnexpectedDecoder:
        def decode(self, payload: bytes) -> np.ndarray:
            return decoded

    client._dec = _UnexpectedDecoder()
    before = client._opus_decode_errors
    frame = client._decode_playout_frame(b"valid-opus-packet")

    assert frame.shape == (int(FRAME_SAMPLES),)
    assert np.array_equal(frame, np.zeros((int(FRAME_SAMPLES),), dtype=np.float32))
    assert client._opus_decode_errors == before + 1
    sender.close()


def test_portaudio_callbacks_never_call_codec_socket_or_jitter(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _ForbiddenEncoder:
        def encode(self, frame: np.ndarray) -> bytes:
            raise AssertionError("input callback called the Opus encoder")

    class _ForbiddenDecoder:
        def decode(self, payload: bytes) -> np.ndarray:
            raise AssertionError("output callback called the Opus decoder")

    class _ForbiddenSocket:
        def sendto(self, data: bytes, addr: tuple) -> None:
            raise AssertionError("input callback sent a UDP packet")

        def close(self) -> None:
            pass

    client._sock.close()
    client._sock = _ForbiddenSocket()
    client._enc = _ForbiddenEncoder()
    client._dec = _ForbiddenDecoder()
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    input_frame = np.full((int(FRAME_SAMPLES), 1), 0.25, dtype=np.float32)
    output = np.empty((int(FRAME_SAMPLES), 1), dtype=np.float32)
    client._playout_ring.push(
        np.full((int(FRAME_SAMPLES),), 0.5, dtype=np.float32),
        metadata=10,
    )

    def _allocation_forbidden(*args, **kwargs):
        raise AssertionError("callback allocated a new NumPy audio buffer")

    capture_work_id = id(client._capture_work)
    output_work_id = id(client._output_work)
    monkeypatch.setattr(client_mod.np, "zeros", _allocation_forbidden)
    monkeypatch.setattr(client_mod.np, "empty", _allocation_forbidden)

    client._in_callback(input_frame, int(FRAME_SAMPLES), None, None)
    client._out_callback(output, int(FRAME_SAMPLES), None, None)

    assert client._tx_ring.depth == 1
    assert id(client._capture_work) == capture_work_id
    assert id(client._output_work) == output_work_id
    assert np.array_equal(output[:, 0], np.full(int(FRAME_SAMPLES), 0.5, dtype=np.float32))
    sender.close()


def test_capture_ring_drops_oldest_frames_when_tx_worker_is_late(client_factory):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    input_frame = np.empty((int(FRAME_SAMPLES), 1), dtype=np.float32)

    extra_frames = 3
    for value in range(client_mod._TX_RING_CAPACITY + extra_frames):
        input_frame.fill(float(value) / 100.0)
        client._in_callback(input_frame, int(FRAME_SAMPLES), None, None)

    stats = client._tx_ring.stats
    assert stats.depth == client_mod._TX_RING_CAPACITY
    assert stats.dropped == extra_frames

    output = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
    observed: list[float] = []
    for _ in range(client_mod._TX_RING_CAPACITY):
        metadata = client._tx_ring.pop_into(output)
        assert metadata is not None
        _epoch, bus_mask = client._unpack_tx_metadata(metadata)
        assert bus_mask == 1 << 0
        observed.append(float(output[0]))

    expected = [
        float(value) / 100.0
        for value in range(extra_frames, client_mod._TX_RING_CAPACITY + extra_frames)
    ]
    assert observed == pytest.approx(expected)
    sender.close()


def test_playout_worker_exclusively_decodes_before_output_callback(
    client_factory,
    monkeypatch,
):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _SingleFrameJitterBuffer:
        def __init__(self) -> None:
            self.frame = JitterFrame(
                sequence_number=77,
                payload=b"opus",
                metadata=JitterFrameMetadata(session_token=SESSION_TOKEN),
            )

        def pop_frame(self):
            frame, self.frame = self.frame, None
            return frame

        def reset(self) -> None:
            self.frame = None

    class _RecordingDecoder:
        def __init__(self) -> None:
            self.thread_ids: list[int] = []

        def decode(self, payload: bytes) -> np.ndarray:
            self.thread_ids.append(threading.get_ident())
            return np.full((int(FRAME_SAMPLES),), 0.25, dtype=np.float32)

    decoder = _RecordingDecoder()
    client._jb = _SingleFrameJitterBuffer()
    monkeypatch.setattr(client_mod, "OpusDecoder", lambda: decoder)
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    playout_thread = threading.Thread(
        target=client._playout_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-audio-playout",
    )
    client._playout_thread = playout_thread
    playout_thread.start()

    assert _wait_until(lambda: client._playout_ring.depth == 1)
    output = np.empty((int(FRAME_SAMPLES), 1), dtype=np.float32)
    client._out_callback(output, int(FRAME_SAMPLES), None, None)

    client._stop.set()
    playout_thread.join(timeout=1.0)
    assert not playout_thread.is_alive()
    assert decoder.thread_ids == [playout_thread.ident]
    assert client._opus_decoder_resets == 1
    assert client._opus_decoder_reset_errors == 0
    assert np.array_equal(
        output[:, 0],
        np.full((int(FRAME_SAMPLES),), 0.25, dtype=np.float32),
    )
    sender.close()


def test_session_reset_clears_tx_playout_and_jitter_state(client_factory):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    old_epoch = client._audio_session_epoch

    client._in_callback(
        np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32),
        int(FRAME_SAMPLES),
        None,
        None,
    )
    client._playout_ring.push(
        np.ones((int(FRAME_SAMPLES),), dtype=np.float32),
        metadata=1,
    )
    client._jb.push(1, b"opus", JitterFrameMetadata(session_token=SESSION_TOKEN))
    assert client._tx_ring.depth == 1
    assert client._playout_ring.depth == 1
    assert client._jb.buffered_frames == 1

    client._reset_session_token()

    assert client._session_token == 0
    assert client._audio_session_epoch != old_epoch
    assert client._tx_ring.depth == 0
    assert client._playout_ring.depth == 0
    assert client._jb.buffered_frames == 0
    sender.close()


def test_tx_worker_recreates_encoder_for_each_authenticated_epoch(
    client_factory,
    monkeypatch,
):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _RecordingSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def sendto(self, data: bytes, addr: tuple) -> None:
            self.sent.append(bytes(data))

        def close(self) -> None:
            pass

    created: list[object] = []

    class _EpochEncoder:
        def __init__(self) -> None:
            self.constructed_by = threading.get_ident()
            self.encoded_by: list[int] = []
            created.append(self)

        def encode(self, frame: np.ndarray) -> bytes:
            self.encoded_by.append(threading.get_ident())
            return b"opus"

    client._sock.close()
    client._sock = _RecordingSocket()
    monkeypatch.setattr(client_mod, "OpusEncoder", _EpochEncoder)
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    tx_thread = threading.Thread(
        target=client._tx_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-epoch-tx",
    )
    client._tx_thread = tx_thread
    tx_thread.start()

    capture = np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32)
    client._in_callback(capture, int(FRAME_SAMPLES), None, None)
    assert _wait_until(lambda: len(client._sock.sent) == 1)

    next_token = SESSION_TOKEN + 1
    client._activate_session_token(next_token)
    client._in_callback(capture, int(FRAME_SAMPLES), None, None)
    assert _wait_until(lambda: len(client._sock.sent) == 2)

    client._stop.set()
    tx_thread.join(timeout=1.0)
    assert not tx_thread.is_alive()
    assert len(created) == 2
    assert created[0] is not created[1]
    assert {encoder.constructed_by for encoder in created} == {tx_thread.ident}
    assert {
        thread_id
        for encoder in created
        for thread_id in encoder.encoded_by
    } == {tx_thread.ident}
    packets = [unpack_audio_packet_v2(data) for data in client._sock.sent]
    assert [packet.session_token for packet in packets] == [
        SESSION_TOKEN,
        next_token,
    ]
    assert client._opus_encoder_resets == 2
    assert client._opus_encoder_reset_errors == 0
    sender.close()


def test_playout_worker_recreates_decoder_for_each_authenticated_epoch(
    client_factory,
    monkeypatch,
):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _FrameQueueJitterBuffer:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._frames: list[JitterFrame] = []

        def add(self, token: int, sequence: int) -> None:
            with self._lock:
                self._frames.append(
                    JitterFrame(
                        sequence_number=sequence,
                        payload=b"opus",
                        metadata=JitterFrameMetadata(session_token=token),
                    )
                )

        def pop_frame(self):
            with self._lock:
                if not self._frames:
                    return None
                return self._frames.pop(0)

        def reset(self) -> None:
            with self._lock:
                self._frames.clear()

    created: list[object] = []

    class _EpochDecoder:
        def __init__(self) -> None:
            self.constructed_by = threading.get_ident()
            self.decoded_by: list[int] = []
            created.append(self)

        def decode(self, payload: bytes) -> np.ndarray:
            self.decoded_by.append(threading.get_ident())
            return np.full((int(FRAME_SAMPLES),), 0.25, dtype=np.float32)

    jitter = _FrameQueueJitterBuffer()
    client._jb = jitter
    monkeypatch.setattr(client_mod, "OpusDecoder", _EpochDecoder)
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    jitter.add(SESSION_TOKEN, 1)
    playout_thread = threading.Thread(
        target=client._playout_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-epoch-playout",
    )
    client._playout_thread = playout_thread
    playout_thread.start()

    assert _wait_until(lambda: client._playout_ring.depth == 1)
    first_pcm = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
    assert client._playout_ring.pop_into(first_pcm) == 1

    next_token = SESSION_TOKEN + 1
    client._activate_session_token(next_token)
    jitter.add(next_token, 2)
    assert _wait_until(lambda: client._playout_ring.depth == 1)

    client._stop.set()
    playout_thread.join(timeout=1.0)
    assert not playout_thread.is_alive()
    assert len(created) == 2
    assert created[0] is not created[1]
    assert {decoder.constructed_by for decoder in created} == {
        playout_thread.ident
    }
    assert {
        thread_id
        for decoder in created
        for thread_id in decoder.decoded_by
    } == {playout_thread.ident}
    assert client._opus_decoder_resets == 2
    assert client._opus_decoder_reset_errors == 0
    sender.close()


def test_codec_reset_failure_is_fail_closed(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _RecordingSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def sendto(self, data: bytes, addr: tuple) -> None:
            self.sent.append(bytes(data))

        def close(self) -> None:
            pass

    def _failed_encoder():
        raise RuntimeError("codec reset unavailable")

    client._sock.close()
    client._sock = _RecordingSocket()
    monkeypatch.setattr(client_mod, "OpusEncoder", _failed_encoder)
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    tx_thread = threading.Thread(
        target=client._tx_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-failed-codec-reset",
    )
    client._tx_thread = tx_thread
    tx_thread.start()

    client._in_callback(
        np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32),
        int(FRAME_SAMPLES),
        None,
        None,
    )
    assert _wait_until(lambda: client._opus_encoder_reset_errors == 1)
    assert client._sock.sent == []
    assert client._tx_udp_sent == 0

    client._stop.set()
    tx_thread.join(timeout=1.0)
    assert not tx_thread.is_alive()
    sender.close()


def test_decoder_reset_failure_publishes_no_pcm(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    class _SingleFrameJitterBuffer:
        def __init__(self) -> None:
            self.frame = JitterFrame(
                sequence_number=1,
                payload=b"opus",
                metadata=JitterFrameMetadata(session_token=SESSION_TOKEN),
            )

        def pop_frame(self):
            frame, self.frame = self.frame, None
            return frame

        def reset(self) -> None:
            self.frame = None

    def _failed_decoder():
        raise RuntimeError("decoder reset unavailable")

    client._jb = _SingleFrameJitterBuffer()
    monkeypatch.setattr(client_mod, "OpusDecoder", _failed_decoder)
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    playout_thread = threading.Thread(
        target=client._playout_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-failed-decoder-reset",
    )
    client._playout_thread = playout_thread
    playout_thread.start()

    assert _wait_until(lambda: client._opus_decoder_reset_errors == 1)
    assert client._playout_ring.depth == 0
    assert client._opus_decoder_resets == 0

    client._stop.set()
    playout_thread.join(timeout=1.0)
    assert not playout_thread.is_alive()
    sender.close()


def test_full_stop_is_idempotent_and_restart_fails_clearly(client_factory):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    client.stop()
    client.stop()

    assert client._fully_stopped is True
    assert client.get_stats_snapshot()["fully_stopped"] is True
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        client.start()
    with pytest.raises(RuntimeError, match="cannot be reconnected"):
        client.reconnect_network()
    sender.close()


def test_audio_start_establishes_input_before_output(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    events: list[str] = []

    class _Stream:
        def __init__(self, role: str) -> None:
            self.role = role

        def start(self) -> None:
            events.append(f"start_{self.role}")

        def abort(self) -> None:
            events.append(f"abort_{self.role}")

        def stop(self) -> None:
            events.append(f"stop_{self.role}")

        def close(self) -> None:
            events.append(f"close_{self.role}")

    input_stream = _Stream("input")
    output_stream = _Stream("output")

    monkeypatch.setattr(client_mod.sd, "stop", lambda: None)
    monkeypatch.setattr(
        client,
        "_start_network_threads",
        lambda: events.append("start_network"),
    )

    def open_input():
        events.append("open_input")
        return input_stream

    def open_output():
        events.append("open_output")
        return output_stream

    monkeypatch.setattr(client, "_open_input_stream", open_input)
    monkeypatch.setattr(client, "_open_output_stream", open_output)

    client.start()

    assert events == [
        "start_network",
        "open_input",
        "start_input",
        "open_output",
        "start_output",
    ]
    assert client._in_stream is input_stream
    assert client._out_stream is output_stream

    client.stop()
    sender.close()


def test_audio_start_failure_is_translated_and_rolls_back_both_streams(
    client_factory,
    monkeypatch,
):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    client.config.input_device = 91
    client.config.output_device = 92
    events: list[str] = []

    class _Stream:
        def __init__(self, role: str, *, fail_start: bool = False) -> None:
            self.role = role
            self.fail_start = fail_start

        def start(self) -> None:
            events.append(f"start_{self.role}")
            if self.fail_start:
                raise RuntimeError("Device unavailable")

        def abort(self) -> None:
            events.append(f"abort_{self.role}")

        def stop(self) -> None:
            events.append(f"stop_{self.role}")
            if self.role == "input":
                raise RuntimeError("already stopped")

        def close(self) -> None:
            events.append(f"close_{self.role}")

    input_stream = _Stream("input")
    output_stream = _Stream("output", fail_start=True)

    def fail_network_cleanup() -> bool:
        raise RuntimeError("network cleanup failed")

    monkeypatch.setattr(client_mod.sd, "stop", lambda: None)
    monkeypatch.setattr(client, "_start_network_threads", lambda: None)
    monkeypatch.setattr(client, "_stop_network", fail_network_cleanup)
    monkeypatch.setattr(client, "_open_input_stream", lambda: input_stream)
    monkeypatch.setattr(client, "_open_output_stream", lambda: output_stream)
    monkeypatch.setattr(
        client_mod.sd,
        "query_devices",
        lambda _device: {
            "name": "G435 Bluetooth Gaming Headset Hands-Free",
            "hostapi": 3,
        },
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.start()

    message = str(exc_info.value)
    assert "indisponible" in message
    assert "Bluetooth" in message
    assert "mains-libres/HFP" in message
    assert events[:4] == [
        "start_input",
        "start_output",
        "abort_output",
        "stop_output",
    ]
    assert "close_output" in events
    assert "close_input" in events
    assert client._in_stream is None
    assert client._out_stream is None
    assert client._fully_stopped is True
    sender.close()


def test_wasapi_auto_conversion_is_used_for_client_input_and_output(
    client_factory,
    monkeypatch,
):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    client.config.input_device = 50
    client.config.output_device = 48

    settings_created: list[object] = []
    checked: list[tuple[str, dict]] = []
    opened: list[tuple[str, dict]] = []

    def make_wasapi_settings(**kwargs):
        marker = object()
        settings_created.append(marker)
        assert kwargs == {"exclusive": False, "auto_convert": True}
        return marker

    monkeypatch.setattr(client_mod.sd, "WasapiSettings", make_wasapi_settings)
    monkeypatch.setattr(
        client_mod.sd,
        "query_hostapis",
        lambda _index: {"name": "Windows WASAPI"},
    )
    monkeypatch.setattr(
        client_mod.sd,
        "query_devices",
        lambda device: {
            "name": f"Device {device}",
            "hostapi": 2,
            "max_input_channels": 1,
            "max_output_channels": 1,
            "default_samplerate": 44_100.0,
        },
    )
    monkeypatch.setattr(
        client_mod.sd,
        "check_input_settings",
        lambda **kwargs: checked.append(("input", dict(kwargs))),
    )
    monkeypatch.setattr(
        client_mod.sd,
        "check_output_settings",
        lambda **kwargs: checked.append(("output", dict(kwargs))),
    )
    monkeypatch.setattr(
        client_mod.sd,
        "InputStream",
        lambda **kwargs: opened.append(("input", dict(kwargs))) or object(),
    )
    monkeypatch.setattr(
        client_mod.sd,
        "OutputStream",
        lambda **kwargs: opened.append(("output", dict(kwargs))) or object(),
    )

    client._open_input_stream()
    client._open_output_stream()

    assert len(settings_created) == 2
    assert [kind for kind, _kwargs in checked] == ["input", "output"]
    assert [kind for kind, _kwargs in opened] == ["input", "output"]
    for position, (_kind, kwargs) in enumerate(checked):
        assert kwargs["extra_settings"] is settings_created[position]
    for position, (_kind, kwargs) in enumerate(opened):
        assert kwargs["extra_settings"] is settings_created[position]
    sender.close()
