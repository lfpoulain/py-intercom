from __future__ import annotations

import socket
import struct
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd

import py_intercom.common.opus_codec as opus_codec_mod
from py_intercom.client import audio_io as audio_io_mod
from py_intercom.client import client as client_mod
from py_intercom.client import control as control_mod
from py_intercom.client import transport as transport_mod
from py_intercom.client.client import ClientConfig, IntercomClient
from py_intercom.client.control import ControlRefused
from py_intercom.common.constants import (
    CTRL_RECONNECT_MAX_S,
    CTRL_RECONNECT_MIN_S,
    FRAME_SAMPLES,
    MAX_GAIN_DB,
    PCM_RING_TRIM_ABOVE_FRAMES,
    SAMPLE_RATE,
)
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
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", _FakeDecoder)
    clients: list[IntercomClient] = []

    def make(*, server_port: int, control_port: int = 1, server_ip: str = "127.0.0.1") -> IntercomClient:
        client = IntercomClient(
            client_id=123,
            config=ClientConfig(
                server_ip=server_ip,
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

    assert not client._accept_server_udp_packet(_server_packet(client, direction=AudioPacketDirection.CLIENT_TO_SERVER))
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


def test_welcome_racing_a_disconnect_never_rearms_the_session(client_factory, monkeypatch):
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = client_factory(server_port=udp_server.getsockname()[1])
    stop_event = threading.Event()
    udp_ready = threading.Event()
    with client._network_state_lock:
        client._network_generation += 1
        client._stop = stop_event
        client._udp_ready = udp_ready
    real_parse_session_token = control_mod.parse_session_token

    def parse_then_disconnect(value):
        # The operator disconnects after the welcome passed the generation
        # check, before its token is activated.
        token = real_parse_session_token(value)
        assert client._stop_network() is True
        return token

    monkeypatch.setattr(control_mod, "parse_session_token", parse_then_disconnect)
    client._control_handle_msg(
        {
            "type": "welcome",
            "version": CONTROL_PROTOCOL_VERSION,
            "session_token": format_session_token(SESSION_TOKEN),
        },
        generation=client._network_generation,
        stop_event=stop_event,
        udp_ready=udp_ready,
    )

    # The next generation starts from an empty session, as it assumes.
    assert client._session_token == 0
    assert client._ctrl_received_welcome is False
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
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", lambda: encoder)
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
    assert {packet.flags for packet in packets} == {AudioPacketDirection.CLIENT_TO_SERVER}
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
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", _PttMutatingEncoder)
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

    before = client._opus_decode_errors
    frame = client._decode_playout_frame(b"valid-opus-packet", _UnexpectedDecoder())

    assert frame.shape == (int(FRAME_SAMPLES),)
    assert np.array_equal(frame, np.zeros((int(FRAME_SAMPLES),), dtype=np.float32))
    assert client._opus_decode_errors == before + 1
    sender.close()


def test_portaudio_callbacks_never_call_codec_socket_or_jitter(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])

    def _forbidden_codec():
        raise AssertionError("a callback created an Opus codec")

    class _ForbiddenSocket:
        def sendto(self, data: bytes, addr: tuple) -> None:
            raise AssertionError("input callback sent a UDP packet")

        def close(self) -> None:
            pass

    class _ForbiddenJitterBuffer:
        def push(self, *args) -> None:
            raise AssertionError("a callback touched the jitter buffer")

        pop_frame = push

        def reset(self) -> None:
            pass

    client._sock.close()
    client._sock = _ForbiddenSocket()
    client._jb = _ForbiddenJitterBuffer()
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", _forbidden_codec)
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", _forbidden_codec)
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

    # Callbacks swallow their exceptions to keep PortAudio running: a
    # forbidden call shows up as a counted callback error.
    assert (client._input_callback_errors, client._output_callback_errors) == (0, 0)
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

    expected = [float(value) / 100.0 for value in range(extra_frames, client_mod._TX_RING_CAPACITY + extra_frames)]
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
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", lambda: decoder)
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
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", _EpochEncoder)
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
    client._activate_session_token(next_token, client._stop)
    client._in_callback(capture, int(FRAME_SAMPLES), None, None)
    assert _wait_until(lambda: len(client._sock.sent) == 2)

    client._stop.set()
    tx_thread.join(timeout=1.0)
    assert not tx_thread.is_alive()
    assert len(created) == 2
    assert created[0] is not created[1]
    assert {encoder.constructed_by for encoder in created} == {tx_thread.ident}
    assert {thread_id for encoder in created for thread_id in encoder.encoded_by} == {tx_thread.ident}
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
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", _EpochDecoder)
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
    client._activate_session_token(next_token, client._stop)
    jitter.add(next_token, 2)
    assert _wait_until(lambda: client._playout_ring.depth == 1)

    client._stop.set()
    playout_thread.join(timeout=1.0)
    assert not playout_thread.is_alive()
    assert len(created) == 2
    assert created[0] is not created[1]
    assert {decoder.constructed_by for decoder in created} == {playout_thread.ident}
    assert {thread_id for decoder in created for thread_id in decoder.decoded_by} == {playout_thread.ident}
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
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", _failed_encoder)
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
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", _failed_decoder)
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


def test_audio_callbacks_ride_out_passing_errors_and_give_up_persistent_ones(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    broken = [True]
    real_push = client._tx_ring.push
    real_pop_into = client._playout_ring.pop_into

    def _push(*args, **kwargs):
        if broken[0]:
            raise RuntimeError("ring corrupted")
        return real_push(*args, **kwargs)

    def _pop_into(*args, **kwargs):
        if broken[0]:
            raise RuntimeError("ring corrupted")
        return real_pop_into(*args, **kwargs)

    monkeypatch.setattr(client._tx_ring, "push", _push)
    monkeypatch.setattr(client._playout_ring, "pop_into", _pop_into)
    capture = np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32)
    output = np.empty((int(FRAME_SAMPLES), 1), dtype=np.float32)
    threshold = audio_io_mod._CALLBACK_ABORT_AFTER_ERRORS

    def run_frame() -> None:
        output.fill(0.5)
        client._in_callback(capture, int(FRAME_SAMPLES), None, None)
        client._out_callback(output, int(FRAME_SAMPLES), None, None)
        assert not output.any()

    # An exception escaping a callback would abort its stream at once.
    for _ in range(threshold - 1):
        run_frame()
    # One good frame clears the streak: passing errors never add up to an abort.
    broken[0] = False
    run_frame()
    broken[0] = True
    for _ in range(threshold - 1):
        run_frame()

    # A lasting failure gives the stream up, so that it reads inactive: the
    # operator status turns degraded and reconnect_network reopens it.
    output.fill(0.5)
    with pytest.raises(sd.CallbackAbort):
        client._in_callback(capture, int(FRAME_SAMPLES), None, None)
    with pytest.raises(sd.CallbackAbort):
        client._out_callback(output, int(FRAME_SAMPLES), None, None)
    assert not output.any()
    stats = client.get_stats_snapshot()
    assert stats["input_callback_errors"] == stats["output_callback_errors"] == 2 * threshold - 1
    sender.close()


class _ToggleStream:
    def __init__(self, role: str, events: list[str], *, active: bool) -> None:
        self.role = role
        self.events = events
        self.active = active

    def start(self) -> None:
        self.events.append(f"start_{self.role}")
        self.active = True

    def abort(self) -> None:
        self.events.append(f"abort_{self.role}")
        self.active = False

    def stop(self) -> None:
        self.events.append(f"stop_{self.role}")

    def close(self) -> None:
        self.events.append(f"close_{self.role}")


def test_reconnect_reopens_audio_streams_that_died(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    events: list[str] = []
    # The headset was unplugged: WASAPI invalidated the capture stream.
    client._in_stream = _ToggleStream("old_input", events, active=False)
    client._out_stream = _ToggleStream("old_output", events, active=True)
    monkeypatch.setattr(client, "_open_input_stream", lambda: _ToggleStream("input", events, active=False))
    monkeypatch.setattr(client, "_open_output_stream", lambda: _ToggleStream("output", events, active=False))
    monkeypatch.setattr(client, "_start_network_threads", lambda: events.append("start_network"))
    # The capture callback gave its stream up after a lasting failure.
    client._input_callback_error_streak = audio_io_mod._CALLBACK_ABORT_AFTER_ERRORS

    client.reconnect_network()

    assert client._input_callback_error_streak == 0
    assert events == [
        "abort_old_output",
        "stop_old_output",
        "close_old_output",
        "abort_old_input",
        "stop_old_input",
        "close_old_input",
        "start_input",
        "start_output",
        "start_network",
    ]
    assert client.audio_streams_active() is True
    sender.close()


def test_reconnect_keeps_running_audio_streams(client_factory, monkeypatch):
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    client = client_factory(server_port=sender.getsockname()[1])
    events: list[str] = []
    input_stream = _ToggleStream("input", events, active=True)
    output_stream = _ToggleStream("output", events, active=True)
    client._in_stream = input_stream
    client._out_stream = output_stream
    monkeypatch.setattr(client, "_start_network_threads", lambda: events.append("start_network"))

    client.reconnect_network()

    assert events == ["start_network"]
    assert client._in_stream is input_stream
    assert client._out_stream is output_stream
    sender.close()


def test_server_host_name_is_resolved_once_per_session(client_factory, monkeypatch):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    port = int(receiver.getsockname()[1])
    client = client_factory(server_port=port, server_ip="localhost")

    assert client._server_addr == ("127.0.0.1", port)
    assert "127.0.0.1" in client._expected_server_udp_ips

    class _RecordingSocket:
        def __init__(self) -> None:
            self.addresses: list[tuple] = []

        def sendto(self, data: bytes, addr: tuple) -> None:
            self.addresses.append(addr)

        def close(self) -> None:
            pass

    def _no_resolution(*args, **kwargs):
        raise AssertionError("the audio path resolved the server name")

    client._sock.close()
    client._sock = _RecordingSocket()
    monkeypatch.setattr(transport_mod.socket, "getaddrinfo", _no_resolution)
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = SESSION_TOKEN
    client._ptt_buses = {0: True}
    tx_thread = threading.Thread(
        target=client._tx_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="test-resolved-tx",
    )
    tx_thread.start()
    client._in_callback(np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32), int(FRAME_SAMPLES), None, None)

    assert _wait_until(lambda: len(client._sock.addresses) == 1)
    client._stop.set()
    tx_thread.join(timeout=1.0)
    assert client._sock.addresses == [("127.0.0.1", port)]
    receiver.close()


def test_stop_network_wakes_blocked_udp_and_control_workers(client_factory):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    # Nothing listens on the control port: on Windows, connect() then waits
    # for its whole 1 s timeout, and recvfrom() for its 0.5 s timeout.
    client = client_factory(server_port=receiver.getsockname()[1], control_port=1)
    client._start_network_threads()
    time.sleep(0.05)

    started = time.monotonic()
    assert client._stop_network() is True

    assert time.monotonic() - started < 0.3
    receiver.close()


def test_disconnect_interrupts_a_pending_control_connect(client_factory, monkeypatch):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])
    connect_entered = threading.Event()

    class _PendingConnectSocket:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def settimeout(self, timeout: float) -> None:
            pass

        def connect(self, addr: tuple) -> None:
            connect_entered.set()
            if not self.closed.wait(timeout=5.0):
                raise TimeoutError("timed out")
            raise OSError("socket closed during connect")

        def shutdown(self, how: int) -> None:
            raise OSError("socket is not connected")

        def close(self) -> None:
            self.closed.set()

    pending = _PendingConnectSocket()
    real_socket = socket.socket

    def make_socket(family=socket.AF_INET, kind=socket.SOCK_STREAM, *args):
        return pending if kind == socket.SOCK_STREAM else real_socket(family, kind, *args)

    monkeypatch.setattr(client_mod.socket, "socket", make_socket)
    stop_event = threading.Event()
    udp_ready = threading.Event()
    with client._network_state_lock:
        client._network_generation = 3
        client._stop = stop_event
        client._udp_ready = udp_ready
    ctrl_thread = threading.Thread(target=client._control_loop, args=(stop_event, 3, udp_ready))
    client._ctrl_thread = ctrl_thread
    ctrl_thread.start()
    assert connect_entered.wait(timeout=1.0)

    started = time.monotonic()
    assert client._stop_network() is True

    assert time.monotonic() - started < 0.5
    assert pending.closed.is_set()
    assert not ctrl_thread.is_alive()
    receiver.close()


