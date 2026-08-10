import json
import queue
import struct
import threading
import time

import numpy as np
import pytest

from py_intercom.common.buses import (
    FIXED_BUSES,
    FIXED_BUS_IDS,
    PLATEAU_BUS_ID,
    REGIE_BUS_ID,
    VMIX_BUS_ID,
)
from py_intercom.common.constants import CTRL_LIVENESS_TIMEOUT_S, FRAME_SAMPLES
from py_intercom.common.control_protocol import (
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_LINE_BYTES,
    JsonLineBuffer,
    encode_control_message,
    parse_session_token,
)
from py_intercom.common.jitter_buffer import JitterFrame, JitterFrameMetadata, OpusPacketJitterBuffer
from py_intercom.common.identity import client_id_from_uuid
from py_intercom.common.packets import (
    AudioPacketDirection,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)
from py_intercom.common.routing import RoutedSource, RoutingResult
import py_intercom.server.server as server_mod


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
    def __init__(self, *args, **kwargs):
        self.frames = []

    def encode(self, frame):
        self.frames.append(np.array(frame, dtype=np.float32, copy=True))
        return b"opus"


class _FakeDecoder:
    def __init__(self, *args, frame_samples=FRAME_SAMPLES, **kwargs):
        self.frame_samples = int(frame_samples)

    def decode(self, payload):
        return np.zeros((self.frame_samples,), dtype=np.float32)


class _ValueDecoder:
    def __init__(self, value):
        self.value = float(value)

    def decode(self, payload):
        return np.full((FRAME_SAMPLES,), self.value, dtype=np.float32)


class _OneFrameJitter:
    def __init__(self, client_id, bus_mask, *, token=SESSION_TOKEN):
        self._frame = JitterFrame(
            sequence_number=1,
            payload=b"opus",
            metadata=JitterFrameMetadata(
                session_token=int(token),
                client_id=int(client_id),
                bus_mask=int(bus_mask),
                flags=int(AudioPacketDirection.CLIENT_TO_SERVER),
            ),
        )

    def pop_frame(self):
        frame, self._frame = self._frame, None
        return frame

    def reset(self):
        self._frame = None


class _AlwaysReadyJitter:
    def pop_frame(self):
        return JitterFrame(
            sequence_number=1,
            payload=b"opus",
            metadata=JitterFrameMetadata(
                session_token=SESSION_TOKEN,
                client_id=1,
                bus_mask=1,
                flags=int(AudioPacketDirection.CLIENT_TO_SERVER),
            ),
        )

    def reset(self):
        return None


