"""End-to-end client <-> server tests over real loopback sockets.

Everything is real except the sound cards: the server runs its control, UDP,
mix and broadcast workers; clients run their full network stack with real
Opus. Microphone input is injected through the client's capture callback and
playback is read from the client's playout ring.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid

import numpy as np
import pytest

from py_intercom.client.client import ClientConfig, IntercomClient
from py_intercom.common.buses import PLATEAU_BUS_ID, REGIE_BUS_ID
from py_intercom.common.constants import FRAME_SAMPLES, SAMPLE_RATE
from py_intercom.common.identity import client_id_from_uuid
from py_intercom.server.server import IntercomServer

FRAME_PERIOD_S = FRAME_SAMPLES / SAMPLE_RATE
SILENCE_DBFS = -50.0
SPEECH_DBFS = -30.0


class _FakeStream:
    active = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    abort = stop

    def close(self) -> None:
        self.active = False


def _free_port_pair() -> int:
    """Return a UDP port whose TCP neighbour (control port) is also free."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(("127.0.0.1", 0))
            port = int(udp.getsockname()[1])
        if port >= 65535:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
                tcp.bind(("127.0.0.1", port + 1))
        except OSError:
            continue
        return port
    raise RuntimeError("no free UDP/TCP port pair on loopback")


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _rms_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    mean_square = float(np.mean(np.square(samples, dtype=np.float64)))
    return -120.0 if mean_square <= 0.0 else 10.0 * np.log10(mean_square)


class _Microphone:
    """Feeds a sine wave into a client's capture callback in real time."""

    def __init__(self, client: IntercomClient, frequency_hz: float = 440.0) -> None:
        self._client = client
        self._frequency_hz = frequency_hz
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        phase = 0
        next_tick = time.monotonic()
        while not self._stop.is_set():
            t = (np.arange(FRAME_SAMPLES) + phase) / SAMPLE_RATE
            frame = (0.5 * np.sin(2.0 * np.pi * self._frequency_hz * t)).astype(np.float32)
            phase += FRAME_SAMPLES
            self._client._in_callback(frame.reshape(-1, 1), FRAME_SAMPLES, None, None)
            next_tick += FRAME_PERIOD_S
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def __enter__(self) -> _Microphone:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def _listen(client: IntercomClient, duration_s: float) -> np.ndarray:
    """Drain a client's playout ring for ``duration_s`` like a headphone callback."""
    frame = np.empty(FRAME_SAMPLES, dtype=np.float32)
    received: list[np.ndarray] = []
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        while client._playout_ring.pop_into(frame) is not None:
            received.append(frame.copy())
        time.sleep(FRAME_PERIOD_S / 2)
    return np.concatenate(received) if received else np.zeros(0, dtype=np.float32)


@pytest.fixture
def server(tmp_path):
    # Another process can grab the probed port pair before the server binds
    # it; retry on a fresh pair rather than failing the test.
    for attempt in range(5):
        srv = None
        try:
            srv = IntercomServer(
                bind_ip="127.0.0.1",
                port=_free_port_pair(),
                preset_path=str(tmp_path / f"server_preset_{attempt}.json"),
                discovery_enabled=False,
            )
            srv.start()
            break
        except OSError:
            if srv is not None:
                srv.stop()
    else:
        pytest.fail("could not start a loopback server on a free port")
    yield srv
    srv.stop()


@pytest.fixture
def connect(server, monkeypatch):
    clients: list[IntercomClient] = []

    def _connect(name: str) -> IntercomClient:
        client_uuid = str(uuid.uuid4())
        client = IntercomClient(
            client_id=client_id_from_uuid(client_uuid),
            config=ClientConfig(
                server_ip="127.0.0.1",
                server_port=server.port,
                name=name,
                client_uuid=client_uuid,
            ),
        )
        monkeypatch.setattr(client, "_open_input_stream", _FakeStream)
        monkeypatch.setattr(client, "_open_output_stream", _FakeStream)
        client.start()
        clients.append(client)
        assert _wait_until(lambda: client.get_stats_snapshot()["control_ready"]), (
            f"{name} never reached the online state"
        )
        return client

    yield _connect
    for client in clients:
        client.stop()


def test_clients_complete_handshake_and_appear_on_server(server, connect):
    alice = connect("Alice")
    bob = connect("Bob")

    snapshot = server.get_clients_snapshot()
    assert snapshot[alice.client_id]["name"] == "Alice"
    assert snapshot[bob.client_id]["name"] == "Bob"
    assert all(entry["control_connected"] for entry in snapshot.values())


def test_regie_talk_reaches_other_client_but_not_the_talker(server, connect):
    alice = connect("Alice")
    bob = connect("Bob")

    alice.set_ptt_bus(REGIE_BUS_ID, True)
    with _Microphone(alice):
        _listen(alice, 0.3)
        _listen(bob, 0.3)
        heard_by_bob = _listen(bob, 0.5)
        heard_by_alice = _listen(alice, 0.5)

    assert _rms_dbfs(heard_by_bob) > SPEECH_DBFS
    # Mix-minus: a talker never hears their own voice back.
    assert _rms_dbfs(heard_by_alice) < SILENCE_DBFS