def test_control_reconnect_backoff_is_capped_and_jittered(client_factory, monkeypatch):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])
    waits: list[float] = []

    class _RecordingStopEvent(threading.Event):
        def wait(self, timeout=None) -> bool:
            waits.append(float(timeout))
            if len(waits) >= 8:
                self.set()
            return self.is_set()

    class _RefusedSocket:
        def settimeout(self, timeout: float) -> None:
            pass

        def connect(self, addr: tuple) -> None:
            raise ConnectionRefusedError("server restarting")

        def close(self) -> None:
            pass

    monkeypatch.setattr(client_mod.socket, "socket", lambda *args, **kwargs: _RefusedSocket())
    stop_event = _RecordingStopEvent()
    udp_ready = threading.Event()
    with client._network_state_lock:
        client._network_generation = 5
        client._stop = stop_event
        client._udp_ready = udp_ready

    client._control_loop(stop_event, 5, udp_ready)

    assert len(waits) == 8
    assert CTRL_RECONNECT_MIN_S * 0.8 <= waits[0] <= CTRL_RECONNECT_MIN_S * 1.2
    assert all(CTRL_RECONNECT_MAX_S * 0.8 <= wait <= CTRL_RECONNECT_MAX_S * 1.2 for wait in waits[1:])
    assert len(set(waits[1:])) > 1
    receiver.close()