class _ScriptedConnection:
    def __init__(self, *chunks, on_send=None):
        self._chunks = list(chunks)
        self._on_send = on_send
        self.sent = []
        self.closed = False

    def settimeout(self, timeout):
        return None

    def recv(self, size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def sendall(self, data):
        self.sent.append(bytes(data))
        if self._on_send is not None:
            self._on_send()

    def close(self):
        self.closed = True


class _TrackedAudioStream:
    def __init__(
        self,
        events,
        label,
        *,
        callback=None,
        active=False,
        fail_on_start=False,
    ):
        self.events = events
        self.label = str(label)
        self.callback = callback
        self.active = bool(active)
        self.fail_on_start = bool(fail_on_start)
        self.stopped = False
        self.closed = False

    def start(self):
        self.events.append(f"{self.label}-start")
        if self.fail_on_start:
            raise RuntimeError(f"{self.label} failed")
        self.active = True

    def stop(self):
        self.events.append(f"{self.label}-stop")
        self.active = False
        self.stopped = True

    def close(self):
        self.events.append(f"{self.label}-close")
        self.active = False
        self.closed = True


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(server_mod, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(server_mod, "OpusDecoder", _FakeDecoder)
    srv = server_mod.IntercomServer(
        bind_ip="127.0.0.1",
        port=0,
        preset_path=str(tmp_path / "server-preset.json"),
        discovery_enabled=False,
    )
    try:
        yield srv
    finally:
        srv.stop()


def _client_state(*, addr=("127.0.0.1", 41000), now=None, decoder=None):
    current = time.monotonic() if now is None else float(now)
    return server_mod.ClientState(
        addr=addr,
        last_packet_monotonic=current,
        decoder=decoder or _FakeDecoder(),
        encoder=_FakeEncoder(),
        jb=OpusPacketJitterBuffer(start_frames=1, max_frames=8),
        control_connected=True,
        last_control_monotonic=current,
        session_token=SESSION_TOKEN,
        ptt_buses={0: True},
    )


def _client_packet(
    client_id: int,
    *,
    token: int = SESSION_TOKEN,
    bus_mask: int = 1,
    direction: AudioPacketDirection = AudioPacketDirection.CLIENT_TO_SERVER,
    sequence_number: int = 1,
):
    return unpack_audio_packet_v2(
        pack_audio_packet_v2(
            session_token=token,
            client_id=client_id,
            timestamp_ms=123,
            sequence_number=sequence_number,
            stream_id=0,
            bus_mask=bus_mask,
            direction=direction,
            payload=b"opus",
        )
    )


def test_unexpected_decoded_frame_size_never_kills_mixer(server):
    now = time.monotonic()
    state = _client_state(now=now, decoder=_FakeDecoder(frame_samples=120))
    state.jb = _AlwaysReadyJitter()
    with server._lock:
        server._clients[1] = state

    mixer = threading.Thread(target=server._mix_loop, daemon=True)
    mixer.start()

    deadline = time.monotonic() + 0.5
    while server._rx_invalid_frame_sizes == 0 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert server._rx_invalid_frame_sizes > 0
    assert mixer.is_alive()

    server._stop.set()
    mixer.join(timeout=1.0)
    assert not mixer.is_alive()


def test_bus_routing_uses_frame_mask_and_fresh_control_liveness():
    now = 100.0

    assert server_mod.IntercomServer._live_frame_bus_mask(0, True, now, now=now) == 0
    assert server_mod.IntercomServer._live_frame_bus_mask(1, False, now, now=now) == 0
    assert server_mod.IntercomServer._live_frame_bus_mask(
        1,
        True,
        now - CTRL_LIVENESS_TIMEOUT_S - 0.01,
        now=now,
    ) == 0
    assert server_mod.IntercomServer._live_frame_bus_mask(1 << 1, True, now, now=now) == 1 << 1
    assert server_mod.IntercomServer._live_frame_bus_mask(1 << 0, True, now, now=now) == 1 << 0
    assert server_mod.IntercomServer._live_frame_bus_mask(1 << 15, True, now, now=now) == 0


def test_mix_loop_delegates_fixed_bus_routing_to_pure_engine(server, monkeypatch):
    now = time.monotonic()
    double_db = 20.0 * np.log10(2.0)
    quadruple_db = 20.0 * np.log10(4.0)

    direct = _client_state(now=now, decoder=_ValueDecoder(0.10))
    direct.jb = _OneFrameJitter(1, 1 << REGIE_BUS_ID)

    multi_ptt = _client_state(now=now, decoder=_ValueDecoder(0.20))
    multi_ptt.jb = _OneFrameJitter(
        2,
        (1 << PLATEAU_BUS_ID) | (1 << VMIX_BUS_ID),
    )
    multi_ptt.input_gain_db = float(double_db)
    multi_ptt.applies_input_gain_locally = False

    stale = _client_state(now=now, decoder=_ValueDecoder(0.50))
    stale.jb = _OneFrameJitter(3, 1 << REGIE_BUS_ID)
    stale.last_control_monotonic = now - CTRL_LIVENESS_TIMEOUT_S - 1.0

    with server._lock:
        server._clients.update({1: direct, 2: multi_ptt, 3: stale})
        server._buses[PLATEAU_BUS_ID].gain_db = float(double_db)
        server._buses[PLATEAU_BUS_ID].feed_to_regie = True
        server._buses[VMIX_BUS_ID].gain_db = float(quadruple_db)
        server._buses[VMIX_BUS_ID].feed_to_regie = True
        server._outputs.update(
            {
                10: server_mod.OutputState(10, 0, REGIE_BUS_ID),
                11: server_mod.OutputState(11, 0, PLATEAU_BUS_ID),
                12: server_mod.OutputState(12, 0, VMIX_BUS_ID),
            }
        )

    real_route_sources = server_mod.route_sources
    real_build_destination_mix = server_mod.build_destination_mix
    route_calls = []
    destination_calls = []

    def route_spy(sources, config):
        source_list = list(sources)
        route_calls.append((source_list, config))
        result = real_route_sources(source_list, config)
        server._stop.set()
        return result

    def destination_spy(*args, **kwargs):
        destination_calls.append((args, kwargs))
        return real_build_destination_mix(*args, **kwargs)

    monkeypatch.setattr(server_mod, "route_sources", route_spy)
    monkeypatch.setattr(server_mod, "build_destination_mix", destination_spy)

    server._mix_loop()

    assert len(route_calls) == 1
    routed_sources, routing_config = route_calls[0]
    assert {source.client_id: source.bus_mask for source in routed_sources} == {
        1: 1 << REGIE_BUS_ID,
        2: (1 << PLATEAU_BUS_ID) | (1 << VMIX_BUS_ID),
        3: 0,
    }
    assert routing_config.feed_to_regie[PLATEAU_BUS_ID]
    assert routing_config.feed_to_regie[VMIX_BUS_ID]
    assert len(destination_calls) == 1

    envelope = server._mix_queue.get_nowait()
    assert isinstance(envelope, server_mod._RoutingEnvelope)
    assert envelope.routing_epoch == server._routing_epoch
    result = envelope.result
    assert isinstance(result, RoutingResult)
    np.testing.assert_allclose(result.bus_frames[REGIE_BUS_ID], 0.10, atol=1e-6)
    np.testing.assert_allclose(result.bus_frames[PLATEAU_BUS_ID], 0.80, atol=1e-6)
    np.testing.assert_allclose(result.bus_frames[VMIX_BUS_ID], 1.60, atol=1e-6)
    # The multi-PTT client feeds the Régie monitor once, via Plateau first.
    np.testing.assert_allclose(result.regie_linear, 0.90, atol=1e-6)
    np.testing.assert_allclose(result.regie_contribution_by_client[2], 0.80, atol=1e-6)

    output_frame = np.empty((FRAME_SAMPLES,), dtype=np.float32)
    assert server._outputs[10].ring.pop_into(output_frame) is not None
    np.testing.assert_allclose(output_frame, 0.10, atol=1e-6)
    assert server._outputs[11].ring.pop_into(output_frame) is not None
    np.testing.assert_allclose(output_frame, 0.80, atol=1e-6)
    # VMix is linear at 1.60 above, and limited only for its physical output.
    assert server._outputs[12].ring.pop_into(output_frame) is not None
    np.testing.assert_allclose(output_frame, 0.99, atol=1e-6)


def test_mix_queue_drops_oldest_frame_when_full(server):
    server._mix_queue = queue.Queue(maxsize=2)
    server._mix_queue.put_nowait("oldest")
    server._mix_queue.put_nowait("middle")

    server._enqueue_latest_mix("newest")

    assert server._mix_queue.get_nowait() == "middle"
    assert server._mix_queue.get_nowait() == "newest"
    assert server._mix_queue_drops == 1


def test_routing_epoch_invalidates_queued_and_device_audio(server):
    output = server_mod.OutputState(
        output_id=3,
        device=0,
        bus_id=PLATEAU_BUS_ID,
    )
    output.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))
    routed = server_mod.route_sources([])
    old_epoch = server._routing_epoch
    server._mix_queue.put_nowait(
        server_mod._RoutingEnvelope(routing_epoch=old_epoch, result=routed)
    )

    with server._lock:
        server._outputs[output.output_id] = output
        new_epoch = server._advance_routing_epoch_locked()

    assert new_epoch != old_epoch
    assert server._mix_queue.empty()
    assert server._stale_mix_epochs_dropped == 1
    scratch = np.empty((FRAME_SAMPLES,), dtype=np.float32)
    assert output.ring.pop_into(scratch) is None