def test_releasing_ptt_stops_audio(server, connect):
    alice = connect("Alice")
    bob = connect("Bob")

    with _Microphone(alice):
        alice.set_ptt_bus(REGIE_BUS_ID, True)
        _listen(bob, 0.3)
        assert _rms_dbfs(_listen(bob, 0.3)) > SPEECH_DBFS

        alice.set_ptt_bus(REGIE_BUS_ID, False)
        _listen(bob, 0.2)
        assert _rms_dbfs(_listen(bob, 0.4)) < SILENCE_DBFS


def test_plateau_talk_without_feed_does_not_reach_regie_listeners(server, connect):
    server.set_bus_feed_to_regie(PLATEAU_BUS_ID, False)
    alice = connect("Alice")
    bob = connect("Bob")

    alice.set_ptt_bus(PLATEAU_BUS_ID, True)
    with _Microphone(alice):
        _listen(bob, 0.3)
        heard_by_bob = _listen(bob, 0.5)

    assert _rms_dbfs(heard_by_bob) < SILENCE_DBFS


def test_end_to_end_latency_stays_low(server, connect):
    alice = connect("Alice")
    bob = connect("Bob")
    alice.set_ptt_bus(REGIE_BUS_ID, True)
    _listen(bob, 0.2)

    with _Microphone(alice):
        started = time.monotonic()
        frame = np.empty(FRAME_SAMPLES, dtype=np.float32)
        first_audio_s = None
        while time.monotonic() - started < 2.0:
            if bob._playout_ring.pop_into(frame) is not None and _rms_dbfs(frame) > SPEECH_DBFS:
                first_audio_s = time.monotonic() - started
                break
            time.sleep(0.001)

    assert first_audio_s is not None, "Bob never heard Alice"
    # Network + jitter pre-buffering on both hops, without the sound cards.
    assert first_audio_s < 0.25


def test_disconnect_revokes_the_session_on_the_server(server, connect):
    alice = connect("Alice")
    bob = connect("Bob")

    alice.disconnect_network()

    assert _wait_until(
        lambda: not server.get_clients_snapshot()[alice.client_id]["control_connected"]
    )
    assert server.get_clients_snapshot()[bob.client_id]["control_connected"]


def test_client_reconnects_with_a_fresh_session(server, connect):
    alice = connect("Alice")
    bob = connect("Bob")

    alice.reconnect_network()
    assert _wait_until(lambda: alice.get_stats_snapshot()["control_ready"])

    alice.set_ptt_bus(REGIE_BUS_ID, True)
    with _Microphone(alice):
        _listen(bob, 0.3)
        assert _rms_dbfs(_listen(bob, 0.5)) > SPEECH_DBFS


class _ProgramFeed:
    """Plays a 2 kHz program tone into the server's Return bus in real time."""

    FREQUENCY_HZ = 2000.0

    def __init__(self, server: IntercomServer) -> None:
        self._server = server
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        phase = 0
        next_tick = time.monotonic()
        while not self._stop.is_set():
            t = (np.arange(FRAME_SAMPLES) + phase) / SAMPLE_RATE
            phase += FRAME_SAMPLES
            self._server._return_frames.push((0.3 * np.sin(2.0 * np.pi * self.FREQUENCY_HZ * t)).astype(np.float32))
            next_tick += FRAME_PERIOD_S
            time.sleep(max(0.0, next_tick - time.monotonic()))

    def __enter__(self) -> _ProgramFeed:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def _tone_level_db(samples: np.ndarray, frequency_hz: float) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    freqs = np.fft.rfftfreq(samples.size, 1.0 / SAMPLE_RATE)
    band = (freqs > frequency_hz - 50) & (freqs < frequency_hz + 50)
    return 20.0 * np.log10(float(spectrum[band].max()) + 1e-12)


def test_return_ducking_lowers_the_program_while_someone_talks(server, connect):
    server.set_return_enabled(True)
    alice = connect("Alice")
    bob = connect("Bob")
    bob.set_listen_return_bus(True)
    bob.set_duck_return(True)
    assert _wait_until(lambda: server.get_clients_snapshot()[bob.client_id]["duck_return"])

    with _ProgramFeed(server):
        _listen(bob, 0.4)
        program_alone = _tone_level_db(_listen(bob, 0.5), _ProgramFeed.FREQUENCY_HZ)

        alice.set_ptt_bus(REGIE_BUS_ID, True)
        with _Microphone(alice):
            _listen(bob, 0.4)
            program_ducked = _tone_level_db(_listen(bob, 0.5), _ProgramFeed.FREQUENCY_HZ)
        alice.set_ptt_bus(REGIE_BUS_ID, False)

        _listen(bob, 1.0)  # hold, then release
        program_restored = _tone_level_db(_listen(bob, 0.5), _ProgramFeed.FREQUENCY_HZ)

    assert program_alone - program_ducked > 10.0
    assert abs(program_restored - program_alone) < 3.0


def test_return_is_not_ducked_when_the_option_is_off(server, connect):
    server.set_return_enabled(True)
    alice = connect("Alice")
    bob = connect("Bob")
    bob.set_listen_return_bus(True)

    with _ProgramFeed(server):
        _listen(bob, 0.4)
        program_alone = _tone_level_db(_listen(bob, 0.5), _ProgramFeed.FREQUENCY_HZ)
        alice.set_ptt_bus(REGIE_BUS_ID, True)
        with _Microphone(alice):
            _listen(bob, 0.4)
            program_with_voice = _tone_level_db(_listen(bob, 0.5), _ProgramFeed.FREQUENCY_HZ)

    assert abs(program_alone - program_with_voice) < 3.0