def test_server_bus_config_must_be_a_list(client_factory):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._control_handle_msg(
        {
            "type": "welcome",
            "version": CONTROL_PROTOCOL_VERSION,
            "session_token": format_session_token(SESSION_TOKEN),
        }
    )

    with pytest.raises(ControlProtocolError, match="must be a list"):
        client._control_handle_msg({"type": "update", "config": {"buses": {"0": {"name": "Régie"}}}})
    assert client._udp_ready.is_set() is False

    # The buses are the fixed catalog: the entries themselves are not used.
    client._control_handle_msg({"type": "update", "config": {"buses": [{"bus_id": 0}, "malformed"]}})
    assert client._udp_ready.is_set() is True
    receiver.close()


_PLAYOUT_PERIOD_S = float(FRAME_SAMPLES) / float(SAMPLE_RATE)


def _run_playout_against_simulated_card(
    client: IntercomClient,
    monkeypatch,
    *,
    frames: int = 30,
    late_wakes: dict[int, float] | None = None,
    stalls_after_push: dict[int, float] | None = None,
    card_runs_during_stalls: bool = True,
) -> SimpleNamespace:
    """Run the playout worker on a simulated clock against a simulated sound card.

    The card reads the PCM ring every period, three quarters of a period after
    each worker tick. One frame is queued beforehand, so the ring starts with
    its steady-state margin of one frame. ``late_wakes[n]`` makes the worker
    wake up that late before its n-th jitter pop; ``stalls_after_push[n]``
    deschedules it right after it published its n-th frame.

    Returns the jitter pop times and, for each card read, the ring depth left
    behind (-1 when the card found the ring empty and played silence).
    """
    late_wakes = dict(late_wakes or {})
    stalls_after_push = dict(stalls_after_push or {})
    ring = client._playout_ring
    real_push = ring.push
    clock = [1000.0]
    next_card_read = [clock[0] + 0.75 * _PLAYOUT_PERIOD_S]
    card_output = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
    card_reads: list[int] = []
    pops: list[float] = []

    def advance(seconds: float, *, card_runs: bool = True) -> None:
        target = clock[0] + float(seconds)
        while next_card_read[0] <= target:
            clock[0] = next_card_read[0]
            if card_runs:
                played = ring.pop_into(card_output) is not None
                card_reads.append(ring.depth if played else -1)
            next_card_read[0] += _PLAYOUT_PERIOD_S
        clock[0] = target

    class _SimulatedStopEvent(threading.Event):
        def wait(self, timeout=None) -> bool:
            advance(float(timeout))
            advance(late_wakes.pop(len(pops) + 1, 0.0), card_runs=card_runs_during_stalls)
            return self.is_set()

    stop_event = _SimulatedStopEvent()

    class _EndlessJitterBuffer:
        def pop_frame(self):
            pops.append(clock[0])
            if len(pops) == frames:
                stop_event.set()
            return JitterFrame(
                sequence_number=len(pops),
                payload=b"opus",
                metadata=JitterFrameMetadata(session_token=SESSION_TOKEN),
            )

        def reset(self) -> None:
            pass

    def push_then_stall(frame, metadata=0) -> None:
        real_push(frame, metadata=metadata)
        advance(stalls_after_push.pop(len(pops), 0.0), card_runs=card_runs_during_stalls)

    monkeypatch.setattr(ring, "push", push_then_stall)
    monkeypatch.setattr(transport_mod, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    client._jb = _EndlessJitterBuffer()
    udp_ready = threading.Event()
    udp_ready.set()
    with client._network_state_lock:
        client._stop = stop_event
        client._udp_ready = udp_ready
    client._session_token = SESSION_TOKEN
    real_push(np.zeros((int(FRAME_SAMPLES),), dtype=np.float32))

    client._playout_loop(stop_event, client._network_generation, udp_ready)
    return SimpleNamespace(pops=pops, card_reads=card_reads)


def _elapsed_ticks(pops: list[float]) -> int:
    return round((pops[-1] - pops[0]) / _PLAYOUT_PERIOD_S)


@pytest.mark.parametrize(
    "delay",
    [
        {"late_wakes": {10: 0.012}},
        {"late_wakes": {10: 0.015}},
        {"stalls_after_push": {10: 0.022}},
    ],
    ids=["woken-12ms-late", "woken-15ms-late", "descheduled-22ms"],
)
def test_playout_makes_up_the_ticks_a_late_worker_missed(client_factory, monkeypatch, delay):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])

    run = _run_playout_against_simulated_card(client, monkeypatch, **delay)

    # The ring margin covered the delay: the card never played silence, every
    # tick still produced its frame and the margin is whole again afterwards.
    assert -1 not in run.card_reads
    assert len(run.pops) - 1 == _elapsed_ticks(run.pops)
    assert run.card_reads[-5:] == [PCM_RING_TRIM_ABOVE_FRAMES] * 5
    receiver.close()