def test_routing_epoch_keeps_old_packet_out_of_replacement_decoder(server):
    class _RecordingDecoder:
        def __init__(self, value):
            self.value = float(value)
            self.payloads = []

        def decode(self, payload):
            self.payloads.append(bytes(payload))
            return np.full((FRAME_SAMPLES,), self.value, dtype=np.float32)

    old_decoder = _RecordingDecoder(0.25)
    replacement_decoder = _RecordingDecoder(0.50)
    now = time.monotonic()
    state = _client_state(now=now, decoder=old_decoder)

    class _ReplacingJitter:
        def pop_frame(self):
            with server._lock:
                state.decoder = replacement_decoder
                state.jb = OpusPacketJitterBuffer(start_frames=1, max_frames=8)
                state.session_token = SESSION_TOKEN + 1
                server._advance_routing_epoch_locked()
            server._stop.set()
            return JitterFrame(
                sequence_number=1,
                payload=b"old-session-opus",
                metadata=JitterFrameMetadata(
                    session_token=SESSION_TOKEN,
                    client_id=7,
                    bus_mask=1 << REGIE_BUS_ID,
                    flags=int(AudioPacketDirection.CLIENT_TO_SERVER),
                ),
            )

        def reset(self):
            return None

    state.jb = _ReplacingJitter()
    with server._lock:
        server._clients[7] = state

    server._mix_loop()

    assert old_decoder.payloads == [b"old-session-opus"]
    assert replacement_decoder.payloads == []
    assert server._mix_queue.empty()
    assert server._stale_mix_epochs_dropped == 1


def test_forget_client_atomically_revokes_session_and_queued_audio(server):
    client_uuid = "forgotten-live-client"
    client_id = client_id_from_uuid(client_uuid)
    state = _client_state()
    state.client_uuid = client_uuid
    connection = _ScriptedConnection()
    routed = server_mod.route_sources(
        [
            RoutedSource(
                client_id,
                np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32),
                1 << REGIE_BUS_ID,
            )
        ]
    )
    output = server_mod.OutputState(8, 0, PLATEAU_BUS_ID)
    output.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))
    with server._lock:
        server._clients[client_id] = state
        server._client_profiles[client_uuid] = server_mod.ClientProfile(client_uuid)
        server._outputs[output.output_id] = output
        old_epoch = server._routing_epoch
        server._mix_queue.put_nowait(
            server_mod._RoutingEnvelope(old_epoch, routed)
        )
    with server._ctrl_lock:
        server._ctrl_sessions[client_id] = connection
        server._ctrl_send_locks[client_id] = threading.Lock()

    server.forget_client(client_id)

    assert server._routing_epoch != old_epoch
    assert server._mix_queue.empty()
    assert output.ring.depth == 0
    assert client_id not in server._clients
    assert client_uuid not in server._client_profiles
    assert client_id not in server._ctrl_sessions
    assert connection.closed
    kick = JsonLineBuffer().feed(connection.sent[0])[0]
    assert kick["type"] == "kick"


def test_output_callback_uses_preallocated_frame_and_silences_underflow(
    server,
    monkeypatch,
):
    state = server_mod.OutputState(
        output_id=3,
        device=0,
        bus_id=PLATEAU_BUS_ID,
        gain_db=float(20.0 * np.log10(2.0)),
    )
    state.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))
    outdata = np.full((FRAME_SAMPLES, 1), 7.0, dtype=np.float32)

    def forbid_concatenate(*args, **kwargs):
        raise AssertionError("audio callback must not concatenate buffers")

    with monkeypatch.context() as isolated:
        isolated.setattr(server_mod.np, "concatenate", forbid_concatenate)
        server._output_state_callback(state, outdata, FRAME_SAMPLES, None, None)

    np.testing.assert_allclose(outdata[:, 0], 0.50, atol=1e-6)
    assert state.ring.stats.popped == 1
    assert state.underflows == 0

    outdata.fill(7.0)
    server._output_state_callback(state, outdata, FRAME_SAMPLES, None, None)

    np.testing.assert_array_equal(outdata, 0.0)
    assert state.underflows == 1
    assert state.ring.stats.underflows == 1
    assert state.vu_dbfs == -60.0


def test_output_ring_drops_oldest_frame_instead_of_accumulating_latency(server):
    state = server_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    state.ring = server_mod.FrameRingBuffer(capacity=2, frame_samples=FRAME_SAMPLES)
    for value in (0.1, 0.2, 0.3):
        state.ring.push(np.full((FRAME_SAMPLES,), value, dtype=np.float32))

    assert state.ring.stats.depth == 2
    assert state.ring.stats.dropped == 1

    outdata = np.empty((FRAME_SAMPLES, 1), dtype=np.float32)
    server._output_state_callback(state, outdata, FRAME_SAMPLES, None, None)
    np.testing.assert_allclose(outdata[:, 0], 0.2, atol=1e-6)
    server._output_state_callback(state, outdata, FRAME_SAMPLES, None, None)
    np.testing.assert_allclose(outdata[:, 0], 0.3, atol=1e-6)


