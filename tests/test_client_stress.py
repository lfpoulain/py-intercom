from __future__ import annotations

import socket
import threading
import time

import numpy as np
import pytest

from py_intercom.client import client as client_mod
from py_intercom.client.client import ClientConfig, IntercomClient
from py_intercom.common.buses import FIXED_BUS_IDS
from py_intercom.common.constants import FRAME_SAMPLES
from py_intercom.common.packets import unpack_audio_packet_v2


SESSION_TOKEN = 0x123456789ABC0000


class _FakeEncoder:
    def encode(self, frame: np.ndarray) -> bytes:
        return b"opus"


class _FakeDecoder:
    def decode(self, payload: bytes) -> np.ndarray:
        return np.zeros((int(FRAME_SAMPLES),), dtype=np.float32)


@pytest.fixture
def stress_client(monkeypatch):
    monkeypatch.setattr(client_mod, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(client_mod, "OpusDecoder", _FakeDecoder)
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(("127.0.0.1", 0))
    client = IntercomClient(
        client_id=321,
        config=ClientConfig(
            server_ip="127.0.0.1",
            server_port=int(udp_server.getsockname()[1]),
            control_port=1,
        ),
    )

    yield client, udp_server

    try:
        client.stop()
    except Exception:
        pass
    udp_server.close()


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


def _activate_audio(client: IntercomClient, *, token: int, bus_id: int) -> None:
    client._stop = threading.Event()
    client._udp_ready = threading.Event()
    client._udp_ready.set()
    client._session_token = int(token)
    with client._state_lock:
        client._ptt_buses = {int(bus_id): True}


def test_one_hundred_network_generations_leave_no_live_worker(stress_client):
    client, _udp_server = stress_client
    generation_barriers: dict[int, threading.Barrier] = {}
    entered_threads: list[threading.Thread] = []
    entered_lock = threading.Lock()

    def simulated_worker(
        stop_event: threading.Event,
        generation: int,
        udp_ready: threading.Event,
    ) -> None:
        with entered_lock:
            entered_threads.append(threading.current_thread())
        generation_barriers[int(generation)].wait(timeout=1.0)
        stop_event.wait()

    client._rx_loop = simulated_worker
    client._control_loop = simulated_worker
    client._tx_loop = simulated_worker
    client._playout_loop = simulated_worker

    expected_generation = 1
    generation_barriers[expected_generation] = threading.Barrier(5)
    client._start_network_threads()
    generation_barriers[expected_generation].wait(timeout=1.0)

    for expected_generation in range(2, 101):
        generation_barriers[expected_generation] = threading.Barrier(5)
        client.reconnect_network()
        generation_barriers[expected_generation].wait(timeout=1.0)
        assert client._network_generation == expected_generation

    assert client._stop_network() is True
    assert client._network_generation == 100
    assert len(entered_threads) == 400
    assert len({thread.name for thread in entered_threads}) == 400
    assert not any(thread.is_alive() for thread in entered_threads)
    assert client._ctrl_thread is None
    assert client._rx_thread is None
    assert client._tx_thread is None
    assert client._playout_thread is None


def test_client_rings_remain_bounded_and_drop_oldest_under_load(stress_client):
    client, _udp_server = stress_client
    first_bus = int(min(FIXED_BUS_IDS))
    _activate_audio(client, token=SESSION_TOKEN, bus_id=first_bus)
    input_frame = np.empty((int(FRAME_SAMPLES), 1), dtype=np.float32)
    playout_frame = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
    frame_count = 5_000

    for sequence in range(frame_count):
        value = float(sequence % 100) / 200.0
        input_frame.fill(value)
        playout_frame.fill(-value)
        client._in_callback(input_frame, int(FRAME_SAMPLES), None, None)
        client._playout_ring.push(playout_frame, metadata=sequence)

    tx_stats = client._tx_ring.stats
    playout_stats = client._playout_ring.stats
    assert tx_stats.depth == client._tx_ring.capacity
    assert playout_stats.depth == client._playout_ring.capacity
    assert tx_stats.dropped == frame_count - client._tx_ring.capacity
    assert playout_stats.dropped == frame_count - client._playout_ring.capacity

    output = np.empty((int(FRAME_SAMPLES),), dtype=np.float32)
    playout_metadata: list[int] = []
    while client._playout_ring.depth:
        metadata = client._playout_ring.pop_into(output)
        assert metadata is not None
        playout_metadata.append(metadata)
    assert playout_metadata == list(
        range(frame_count - client._playout_ring.capacity, frame_count)
    )


def test_rapid_ptt_and_epoch_changes_never_send_an_old_frame(
    stress_client,
    monkeypatch,
):
    client, _udp_server = stress_client
    first_encode_entered = threading.Event()
    release_first_encode = threading.Event()
    encoders_created = 0

    class _EpochEncoder:
        def __init__(self) -> None:
            nonlocal encoders_created
            self.instance_number = encoders_created
            encoders_created += 1

        def encode(self, frame: np.ndarray) -> bytes:
            if self.instance_number == 0:
                first_encode_entered.set()
                assert release_first_encode.wait(timeout=1.0)
            return b"opus"

    class _RecordingSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.packet_sent = threading.Event()

        def sendto(self, data: bytes, addr: tuple) -> None:
            self.sent.append(bytes(data))
            self.packet_sent.set()

        def close(self) -> None:
            pass

    client._sock.close()
    recording_socket = _RecordingSocket()
    client._sock = recording_socket
    monkeypatch.setattr(client_mod, "OpusEncoder", _EpochEncoder)
    bus_ids = sorted(int(bus_id) for bus_id in FIXED_BUS_IDS)
    _activate_audio(client, token=SESSION_TOKEN, bus_id=bus_ids[0])
    tx_thread = threading.Thread(
        target=client._tx_loop,
        args=(client._stop, client._network_generation, client._udp_ready),
        name="stress-epoch-tx",
    )
    client._tx_thread = tx_thread
    tx_thread.start()

    frame = np.ones((int(FRAME_SAMPLES), 1), dtype=np.float32)
    client._in_callback(frame, int(FRAME_SAMPLES), None, None)
    assert first_encode_entered.wait(timeout=1.0)

    final_token = SESSION_TOKEN
    final_bus = bus_ids[0]
    for change in range(1, 51):
        final_token = SESSION_TOKEN + change
        final_bus = bus_ids[change % len(bus_ids)]
        client._activate_session_token(final_token)
        with client._state_lock:
            client._ptt_buses = {final_bus: True}
        frame.fill(float(change) / 100.0)
        client._in_callback(frame, int(FRAME_SAMPLES), None, None)

    release_first_encode.set()
    assert recording_socket.packet_sent.wait(timeout=1.0)
    assert _wait_until(lambda: client._tx_ring.depth == 0)

    client._stop.set()
    tx_thread.join(timeout=1.0)
    assert not tx_thread.is_alive()
    assert len(recording_socket.sent) == 1
    packet = unpack_audio_packet_v2(recording_socket.sent[0])
    assert packet.session_token == final_token
    assert packet.bus_mask == 1 << final_bus
    assert packet.sequence_number == 1
    assert encoders_created == 2


def test_stop_network_interrupts_tx_worker_blocked_on_empty_ring(stress_client):
    client, _udp_server = stress_client
    stop_event = threading.Event()
    udp_ready = threading.Event()
    wait_entered = threading.Event()
    original_wait = client._tx_ring.wait_pop_into

    def tracked_wait(*args, **kwargs):
        wait_entered.set()
        return original_wait(*args, **kwargs)

    client._tx_ring.wait_pop_into = tracked_wait
    with client._network_state_lock:
        client._network_generation = 1
        client._stop = stop_event
        client._udp_ready = udp_ready
    tx_thread = threading.Thread(
        target=client._tx_loop,
        args=(stop_event, 1, udp_ready),
        name="stress-blocked-tx",
    )
    client._tx_thread = tx_thread
    tx_thread.start()

    assert wait_entered.wait(timeout=1.0)
    started = time.monotonic()
    assert client._stop_network() is True
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert not tx_thread.is_alive()
    assert client._tx_thread is None
    assert client._tx_ring.stats.underflows == 0