@pytest.mark.parametrize(
    "delay",
    [{"late_wakes": {10: 0.045}}, {"stalls_after_push": {10: 0.045}}],
    ids=["woken-45ms-late", "descheduled-45ms"],
)
def test_playout_drops_the_ticks_the_card_already_played_silence_for(client_factory, monkeypatch, delay):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])

    run = _run_playout_against_simulated_card(client, monkeypatch, **delay)

    silenced = run.card_reads.count(-1)
    assert silenced > 0
    # Exactly the ticks covered by silence are dropped (their frames are left
    # to the jitter buffer's latency cap): bursting them into the ring would
    # delay everything played afterwards.
    assert len(run.pops) - 1 + silenced == _elapsed_ticks(run.pops)
    after_silence = run.card_reads[run.card_reads.index(-1) :]
    assert max(after_silence) <= PCM_RING_TRIM_ABOVE_FRAMES
    assert run.card_reads[-5:] == [PCM_RING_TRIM_ABOVE_FRAMES] * 5
    receiver.close()


def test_playout_drops_a_stall_too_long_to_make_up(client_factory, monkeypatch):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])

    # The whole process was descheduled for 100 ms, sound card callback
    # included: no silence was counted, but ten ticks are overdue.
    run = _run_playout_against_simulated_card(
        client,
        monkeypatch,
        stalls_after_push={10: 0.100},
        card_runs_during_stalls=False,
    )

    resumed_at = run.pops[10]
    assert run.pops.count(resumed_at) == 1
    receiver.close()