def test_udp_rejects_unknown_inactive_and_wrong_source_clients(server):
    now = time.monotonic()
    packet = _client_packet(7)

    assert not server._accept_udp_packet(packet, ("127.0.0.1", 41000), now=now)
    assert 7 not in server._clients
    assert server._rx_unknown_clients == 1

    state = _client_state(addr=("127.0.0.1", 41000), now=now)
    state.control_connected = False
    with server._lock:
        server._clients[7] = state
    assert not server._accept_udp_packet(packet, ("127.0.0.1", 41000), now=now)
    assert server._rx_inactive_clients == 1

    state.control_connected = True
    state.last_control_monotonic = now
    state.session_token = SESSION_TOKEN
    assert not server._accept_udp_packet(packet, ("127.0.0.1", 42000), now=now)
    assert state.addr == ("127.0.0.1", 41000)
    assert state.jb.buffered_frames == 0
    assert server._rx_wrong_source == 1

    assert server._accept_udp_packet(packet, ("127.0.0.1", 41000), now=now)
    assert state.addr == ("127.0.0.1", 41000)
    assert state.jb.buffered_frames == 1
    assert server._rx_packets == 1


def test_udp_token_direction_and_route_validation_is_fail_closed(server):
    now = time.monotonic()
    state = _client_state(now=now)
    with server._lock:
        server._clients[7] = state

    assert not server._accept_udp_packet(
        _client_packet(7, token=SESSION_TOKEN + 1), state.addr, now=now
    )
    assert server._rx_invalid_tokens == 1
    assert state.jb.buffered_frames == 0

    assert not server._accept_udp_packet(
        _client_packet(7, direction=AudioPacketDirection.SERVER_TO_CLIENT),
        state.addr,
        now=now,
    )
    assert server._rx_invalid_directions == 1

    assert not server._accept_udp_packet(
        _client_packet(7, bus_mask=1 << 15), state.addr, now=now
    )
    assert server._rx_invalid_route_masks == 1
    assert state.jb.buffered_frames == 0


def test_routing_metadata_stays_with_frame_when_tcp_ptt_changes(server):
    now = time.monotonic()
    state = _client_state(now=now)
    with server._lock:
        server._clients[7] = state

    assert server._accept_udp_packet(
        _client_packet(7, bus_mask=1 << 0), state.addr, now=now
    )
    state.ptt_buses = {2: True}

    frame = state.jb.pop_frame()
    assert frame is not None
    assert frame.metadata.bus_mask == 1 << 0
    effective_mask = server._live_frame_bus_mask(
        frame.metadata.bus_mask,
        state.control_connected,
        state.last_control_monotonic,
        now=now,
    )
    assert effective_mask == 1 << 0
    assert effective_mask & (1 << 0)
    assert not effective_mask & (1 << 2)


def test_stale_sessions_are_ignored_for_receive_and_broadcast(server):
    now = time.monotonic()
    state = _client_state(now=now)
    with server._lock:
        server._clients[8] = state

    expired_at = now + CTRL_LIVENESS_TIMEOUT_S + 0.01
    packet = _client_packet(8)

    assert not server._accept_udp_packet(packet, state.addr, now=expired_at)
    assert not state.control_connected
    assert state.session_token == 0
    assert server._active_broadcast_destinations(now=expired_at) == []


def test_reopen_return_always_closes_previous_stream(server, monkeypatch):
    calls = []
    server._running = True
    server._return_enabled = False
    server._return_input_device = 12
    monkeypatch.setattr(server, "_close_return_input_stream", lambda: calls.append("close"))
    monkeypatch.setattr(server, "_open_return_input_stream", lambda device: calls.append(("open", device)))

    server.reopen_return_input()

    assert calls == ["close"]


def test_output_device_switch_starts_candidate_before_atomic_swap(
    server,
    monkeypatch,
):
    events = []
    candidates = []
    old_stream = _TrackedAudioStream(events, "old", active=True)
    old_state = server_mod.OutputState(5, 1, PLATEAU_BUS_ID, gain_db=-3.0)
    old_state.stream = old_stream
    with server._lock:
        server._outputs[5] = old_state
    server._running = True

    monkeypatch.setattr(
        server_mod.sd,
        "query_devices",
        lambda device: {"name": "fake", "hostapi": 0, "max_output_channels": 1},
    )
    monkeypatch.setattr(server_mod.sd, "check_output_settings", lambda **kwargs: None)

    def output_stream_factory(**kwargs):
        stream = _TrackedAudioStream(
            events,
            "candidate",
            callback=kwargs["callback"],
        )
        candidates.append(stream)
        return stream

    monkeypatch.setattr(server_mod.sd, "OutputStream", output_stream_factory)
    monkeypatch.setattr(server, "_autosave_preset", lambda: events.append("autosave"))

    server.set_output_device(5, 2)

    replacement = server._outputs[5]
    assert replacement is not old_state
    assert replacement.device == 2
    assert replacement.bus_id == PLATEAU_BUS_ID
    assert replacement.gain_db == -3.0
    assert replacement.stream is candidates[0]
    assert replacement.stream.active
    assert events[:3] == ["candidate-start", "old-stop", "old-close"]
    assert events[-1] == "autosave"
    assert old_stream.stopped and old_stream.closed


def test_output_device_switch_failure_preserves_live_stream(server, monkeypatch):
    events = []
    old_stream = _TrackedAudioStream(events, "old", active=True)
    old_state = server_mod.OutputState(6, 1, VMIX_BUS_ID)
    old_state.stream = old_stream
    with server._lock:
        server._outputs[6] = old_state
    server._running = True

    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_output_settings", lambda **kwargs: None)
    monkeypatch.setattr(
        server_mod.sd,
        "OutputStream",
        lambda **kwargs: _TrackedAudioStream(
            events,
            "candidate",
            callback=kwargs["callback"],
            fail_on_start=True,
        ),
    )
    autosaves = []
    monkeypatch.setattr(server, "_autosave_preset", lambda: autosaves.append(True))

    with pytest.raises(RuntimeError):
        server.set_output_device(6, 2)

    assert server._outputs[6] is old_state
    assert old_state.device == 1
    assert old_state.stream is old_stream
    assert old_stream.active
    assert not old_stream.stopped
    assert not old_stream.closed
    assert events == ["candidate-start", "candidate-stop", "candidate-close"]
    assert autosaves == []


def test_return_device_switch_is_transactional(server, monkeypatch):
    events = []
    candidates = []
    old_stream = _TrackedAudioStream(events, "old", active=True)
    old_gate = threading.Event()
    old_gate.set()
    server._running = True
    server._return_enabled = True
    server._return_input_device = 1
    server._return_stream = old_stream
    server._return_stream_gate = old_gate
    server._return_frames.push(np.ones((FRAME_SAMPLES,), dtype=np.float32))

    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_input_settings", lambda **kwargs: None)

    def input_stream_factory(**kwargs):
        stream = _TrackedAudioStream(
            events,
            "candidate",
            callback=kwargs["callback"],
        )
        candidates.append(stream)
        return stream

    monkeypatch.setattr(server_mod.sd, "InputStream", input_stream_factory)
    monkeypatch.setattr(server, "_autosave_preset", lambda: events.append("autosave"))

    server.set_return_input_device(2)

    assert server._return_input_device == 2
    assert server._return_stream is candidates[0]
    assert server._return_stream.active
    assert server._return_stream_gate is not None
    assert server._return_stream_gate.is_set()
    assert not old_gate.is_set()
    assert server._return_frames.depth == 0
    assert events[:3] == ["candidate-start", "old-stop", "old-close"]
    assert events[-1] == "autosave"

    candidates[0].callback(
        np.full((FRAME_SAMPLES, 1), 0.25, dtype=np.float32),
        FRAME_SAMPLES,
        None,
        None,
    )
    assert server._return_frames.depth == 1


def test_return_device_switch_failure_preserves_live_stream(server, monkeypatch):
    events = []
    old_stream = _TrackedAudioStream(events, "old", active=True)
    old_gate = threading.Event()
    old_gate.set()
    server._running = True
    server._return_enabled = True
    server._return_input_device = 1
    server._return_stream = old_stream
    server._return_stream_gate = old_gate

    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_input_settings", lambda **kwargs: None)
    monkeypatch.setattr(
        server_mod.sd,
        "InputStream",
        lambda **kwargs: _TrackedAudioStream(
            events,
            "candidate",
            callback=kwargs["callback"],
            fail_on_start=True,
        ),
    )
    autosaves = []
    monkeypatch.setattr(server, "_autosave_preset", lambda: autosaves.append(True))

    with pytest.raises(RuntimeError):
        server.set_return_input_device(2)

    assert server._return_input_device == 1
    assert server._return_stream is old_stream
    assert old_stream.active
    assert not old_stream.stopped
    assert not old_stream.closed
    assert old_gate.is_set()
    assert events == ["candidate-start", "candidate-stop", "candidate-close"]
    assert autosaves == []


def test_disabling_return_closes_stream_and_clears_buffer(server, monkeypatch):
    events = []
    stream = _TrackedAudioStream(events, "return", active=True)
    gate = threading.Event()
    gate.set()
    server._running = True
    server._return_enabled = True
    server._return_input_device = 1
    server._return_stream = stream
    server._return_stream_gate = gate
    server._return_frames.push(np.ones((FRAME_SAMPLES,), dtype=np.float32))
    monkeypatch.setattr(server, "_autosave_preset", lambda: events.append("autosave"))

    server.set_return_enabled(False)

    assert not server._return_enabled
    assert server._return_stream is None
    assert server._return_stream_gate is None
    assert not gate.is_set()
    assert stream.stopped and stream.closed
    assert server._return_frames.depth == 0
    assert events == ["return-stop", "return-close", "autosave"]


def test_critical_worker_failure_is_reported_and_stops_server(server):
    server._running = True

    def crash():
        raise RuntimeError("boom")

    worker = server._new_worker("test-crash", crash)
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert server._stop.is_set()
    assert not server._running
    assert "RuntimeError: boom" in server._worker_errors["test-crash"]


def test_state_before_hello_cannot_modify_an_existing_client(server):
    now = time.monotonic()
    state = _client_state(now=now)
    state.ptt_buses = {0: False}
    with server._lock:
        server._clients[9] = state

    message = json.dumps(
        {
            "type": "state",
            "client_id": 9,
            "ptt_buses": {"0": True},
        }
    ).encode("utf-8") + b"\n"
    connection = _ScriptedConnection(message, b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert state.ptt_buses == {0: False}
    assert connection.closed


def test_replaced_control_session_cannot_disconnect_current_one(server):
    replacement = _ScriptedConnection()
    client_uuid = "test-client"
    client_id = client_id_from_uuid(client_uuid)
    replaced = False

    def replace_session():
        nonlocal replaced
        if replaced:
            return
        replaced = True
        with server._ctrl_lock:
            server._ctrl_sessions[client_id] = replacement

    hello = json.dumps(
        {
            "type": "hello",
            "version": CONTROL_PROTOCOL_VERSION,
            "client_id": client_id,
            "client_uuid": client_uuid,
            "name": "Test",
            "udp_port": 41000,
        }
    ).encode("utf-8") + b"\n"
    old_connection = _ScriptedConnection(hello, b"", on_send=replace_session)

    server._ctrl_session_loop(old_connection, ("127.0.0.1", 50000))

    with server._lock:
        state = server._clients[client_id]
        assert state.control_connected
        assert state.session_token != 0
    with server._ctrl_lock:
        assert server._ctrl_sessions[client_id] is replacement


def test_stale_control_message_cannot_mutate_replacement_session(
    server,
    monkeypatch,
):
    client_uuid = "state-race-client"
    client_id = client_id_from_uuid(client_uuid)
    replacement = _ScriptedConnection()
    replacement_token = SESSION_TOKEN + 1
    replaced = False

    def replace_after_owner_check(*args, **kwargs):
        nonlocal replaced
        if replaced:
            return
        replaced = True
        with server._ctrl_lock:
            server._ctrl_sessions[client_id] = replacement
        with server._lock:
            state = server._clients[client_id]
            state.session_token = replacement_token
            state.ptt_buses = {REGIE_BUS_ID: False}
            state.control_connected = True

    monkeypatch.setattr(server_mod.logger, "trace", replace_after_owner_check)
    connection = _ScriptedConnection(
        encode_control_message(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": client_uuid,
                "udp_port": 41000,
            }
        )
        + encode_control_message(
            {
                "type": "state",
                "ptt_buses": {str(REGIE_BUS_ID): True},
            }
        ),
        b"",
    )

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert replaced
    with server._ctrl_lock:
        assert server._ctrl_sessions[client_id] is replacement
    with server._lock:
        state = server._clients[client_id]
        assert state.session_token == replacement_token
        assert state.ptt_buses == {REGIE_BUS_ID: False}
        assert state.control_connected is True