# --- Server refusal and reconnection backoff ------------------------------------------


def _welcome_line() -> bytes:
    return encode_control_message(
        {"type": "welcome", "version": CONTROL_PROTOCOL_VERSION, "session_token": format_session_token(SESSION_TOKEN)}
    ) + encode_control_message({"type": "update", "config": {"buses": []}})


class _ScriptedControlSocket:
    """Accepts the connection, then returns ``chunks`` from recv (b"" = closed by the server)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []

    def settimeout(self, timeout: float) -> None:
        pass

    def connect(self, addr: tuple) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        pass


def _run_control_loop(client: IntercomClient, monkeypatch, scripts: list[list[bytes]], *, max_waits: int) -> list:
    waits: list[float] = []

    class _RecordingStopEvent(threading.Event):
        def wait(self, timeout=None) -> bool:
            waits.append(float(timeout))
            if len(waits) >= max_waits:
                self.set()
            return self.is_set()

    sockets = iter(_ScriptedControlSocket(chunks) for chunks in scripts)
    monkeypatch.setattr(client_mod.socket, "socket", lambda *args, **kwargs: next(sockets))
    stop_event = _RecordingStopEvent()
    udp_ready = threading.Event()
    with client._network_state_lock:
        client._network_generation = 9
        client._stop = stop_event
        client._udp_ready = udp_ready
    client._control_loop(stop_event, 9, udp_ready)
    return waits


def test_only_a_definitive_error_before_welcome_is_a_refusal(client_factory):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()

    with pytest.raises(ControlRefused, match="name exceeds"):
        client._control_handle_msg({"type": "error", "message": "hello name exceeds 128 characters", "retry": False})
    # Transient (server stopping, slow hello): the loop keeps reconnecting.
    client._control_handle_msg({"type": "error", "message": "server is stopping", "retry": True})

    client._control_handle_msg(
        {"type": "welcome", "version": CONTROL_PROTOCOL_VERSION, "session_token": format_session_token(SESSION_TOKEN)}
    )
    client._control_handle_msg({"type": "error", "message": "state contains an unknown bus id"})
    assert client._ctrl_received_welcome is True
    receiver.close()


def test_refused_hello_stops_retrying_and_reports_the_reason(client_factory, monkeypatch):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])
    refusal = encode_control_message(
        {"type": "error", "message": "client identity collides with an existing client", "retry": False}
    )

    waits = _run_control_loop(client, monkeypatch, [[refusal], [refusal]], max_waits=5)

    assert waits == []  # no retry
    snapshot = client.get_stats_snapshot()
    assert snapshot["control_refusal"] == "client identity collides with an existing client"
    assert snapshot["control_connected"] is False
    assert client._session_token == 0
    receiver.close()


def test_a_new_network_session_clears_the_refusal(client_factory):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1], control_port=1)
    client._ctrl_refusal = "hello name exceeds 128 characters"

    client._start_network_threads()
    try:
        assert client.get_stats_snapshot()["control_refusal"] == ""
    finally:
        assert client._stop_network() is True
        receiver.close()


def test_backoff_is_reset_only_by_a_welcome(client_factory, monkeypatch):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])

    # Twice the server accepts the TCP connection then closes it without a
    # welcome, then it welcomes the client and the connection drops.
    waits = _run_control_loop(client, monkeypatch, [[b""], [b""], [_welcome_line(), b""]], max_waits=3)

    assert CTRL_RECONNECT_MIN_S * 0.8 <= waits[0] <= CTRL_RECONNECT_MIN_S * 1.2
    assert CTRL_RECONNECT_MAX_S * 0.8 <= waits[1] <= CTRL_RECONNECT_MAX_S * 1.2
    assert CTRL_RECONNECT_MIN_S * 0.8 <= waits[2] <= CTRL_RECONNECT_MIN_S * 1.2
    receiver.close()


# --- Gains and meters -----------------------------------------------------------------


def test_gains_are_clamped_and_never_become_nan(client_factory):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])
    client._stop = threading.Event()
    client._udp_ready = threading.Event()

    client.set_input_gain_db(-6.0)
    client.set_input_gain_db(float("nan"))
    client.set_return_gain_db(99.0)
    client.set_output_gain_db(-500.0)
    client._control_handle_msg(
        {"type": "welcome", "version": CONTROL_PROTOCOL_VERSION, "session_token": format_session_token(SESSION_TOKEN)}
    )
    client._control_handle_msg({"type": "update", "config": {"buses": [], "input_gain_db": "loud"}})

    snapshot = client.get_stats_snapshot()
    assert snapshot["input_gain_db"] == -6.0
    assert snapshot["return_gain_db"] == MAX_GAIN_DB
    assert snapshot["output_gain_db"] == -60.0
    receiver.close()


def test_meter_levels_never_wait_for_the_state_lock(client_factory):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    client = client_factory(server_port=receiver.getsockname()[1])
    client._in_vu_dbfs = -12.0
    client._out_vu_dbfs = -18.0
    client._return_vu_dbfs = -24.0
    client._bus_vu_dbfs[1] = -6.0
    levels = []

    with client._state_lock:  # held by another thread, e.g. a snapshot
        reader = threading.Thread(target=lambda: levels.append(client.meter_levels()))
        reader.start()
        reader.join(timeout=1.0)

    assert not reader.is_alive()
    assert (levels[0].input_dbfs, levels[0].output_dbfs, levels[0].return_dbfs) == (-12.0, -18.0, -24.0)
    assert levels[0].bus_dbfs[1] == -6.0
    levels[0].bus_dbfs[1] = 0.0  # a copy: the GUI cannot alter the engine
    assert client._bus_vu_dbfs[1] == -6.0
    receiver.close()


def test_oversized_datagram_never_pauses_client_reception(client_factory):
    client = client_factory(server_port=9)
    stop_event = threading.Event()

    class _OversizedDatagram(OSError):
        winerror = 10040  # WSAEMSGSIZE

    class _Socket:
        calls = 0

        def recvfrom(self, size):
            self.calls += 1
            if self.calls == 1:
                raise _OversizedDatagram("message too long")
            stop_event.set()
            raise TimeoutError

    real_sock = client._sock
    client._sock = _Socket()
    with client._network_state_lock:
        client._network_generation = 1
        client._stop = stop_event
    started = time.monotonic()
    try:
        client._rx_loop(stop_event, 1, threading.Event())
    finally:
        client._sock = real_sock

    assert time.monotonic() - started < 0.05
    assert client._rx_invalid_packets == 1
    assert client._rx_socket_errors == 0