def test_concurrent_hellos_commit_owner_and_audio_epoch_atomically(
    server,
    monkeypatch,
):
    class _BlockingPreviousConnection:
        def __init__(self):
            self.close_started = threading.Event()
            self.allow_close = threading.Event()

        def close(self):
            self.close_started.set()
            assert self.allow_close.wait(timeout=2.0)

    class _HoldingConnection:
        def __init__(self, hello):
            self._hello = bytes(hello)
            self._delivered = False
            self._closed_event = threading.Event()
            self.sent = []
            self.closed = False
            self.welcomed = threading.Event()

        def settimeout(self, timeout):
            return None

        def recv(self, size):
            if not self._delivered:
                self._delivered = True
                return self._hello
            self._closed_event.wait(timeout=2.0)
            return b""

        def sendall(self, data):
            if self.closed:
                raise OSError("connection replaced")
            self.sent.append(bytes(data))
            self.welcomed.set()

        def close(self):
            self.closed = True
            self._closed_event.set()

    client_uuid = "simultaneous-hello-client"
    client_id = client_id_from_uuid(client_uuid)
    old_connection = _BlockingPreviousConnection()
    state = _client_state()
    state.client_uuid = client_uuid
    with server._lock:
        server._clients[client_id] = state
    with server._ctrl_lock:
        server._ctrl_sessions[client_id] = old_connection

    tokens = iter((11, 22))
    token_lock = threading.Lock()

    def next_token(bits):
        with token_lock:
            return next(tokens)

    monkeypatch.setattr(server_mod.secrets, "randbits", next_token)

    def hello(port):
        return encode_control_message(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": client_uuid,
                "udp_port": port,
            }
        )

    first = _HoldingConnection(hello(41001))
    second = _HoldingConnection(hello(41002))
    errors = []

    def run(connection, port):
        try:
            server._ctrl_session_loop(connection, ("127.0.0.1", port))
        except OSError:
            # A real replaced socket can fail its pending welcome write.
            pass
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first_thread = threading.Thread(target=run, args=(first, 50001), daemon=True)
    second_thread = threading.Thread(target=run, args=(second, 50002), daemon=True)
    first_thread.start()
    assert old_connection.close_started.wait(timeout=1.0)

    second_thread.start()
    assert second.welcomed.wait(timeout=1.0)
    old_connection.allow_close.set()
    first_thread.join(timeout=1.0)

    parser = JsonLineBuffer()
    second_messages = []
    for payload in second.sent:
        second_messages.extend(parser.feed(payload))
    second_token = parse_session_token(second_messages[0]["session_token"])

    assert not first_thread.is_alive()
    assert errors == []
    with server._ctrl_lock:
        assert server._ctrl_sessions[client_id] is second
    with server._lock:
        current = server._clients[client_id]
        assert current.session_token == second_token == 22
        assert current.addr == ("127.0.0.1", 41002)

    second.close()
    second_thread.join(timeout=1.0)
    assert not second_thread.is_alive()


def test_stop_joins_tracked_workers(server):
    server._running = True
    worker = server._new_worker("test-wait", lambda: server._stop.wait(10.0))
    worker.start()

    server.stop()

    assert not worker.is_alive()


def test_output_retry_worker_is_tracked_and_joined_by_stop(server):
    server._running = True
    server._ensure_output_retry_thread()
    with server._worker_lock:
        retry = server._worker_threads["output-retry"]

    assert retry.is_alive()

    server.stop()

    assert not retry.is_alive()


@pytest.mark.parametrize(
    "hello",
    [
        {"type": "hello", "client_id": 21, "udp_port": 41000},
        {
            "type": "hello",
            "version": 1,
            "client_id": 21,
            "udp_port": 41000,
        },
        {
            "type": "hello",
            "version": CONTROL_PROTOCOL_VERSION + 1,
            "client_id": 21,
            "udp_port": 41000,
        },
        {
            "type": "hello",
            "version": True,
            "client_id": 21,
            "udp_port": 41000,
        },
    ],
)
def test_control_rejects_missing_or_unsupported_version(server, hello):
    connection = _ScriptedConnection(encode_control_message(hello), b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert connection.closed
    assert connection.sent == []
    assert 21 not in server._clients


def test_control_closes_session_when_line_limit_is_exceeded(server):
    connection = _ScriptedConnection(b"x" * (MAX_CONTROL_LINE_BYTES + 1), b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert connection.closed
    assert connection.sent == []
    assert server._clients == {}


def test_control_rejects_non_boolean_hello_gain_ownership(server):
    client_uuid = "invalid-hello-boolean"
    client_id = client_id_from_uuid(client_uuid)
    connection = _ScriptedConnection(
        encode_control_message(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": client_uuid,
                "name": "Invalid",
                "udp_port": 41000,
                "applies_input_gain_locally": "false",
            }
        ),
        b"",
    )

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert connection.closed
    assert connection.sent == []
    assert client_id not in server._clients


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"listen_regie": "false"},
        {"return_gain_db": "3.0"},
        {"ptt_buses": {"0": "true"}},
        {"ptt_buses": {"99": True}},
    ],
)
def test_control_rejects_invalid_state_types_before_any_mutation(
    server,
    invalid_fields,
):
    client_uuid = f"invalid-state-{len(server._clients)}-{repr(invalid_fields)}"
    client_id = client_id_from_uuid(client_uuid)
    hello = encode_control_message(
        {
            "type": "hello",
            "version": CONTROL_PROTOCOL_VERSION,
            "client_id": client_id,
            "client_uuid": client_uuid,
            "name": "Strict state",
            "udp_port": 41000,
        }
    )
    state_message = {
        "type": "state",
        "input_gain_db": 3.0,
        "ptt_buses": {"0": True},
        **invalid_fields,
    }
    connection = _ScriptedConnection(
        hello + encode_control_message(state_message),
        b"",
    )

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    with server._lock:
        state = server._clients[client_id]
        profile = server._client_profiles[client_uuid]
        assert state.input_gain_db == pytest.approx(0.0)
        assert profile.input_gain_db == pytest.approx(0.0)
        assert state.listen_regie is True
        assert state.return_gain_db == pytest.approx(0.0)
        assert state.ptt_buses == {}
        assert state.control_connected is False


def test_control_parses_fragmented_hello_and_batched_state(server):
    client_uuid = "fragmented-client"
    client_id = client_id_from_uuid(client_uuid)
    hello = encode_control_message(
        {
            "type": "hello",
            "version": CONTROL_PROTOCOL_VERSION,
            "client_id": client_id,
            "client_uuid": client_uuid,
            "name": "Fragmented",
            "udp_port": 41000,
        }
    )
    state = encode_control_message(
        {
            "type": "state",
            "input_gain_db": 3.0,
            "ptt_buses": {"0": True},
        }
    )
    connection = _ScriptedConnection(hello[:8], hello[8:] + state, b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    with server._lock:
        client = server._clients[client_id]
        assert client.name == "Fragmented"
        assert client.input_gain_db == pytest.approx(3.0)
        assert not client.control_connected
    sent_parser = JsonLineBuffer()
    sent_messages = []
    for payload in connection.sent:
        sent_messages.extend(sent_parser.feed(payload))
    assert sent_messages[0]["type"] == "welcome"
    assert sent_messages[0]["version"] == CONTROL_PROTOCOL_VERSION
    assert len(sent_messages[0]["session_token"]) == 16
    assert parse_session_token(sent_messages[0]["session_token"]) > 0
    assert sent_messages[1]["type"] == "update"
    assert client.session_token == 0


def test_each_hello_gets_a_new_session_token(server, monkeypatch):
    tokens = iter((1, 2))
    monkeypatch.setattr(server_mod.secrets, "randbits", lambda bits: next(tokens))

    received_tokens = []
    client_uuid = "same-client"
    client_id = client_id_from_uuid(client_uuid)
    for port in (41000, 41001):
        hello = encode_control_message(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": client_uuid,
                "udp_port": port,
            }
        )
        connection = _ScriptedConnection(hello, b"")
        server._ctrl_session_loop(connection, ("127.0.0.1", 50000))
        parser = JsonLineBuffer()
        messages = []
        for payload in connection.sent:
            messages.extend(parser.feed(payload))
        received_tokens.append(parse_session_token(messages[0]["session_token"]))

    assert received_tokens == [1, 2]


def test_hello_rejects_client_id_unrelated_to_uuid(server):
    client_uuid = "strict-identity"
    hello = encode_control_message(
        {
            "type": "hello",
            "version": CONTROL_PROTOCOL_VERSION,
            "client_id": (client_id_from_uuid(client_uuid) + 1) & 0xFFFFFFFF,
            "client_uuid": client_uuid,
            "udp_port": 41000,
        }
    )
    connection = _ScriptedConnection(hello, b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert server._clients == {}
    assert server._ctrl_sessions == {}
    assert server.get_stats_snapshot()["control_invalid_identities"] == 1


def test_hello_rejects_crc_collision_instead_of_replacing_profile(server):
    first_uuid = "CbqlANX5SvlhQR9k"
    second_uuid = "S2gCFl89L6spbARQ"
    client_id = client_id_from_uuid(first_uuid)
    assert client_id == client_id_from_uuid(second_uuid)

    first = _ScriptedConnection(
        encode_control_message(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": first_uuid,
                "udp_port": 41000,
            }
        ),
        b"",
    )
    server._ctrl_session_loop(first, ("127.0.0.1", 50000))

    second = _ScriptedConnection(
        encode_control_message(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": second_uuid,
                "udp_port": 41001,
            }
        ),
        b"",
    )
    server._ctrl_session_loop(second, ("127.0.0.1", 50001))

    assert first_uuid in server._client_profiles
    assert second_uuid not in server._client_profiles
    assert server._clients[client_id].client_uuid == first_uuid
    assert second.sent == []
    assert server.get_stats_snapshot()["control_identity_collisions"] == 1


def test_codec_epoch_failure_does_not_revoke_working_session(server, monkeypatch):
    client_uuid = "working-session"
    client_id = client_id_from_uuid(client_uuid)
    state = _client_state(addr=("127.0.0.1", 41000))
    state.client_uuid = client_uuid
    state.control_connected = True
    old_connection = _ScriptedConnection()
    with server._lock:
        server._clients[client_id] = state
    with server._ctrl_lock:
        server._ctrl_sessions[client_id] = old_connection

    class _CodecInitFailure:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("codec unavailable")

    monkeypatch.setattr(server_mod, "OpusDecoder", _CodecInitFailure)
    replacement = _ScriptedConnection(
        encode_control_message(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": client_uuid,
                "udp_port": 41001,
            }
        ),
        b"",
    )

    server._ctrl_session_loop(replacement, ("127.0.0.1", 50001))

    assert server._ctrl_sessions[client_id] is old_connection
    assert state.control_connected is True
    assert state.session_token == SESSION_TOKEN
    assert server.get_stats_snapshot()["control_codec_init_errors"] == 1


def test_control_session_times_out_without_hello(server, monkeypatch):
    monkeypatch.setattr(server_mod, "CONTROL_HELLO_TIMEOUT_S", 0.01)

    class _SilentConnection(_ScriptedConnection):
        def recv(self, size):
            time.sleep(0.006)
            raise server_mod.socket.timeout()

    connection = _SilentConnection()
    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert connection.closed
    assert server._ctrl_hello_timeouts == 1


def test_server_udp_loop_rejects_legacy_datagram_and_counts_it(server):
    receiver = threading.Thread(target=server._rx_loop, daemon=True)
    receiver.start()
    sender = server_mod.socket.socket(server_mod.socket.AF_INET, server_mod.socket.SOCK_DGRAM)
    try:
        destination = server._sock.getsockname()
        sender.sendto(_legacy_audio_packet(7, 1, 1, b"legacy"), destination)
        deadline = time.monotonic() + 1.0
        while server._rx_invalid_packets == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server._rx_invalid_packets == 1
        assert server._rx_packets == 0
    finally:
        sender.close()
        server._stop.set()
        receiver.join(timeout=1.0)


def test_server_broadcast_uses_pure_mix_minus_return_and_v2_envelope(
    server,
    monkeypatch,
):
    now = time.monotonic()
    state = _client_state(now=now)
    state.listen_regie = True
    state.listen_return_bus = True
    state.return_gain_db = float(20.0 * np.log10(2.0))
    with server._lock:
        server._clients[7] = state

    class _RecordingSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((bytes(data), addr))
            server._stop.set()

        def close(self):
            pass

    server._sock.close()
    recording_socket = _RecordingSocket()
    server._sock = recording_socket
    routed = server_mod.route_sources(
        [
            RoutedSource(7, np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32), 1),
            RoutedSource(8, np.full((FRAME_SAMPLES,), 0.50, dtype=np.float32), 1),
        ]
    )
    server._mix_queue.put_nowait(
        server_mod._RoutingEnvelope(
            routing_epoch=server._routing_epoch,
            result=routed,
        )
    )
    server._return_enabled = True
    server._return_frames.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))

    real_build_destination_mix = server_mod.build_destination_mix
    destination_calls = []

    def destination_spy(*args, **kwargs):
        destination_calls.append((args, kwargs))
        return real_build_destination_mix(*args, **kwargs)

    monkeypatch.setattr(server_mod, "build_destination_mix", destination_spy)

    broadcaster = threading.Thread(target=server._broadcast_loop, daemon=True)
    broadcaster.start()
    broadcaster.join(timeout=1.0)

    assert not broadcaster.is_alive()
    assert len(recording_socket.sent) == 1
    assert len(destination_calls) == 1
    args, kwargs = destination_calls[0]
    np.testing.assert_allclose(args[0], 0.75, atol=1e-6)
    np.testing.assert_allclose(args[1], 0.25, atol=1e-6)
    assert kwargs["listen_regie"] is True
    assert kwargs["listen_return"] is True
    np.testing.assert_allclose(state.encoder.frames[-1], 1.0, atol=1e-6)

    packet = unpack_audio_packet_v2(recording_socket.sent[0][0])
    assert packet.client_id == 7
    assert packet.session_token == SESSION_TOKEN
    assert packet.direction is AudioPacketDirection.SERVER_TO_CLIENT
    assert packet.bus_mask == 0


def test_broadcast_epoch_check_is_atomic_with_udp_send(server, monkeypatch):
    now = time.monotonic()
    state = _client_state(now=now)
    with server._lock:
        server._clients[7] = state

    class _RecordingSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((bytes(data), addr))

        def close(self):
            return None

    server._sock.close()
    recording_socket = _RecordingSocket()
    server._sock = recording_socket
    routed = server_mod.route_sources(
        [RoutedSource(8, np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32), 1)]
    )
    old_epoch = server._routing_epoch
    server._mix_queue.put_nowait(server_mod._RoutingEnvelope(old_epoch, routed))
    real_pack = server_mod.pack_audio_packet_v2

    def revoke_during_packet_build(**kwargs):
        with server._lock:
            server._advance_routing_epoch_locked()
        server._stop.set()
        return real_pack(**kwargs)

    monkeypatch.setattr(server_mod, "pack_audio_packet_v2", revoke_during_packet_build)

    server._broadcast_loop()

    assert recording_socket.sent == []
    assert server._routing_epoch != old_epoch
    assert server._stale_mix_epochs_dropped == 1


def test_engine_snapshots_expose_output_and_return_stream_health(server):
    class _ActiveStream:
        active = True

    output = server_mod.OutputState(output_id=4, device=2, bus_id=1)
    output.stream = _ActiveStream()
    with server._lock:
        server._outputs[4] = output
    with server._return_lock:
        server._return_stream = _ActiveStream()
        server._return_stream_error = ""

    output_snapshot = server.get_outputs_snapshot()[4]
    stats = server.get_stats_snapshot()

    assert output_snapshot["stream_active"] is True
    assert output_snapshot["stream_error"] == ""
    assert stats["return_stream_active"] is True
    assert stats["return_stream_error"] == ""


def test_server_bus_state_is_backed_by_fixed_catalog(server):
    snapshot = server.get_buses_snapshot()

    assert set(snapshot) == set(FIXED_BUS_IDS)
    assert [snapshot[definition.bus_id]["name"] for definition in FIXED_BUSES] == [
        definition.name for definition in FIXED_BUSES
    ]
    assert all(not snapshot[definition.bus_id]["feed_to_regie"] for definition in FIXED_BUSES)
    assert not hasattr(server, "_next_bus_id")

    with pytest.raises(ValueError, match="unknown bus"):
        server.create_output(device=0, bus_id=99)
