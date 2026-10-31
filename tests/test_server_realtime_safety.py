import json
import queue
import struct
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import py_intercom.common.opus_codec as opus_codec_mod
import py_intercom.server.control as control_mod
import py_intercom.server.mixer as mixer_mod
import py_intercom.server.models as models_mod
import py_intercom.server.server as server_mod
from py_intercom.common.buses import (
    FIXED_BUS_IDS,
    FIXED_BUSES,
    PLATEAU_BUS_ID,
    REGIE_BUS_ID,
    VMIX_BUS_ID,
)
from py_intercom.common.constants import (
    CTRL_LIVENESS_TIMEOUT_S,
    FRAME_SAMPLES,
    JB_MAX_CONCEALED_FRAMES,
    JB_START_FRAMES,
)
from py_intercom.common.control_protocol import (
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_LINE_BYTES,
    JsonLineBuffer,
    encode_control_message,
    parse_session_token,
)
from py_intercom.common.identity import client_id_from_uuid
from py_intercom.common.jitter_buffer import (
    JitterFrame,
    JitterFrameMetadata,
    OpusPacketJitterBuffer,
    new_realtime_jitter_buffer,
)
from py_intercom.common.packets import (
    AudioPacketDirection,
    pack_audio_packet_v2,
    unpack_audio_packet_v2,
)
from py_intercom.common.routing import RoutedSource, RoutingResult

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
    def __init__(self, bus_mask, *, token=SESSION_TOKEN):
        self._frame = JitterFrame(
            sequence_number=1,
            payload=b"opus",
            metadata=JitterFrameMetadata(
                session_token=int(token),
                bus_mask=int(bus_mask),
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
                bus_mask=1,
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
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", _FakeDecoder)
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


def _sent_messages(connection):
    return JsonLineBuffer().feed(b"".join(connection.sent))


def _assert_refused_with_error(connection, *, retry: bool):
    assert connection.closed
    messages = _sent_messages(connection)
    assert [message["type"] for message in messages] == ["error"]
    # Only what the station cannot fix by reconnecting stops its retries.
    assert messages[0]["retry"] is retry


def _client_state(*, addr=("127.0.0.1", 41000), now=None, decoder=None):
    current = time.monotonic() if now is None else float(now)
    return models_mod.ClientState(
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
    assert (
        server_mod.IntercomServer._live_frame_bus_mask(
            1,
            True,
            now - CTRL_LIVENESS_TIMEOUT_S - 0.01,
            now=now,
        )
        == 0
    )
    assert server_mod.IntercomServer._live_frame_bus_mask(1 << 1, True, now, now=now) == 1 << 1
    assert server_mod.IntercomServer._live_frame_bus_mask(1 << 0, True, now, now=now) == 1 << 0
    assert server_mod.IntercomServer._live_frame_bus_mask(1 << 15, True, now, now=now) == 0


def test_mix_loop_delegates_fixed_bus_routing_to_pure_engine(server, monkeypatch):
    now = time.monotonic()
    double_db = 20.0 * np.log10(2.0)
    quadruple_db = 20.0 * np.log10(4.0)

    direct = _client_state(now=now, decoder=_ValueDecoder(0.10))
    direct.jb = _OneFrameJitter(1 << REGIE_BUS_ID)

    multi_ptt = _client_state(now=now, decoder=_ValueDecoder(0.20))
    multi_ptt.jb = _OneFrameJitter((1 << PLATEAU_BUS_ID) | (1 << VMIX_BUS_ID))
    multi_ptt.input_gain_db = float(double_db)
    multi_ptt.applies_input_gain_locally = False

    stale = _client_state(now=now, decoder=_ValueDecoder(0.50))
    stale.jb = _OneFrameJitter(1 << REGIE_BUS_ID)
    stale.last_control_monotonic = now - CTRL_LIVENESS_TIMEOUT_S - 1.0

    with server._lock:
        server._clients.update({1: direct, 2: multi_ptt, 3: stale})
        server._buses[PLATEAU_BUS_ID].gain_db = float(double_db)
        server._buses[PLATEAU_BUS_ID].feed_to_regie = True
        server._buses[VMIX_BUS_ID].gain_db = float(quadruple_db)
        server._buses[VMIX_BUS_ID].feed_to_regie = True
        server._outputs.update(
            {
                10: models_mod.OutputState(10, 0, REGIE_BUS_ID),
                11: models_mod.OutputState(11, 0, PLATEAU_BUS_ID),
                12: models_mod.OutputState(12, 0, VMIX_BUS_ID),
            }
        )

    real_route_sources = mixer_mod.route_sources
    real_build_destination_mix = mixer_mod.build_destination_mix
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

    monkeypatch.setattr(mixer_mod, "route_sources", route_spy)
    monkeypatch.setattr(mixer_mod, "build_destination_mix", destination_spy)

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

    result = server._mix_queue.get_nowait()
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
    # VMix stays linear at 1.60 in its ring: only the output callback limits
    # it, after the output gain.
    assert server._outputs[12].ring.pop_into(output_frame) is not None
    np.testing.assert_allclose(output_frame, 1.60, atol=1e-6)


def test_mix_queue_drops_oldest_frame_when_full(server):
    server._mix_queue = queue.Queue(maxsize=2)
    server._mix_queue.put_nowait("oldest")
    server._mix_queue.put_nowait("middle")

    server._enqueue_latest_mix("newest")

    assert server._mix_queue.get_nowait() == "middle"
    assert server._mix_queue.get_nowait() == "newest"
    assert server._mix_queue_drops == 1


def test_routing_epoch_keeps_mixed_audio_for_the_other_talkers(server):
    # A mixed frame sums every talker: dropping it when one session is revoked
    # would cut all the others (Plateau, VMix and every headset) for 10-30 ms.
    output = models_mod.OutputState(
        output_id=3,
        device=0,
        bus_id=PLATEAU_BUS_ID,
    )
    output.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))
    routed = mixer_mod.route_sources([])
    old_epoch = server._routing_epoch
    server._mix_queue.put_nowait(routed)

    with server._lock:
        server._outputs[output.output_id] = output
        new_epoch = server._advance_routing_epoch_locked()

    assert new_epoch != old_epoch
    assert server._mix_queue.get_nowait() is routed
    scratch = np.empty((FRAME_SAMPLES,), dtype=np.float32)
    assert output.ring.pop_into(scratch) is not None


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
                    bus_mask=1 << REGIE_BUS_ID,
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


def test_forget_client_atomically_revokes_session(server):
    client_uuid = "forgotten-live-client"
    client_id = client_id_from_uuid(client_uuid)
    state = _client_state()
    state.client_uuid = client_uuid
    connection = _ScriptedConnection()
    routed = mixer_mod.route_sources(
        [
            RoutedSource(
                client_id,
                np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32),
                1 << REGIE_BUS_ID,
            )
        ]
    )
    output = models_mod.OutputState(8, 0, PLATEAU_BUS_ID)
    output.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))
    with server._lock:
        server._clients[client_id] = state
        server._client_profiles[client_uuid] = models_mod.ClientProfile(client_uuid)
        server._outputs[output.output_id] = output
        old_epoch = server._routing_epoch
        server._mix_queue.put_nowait(routed)
    with server._ctrl_lock:
        server._ctrl_sessions[client_id] = connection
        server._ctrl_send_locks[client_id] = threading.Lock()

    server.forget_client(client_id)

    assert server._routing_epoch != old_epoch
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
    state = models_mod.OutputState(
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
    state = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
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

    assert not server._accept_udp_packet(_client_packet(7, token=SESSION_TOKEN + 1), state.addr, now=now)
    assert server._rx_invalid_tokens == 1
    assert state.jb.buffered_frames == 0

    assert not server._accept_udp_packet(
        _client_packet(7, direction=AudioPacketDirection.SERVER_TO_CLIENT),
        state.addr,
        now=now,
    )
    assert server._rx_invalid_directions == 1

    assert not server._accept_udp_packet(_client_packet(7, bus_mask=1 << 15), state.addr, now=now)
    assert server._rx_invalid_route_masks == 1
    assert state.jb.buffered_frames == 0


def test_routing_metadata_stays_with_frame_when_tcp_ptt_changes(server):
    now = time.monotonic()
    state = _client_state(now=now)
    with server._lock:
        server._clients[7] = state

    assert server._accept_udp_packet(_client_packet(7, bus_mask=1 << 0), state.addr, now=now)
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
    old_state = models_mod.OutputState(5, 1, PLATEAU_BUS_ID, gain_db=-3.0)
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
    old_state = models_mod.OutputState(6, 1, VMIX_BUS_ID)
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

    message = (
        json.dumps(
            {
                "type": "state",
                "client_id": 9,
                "ptt_buses": {"0": True},
            }
        ).encode("utf-8")
        + b"\n"
    )
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

    hello = (
        json.dumps(
            {
                "type": "hello",
                "version": CONTROL_PROTOCOL_VERSION,
                "client_id": client_id,
                "client_uuid": client_uuid,
                "name": "Test",
                "udp_port": 41000,
            }
        ).encode("utf-8")
        + b"\n"
    )
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


def test_stream_supervisor_is_tracked_and_joined_by_stop(server):
    server._running = True
    server._start_stream_supervisor()
    with server._worker_lock:
        supervisor = server._worker_threads["stream-supervisor"]

    assert supervisor.is_alive()

    server.stop()

    assert not supervisor.is_alive()


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

    _assert_refused_with_error(connection, retry=False)
    assert "Version" in _sent_messages(connection)[0]["message"]
    assert 21 not in server._clients


def test_control_closes_session_when_line_limit_is_exceeded(server):
    connection = _ScriptedConnection(b"x" * (MAX_CONTROL_LINE_BYTES + 1), b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    _assert_refused_with_error(connection, retry=True)
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

    _assert_refused_with_error(connection, retry=True)
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
    _assert_refused_with_error(second, retry=False)
    # The refused peer learns why, but never the UUID it collides with.
    refusal = _sent_messages(second)[0]["message"]
    assert "même identité" in refusal
    assert first_uuid not in refusal
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

    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", _CodecInitFailure)
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
    monkeypatch.setattr(control_mod, "CONTROL_HELLO_TIMEOUT_S", 0.01)

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
    routed = mixer_mod.route_sources(
        [
            RoutedSource(7, np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32), 1),
            RoutedSource(8, np.full((FRAME_SAMPLES,), 0.50, dtype=np.float32), 1),
        ]
    )
    server._mix_queue.put_nowait(routed)
    server._return_enabled = True
    server._return_frames.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))

    real_build_destination_mix = mixer_mod.build_destination_mix
    destination_calls = []

    def destination_spy(*args, **kwargs):
        destination_calls.append((args, kwargs))
        return real_build_destination_mix(*args, **kwargs)

    monkeypatch.setattr(mixer_mod, "build_destination_mix", destination_spy)

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
    assert packet.flags == AudioPacketDirection.SERVER_TO_CLIENT
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
    routed = mixer_mod.route_sources([RoutedSource(8, np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32), 1)])
    old_epoch = server._routing_epoch
    server._mix_queue.put_nowait(routed)
    real_pack = mixer_mod.pack_audio_packet_v2

    def revoke_during_packet_build(**kwargs):
        with server._lock:
            server._advance_routing_epoch_locked()
        server._stop.set()
        return real_pack(**kwargs)

    monkeypatch.setattr(mixer_mod, "pack_audio_packet_v2", revoke_during_packet_build)

    server._broadcast_loop()

    assert recording_socket.sent == []
    assert server._routing_epoch != old_epoch
    assert server._stale_mix_epochs_dropped == 1


def test_engine_snapshots_expose_output_and_return_stream_health(server):
    class _ActiveStream:
        active = True

    output = models_mod.OutputState(output_id=4, device=2, bus_id=1)
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


# --- Regression tests for the server engine audit (zone B) -----------------


class _ScriptedJitter:
    def __init__(self, frames):
        self._frames = list(frames)

    def pop_frame(self):
        return self._frames.pop(0) if self._frames else None

    def reset(self):
        self._frames.clear()


class _ActiveStream:
    active = True


def _real_frame(sequence_number, bus_mask, *, token=SESSION_TOKEN):
    return JitterFrame(
        sequence_number=sequence_number,
        payload=b"opus",
        metadata=JitterFrameMetadata(
            session_token=int(token),
            bus_mask=int(bus_mask),
        ),
    )


def _concealed_frame(sequence_number):
    return JitterFrame(sequence_number=sequence_number, payload=b"", concealed=True)


def _run_mix_ticks(server, monkeypatch, ticks):
    real_route_sources = mixer_mod.route_sources
    calls = []

    def route_spy(sources, config):
        calls.append(list(sources))
        if len(calls) >= ticks:
            server._stop.set()
        return real_route_sources(sources, config)

    monkeypatch.setattr(mixer_mod, "route_sources", route_spy)
    server._mix_loop()
    return calls


def _hello(client_uuid, *, udp_port=41000):
    return encode_control_message(
        {
            "type": "hello",
            "version": CONTROL_PROTOCOL_VERSION,
            "client_id": client_id_from_uuid(client_uuid),
            "client_uuid": client_uuid,
            "udp_port": udp_port,
        }
    )


def test_datagrams_from_an_expired_client_revoke_its_session_only_once(server):
    now = time.monotonic()
    state = _client_state(now=now)
    output = models_mod.OutputState(9, 0, PLATEAU_BUS_ID)
    with server._lock:
        server._clients[7] = state
        server._outputs[9] = output
    expired_at = now + CTRL_LIVENESS_TIMEOUT_S + 0.01
    packet = _client_packet(7)

    assert not server._accept_udp_packet(packet, state.addr, now=expired_at)
    epoch_after_revocation = server._routing_epoch
    resets_after_revocation = state.jb.stats.resets
    output.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))

    # A Wi-Fi client keeps sending ~100 datagrams/s after its TCP session
    # expired: none of them may flush the outputs of everybody else again.
    for _ in range(10):
        assert not server._accept_udp_packet(packet, state.addr, now=expired_at)

    assert server._routing_epoch == epoch_after_revocation
    assert state.jb.stats.resets == resets_after_revocation
    assert output.ring.depth == 1
    assert server._rx_inactive_clients == 11


def test_a_hello_recorded_after_the_clock_read_is_not_expired(server):
    # The broadcast loop reads the clock, then waits for the engine lock while
    # a hello installs a fresh session: the session is newer than "now".
    before_hello = time.monotonic()
    state = _client_state(now=before_hello + 0.05)
    with server._lock:
        server._clients[7] = state

    destinations = server._active_broadcast_destinations(now=before_hello)

    assert [entry[0] for entry in destinations] == [7]
    assert state.session_token == SESSION_TOKEN


def test_expiring_an_already_expired_session_does_not_advance_the_epoch(server):
    state = _client_state()
    with server._lock:
        server._clients[7] = state
        server._expire_client_session(state)
        epoch = server._routing_epoch
        server._expire_client_session(state)

    assert server._routing_epoch == epoch


def test_concealed_frame_keeps_the_route_of_the_frame_it_continues(server, monkeypatch):
    now = time.monotonic()
    state = _client_state(now=now, decoder=_ValueDecoder(0.25))
    state.jb = _ScriptedJitter([_real_frame(1, 1 << PLATEAU_BUS_ID), _concealed_frame(2)])
    output = models_mod.OutputState(9, 0, PLATEAU_BUS_ID)
    with server._lock:
        server._clients[7] = state
        server._outputs[9] = output

    calls = _run_mix_ticks(server, monkeypatch, 2)

    # The Deep PLC frame is played on Plateau instead of a 10 ms hole.
    assert [[source.bus_mask for source in sources] for sources in calls] == [
        [1 << PLATEAU_BUS_ID],
        [1 << PLATEAU_BUS_ID],
    ]
    frame = np.empty((FRAME_SAMPLES,), dtype=np.float32)
    for _ in range(2):
        assert output.ring.pop_into(frame) is not None
        np.testing.assert_allclose(frame, 0.25, atol=1e-6)


@pytest.mark.parametrize(
    "remembered_route",
    [
        (0, 0),  # concealment before any real frame of the session
        (SESSION_TOKEN + 1, 1 << REGIE_BUS_ID),  # route learnt in another session
    ],
)
def test_concealed_frame_never_opens_a_route_of_its_own(server, monkeypatch, remembered_route):
    state = _client_state(decoder=_ValueDecoder(0.25))
    state.jb = _ScriptedJitter([_concealed_frame(1)])
    state.last_frame_route = remembered_route
    with server._lock:
        server._clients[7] = state

    calls = _run_mix_ticks(server, monkeypatch, 1)

    assert [source.bus_mask for source in calls[0]] == [0]


def test_client_meter_falls_without_frames_even_while_talking(server, monkeypatch):
    state = _client_state()
    state.ptt_buses = {REGIE_BUS_ID: True}
    state.vu_dbfs = -12.0
    state.jb = _ScriptedJitter([])
    with server._lock:
        server._clients[7] = state

    _run_mix_ticks(server, monkeypatch, 2)

    # The meter releases (3 dB per 10 ms frame) instead of freezing at -12.
    assert state.vu_dbfs == pytest.approx(-18.0)


def test_expired_client_is_shown_offline_with_a_silent_meter(server):
    state = _client_state()
    state.vu_dbfs = -12.0
    with server._lock:
        server._clients[7] = state
        server._expire_client_session(state)

    snapshot = server.get_clients_snapshot()[7]

    assert snapshot["control_connected"] is False
    assert snapshot["vu_dbfs"] == -60.0
    assert snapshot["addr"] is None


def test_output_gain_is_applied_before_the_final_limiter(server):
    double_db = float(20.0 * np.log10(2.0))
    ramp = np.linspace(-0.75, 0.75, FRAME_SAMPLES, dtype=np.float32)
    state = models_mod.OutputState(3, 0, PLATEAU_BUS_ID, gain_db=double_db)
    state.ring.push(ramp)
    outdata = np.empty((FRAME_SAMPLES, 1), dtype=np.float32)

    server._output_state_callback(state, outdata, FRAME_SAMPLES, None, None)

    # +6 dB takes the bus to 1.5 full scale: the limiter scales the whole frame
    # back to 0.99 instead of hard-clipping every sample above 1.0.
    np.testing.assert_allclose(outdata[:, 0], ramp * 2.0 * (0.99 / 1.5), atol=1e-6)


def test_output_attenuation_applies_to_the_linear_bus_sum(server):
    state = models_mod.OutputState(3, 0, PLATEAU_BUS_ID, gain_db=float(20.0 * np.log10(0.5)))
    state.ring.push(np.full((FRAME_SAMPLES,), 1.2, dtype=np.float32))
    outdata = np.empty((FRAME_SAMPLES, 1), dtype=np.float32)

    server._output_state_callback(state, outdata, FRAME_SAMPLES, None, None)

    np.testing.assert_allclose(outdata[:, 0], 0.6, atol=1e-6)


def test_audio_callbacks_count_statuses_instead_of_logging(server, monkeypatch):
    logged = []
    monkeypatch.setattr(server_mod.logger, "debug", lambda *args, **kwargs: logged.append(args))
    state = models_mod.OutputState(3, 0, PLATEAU_BUS_ID)
    outdata = np.empty((FRAME_SAMPLES, 1), dtype=np.float32)
    server._output_state_callback(state, outdata, FRAME_SAMPLES, None, "output underflow")

    server._return_enabled = True
    gate = threading.Event()
    gate.set()
    server._return_in_callback(
        np.zeros((FRAME_SAMPLES - 1, 1), dtype=np.float32),
        FRAME_SAMPLES - 1,
        None,
        "input overflow",
        active_gate=gate,
    )

    assert logged == []
    assert state.callback_statuses == 1
    assert server._return_callback_statuses == 1
    assert server._return_frame_mismatches == 1
    stats = server.get_stats_snapshot()
    assert stats["return_callback_statuses"] == 1
    assert stats["return_frame_mismatches"] == 1


def test_return_device_switch_stops_old_stream_outside_engine_lock(server, monkeypatch):
    lock_free_during_stop = []

    class _LockProbeStream(_TrackedAudioStream):
        def stop(self):
            acquired = server._lock.acquire(blocking=False)
            if acquired:
                server._lock.release()
            lock_free_during_stop.append(acquired)
            super().stop()

    events = []
    old_gate = threading.Event()
    old_gate.set()
    server._running = True
    server._return_enabled = True
    server._return_input_device = 1
    server._return_stream = _LockProbeStream(events, "old", active=True)
    server._return_stream_gate = old_gate
    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_input_settings", lambda **kwargs: None)
    monkeypatch.setattr(
        server_mod.sd,
        "InputStream",
        lambda **kwargs: _TrackedAudioStream(events, "candidate", callback=kwargs["callback"]),
    )
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)

    server.set_return_input_device(2)

    # RX, mix and broadcast need this lock every 10 ms: a slow driver stop
    # must never hold them.
    assert lock_free_during_stop == [True]
    assert events[:3] == ["candidate-start", "old-stop", "old-close"]


def test_failed_forced_reopen_of_a_working_output_raises_no_false_alarm(server, monkeypatch):
    events = []
    working = _TrackedAudioStream(events, "working", active=True)
    state = models_mod.OutputState(6, 1, VMIX_BUS_ID)
    state.stream = working
    state.last_callback_monotonic = time.monotonic()
    with server._lock:
        server._outputs[6] = state
    server._running = True
    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_output_settings", lambda **kwargs: None)
    monkeypatch.setattr(
        server_mod.sd,
        "OutputStream",
        lambda **kwargs: _TrackedAudioStream(events, "candidate", fail_on_start=True),
    )

    server.reopen_outputs(force=True)

    assert server._outputs[6] is state
    assert state.stream is working and working.active
    assert state.stream_error == ""
    assert server.get_outputs_snapshot()[6]["stream_error"] == ""


def test_dead_output_is_closed_before_it_is_reopened(server, monkeypatch):
    events = []
    candidates = []
    state = models_mod.OutputState(6, 1, VMIX_BUS_ID)
    state.stream = _TrackedAudioStream(events, "dead", active=False)
    with server._lock:
        server._outputs[6] = state
    server._running = True
    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_output_settings", lambda **kwargs: None)

    def output_stream_factory(**kwargs):
        stream = _TrackedAudioStream(events, "candidate", callback=kwargs["callback"])
        candidates.append(stream)
        return stream

    monkeypatch.setattr(server_mod.sd, "OutputStream", output_stream_factory)

    server.reopen_outputs()

    # Exclusive drivers (ASIO, WDM-KS) refuse a second handle on the device.
    assert events[:3] == ["dead-stop", "dead-close", "candidate-start"]
    assert server._outputs[6] is state
    assert state.stream is candidates[0]


def test_supervisor_retries_a_dead_output_forever_with_capped_backoff(server, monkeypatch):
    server._running = True
    output = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    with server._lock:
        server._outputs[4] = output
    clock = {"now": 0.0}
    attempts = []

    def failing_open(out):
        attempts.append(clock["now"])
        raise RuntimeError("device gone")

    monkeypatch.setattr(server, "_open_output_stream", failing_open)
    backoff = {}
    start = time.monotonic()
    for second in range(30):
        clock["now"] = float(second)
        server._supervise_streams_once(backoff, clock=lambda: start + clock["now"])

    assert attempts == [0.0, 1.0, 3.0, 8.0, 13.0, 18.0, 23.0, 28.0]

    def working_open(out):
        with out.lock:
            out.stream = _ActiveStream()
            out.last_callback_monotonic = time.monotonic()

    monkeypatch.setattr(server, "_open_output_stream", working_open)
    server._supervise_streams_once(backoff, clock=lambda: start + 33.0)

    assert isinstance(output.stream, _ActiveStream)
    assert backoff == {}


def test_supervisor_leaves_healthy_streams_alone(server, monkeypatch):
    server._running = True
    output = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    output.stream = _ActiveStream()
    now = time.monotonic()
    output.last_callback_monotonic = now
    with server._lock:
        server._outputs[4] = output
    monkeypatch.setattr(
        server,
        "_open_output_stream",
        lambda out: pytest.fail("a healthy output must not be reopened"),
    )

    server._supervise_streams_once({}, clock=lambda: now + 0.5)


def test_supervisor_reopens_a_dead_or_stalled_return_input(server, monkeypatch):
    events = []
    opened = []
    server._running = True
    server._return_enabled = True
    server._return_input_device = 3
    monkeypatch.setattr(server, "_open_return_input_stream", lambda device: opened.append(device))

    server._supervise_streams_once({})
    assert opened == [3]

    # Still "active" for PortAudio, but no callback for two seconds.
    with server._return_lock:
        server._return_stream = _TrackedAudioStream(events, "stalled", active=True)
        server._return_last_callback_monotonic = time.monotonic() - 2.0
    server._supervise_streams_once({})
    assert opened == [3, 3]
    assert events == ["stalled-stop", "stalled-close"]

    server._return_enabled = False
    server._supervise_streams_once({})
    assert opened == [3, 3]


def test_supervisor_tick_cannot_open_an_output_being_created(server, monkeypatch):
    events = []
    streams = []
    server._running = True
    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_output_settings", lambda **kwargs: None)

    def output_stream_factory(**kwargs):
        stream = _TrackedAudioStream(events, "stream", callback=kwargs["callback"])
        streams.append(stream)
        return stream

    monkeypatch.setattr(server_mod.sd, "OutputStream", output_stream_factory)
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)
    engine_lock = server._lock
    ticks = []

    class _TickAfterPublication:
        # A supervisor tick lands as soon as the new output is visible,
        # before create_output has opened its stream.
        def __enter__(self):
            engine_lock.acquire()

        def __exit__(self, *exc_info):
            engine_lock.release()
            if not ticks and server._outputs:
                ticks.append(True)
                server._supervise_streams_once({})

    monkeypatch.setattr(server, "_lock", _TickAfterPublication())

    output_id = server.create_output(1, PLATEAU_BUS_ID)

    # A second stream would drain the same ring and never be closed.
    assert ticks == [True]
    assert len(streams) == 1
    assert server._outputs[output_id].stream is streams[0]


def test_supervisor_never_reopens_an_output_replaced_meanwhile(server, monkeypatch):
    server._running = True
    # The tick saw the old state, now closed by the device swap.
    replaced = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    current = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    current.stream = _ActiveStream()
    current.last_callback_monotonic = time.monotonic()
    with server._lock:
        server._outputs[4] = current
    monkeypatch.setattr(
        server,
        "_open_output_stream",
        lambda out: pytest.fail("a replaced output must not open a second device handle"),
    )

    assert server._reopen_output_in_place(replaced, blocking=False) is False
    assert current.stream is not None


def test_supervisor_never_closes_a_return_stream_committed_meanwhile(server, monkeypatch):
    events = []
    healthy = _TrackedAudioStream(events, "healthy", active=True)
    server._running = True
    server._return_enabled = True
    server._return_input_device = 1
    server._return_stream = _TrackedAudioStream(events, "dead", active=False)
    check_return_health = server._return_stream_needs_reopen

    def operator_commits_after_the_check(*, now):
        needed = check_return_health(now=now)
        # The operator selects the Return device while the supervisor decides.
        server._close_stream(server._commit_return_input_stream(healthy, threading.Event()))
        return needed

    monkeypatch.setattr(server, "_return_stream_needs_reopen", operator_commits_after_the_check)
    monkeypatch.setattr(
        server,
        "_open_return_input_stream",
        lambda device: pytest.fail("a healthy Return must not be reopened"),
    )

    assert server._reopen_return_in_place() is False
    assert server._return_stream is healthy
    assert healthy.active and not healthy.stopped


def test_return_disabled_during_a_supervisor_reopen_stays_closed(server, monkeypatch):
    events = []
    late = []
    server._running = True
    server._return_enabled = True
    server._return_input_device = 1
    monkeypatch.setattr(server_mod.sd, "query_devices", lambda device: {"name": "fake"})
    monkeypatch.setattr(server_mod.sd, "check_input_settings", lambda **kwargs: None)
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)

    def input_stream_factory(**kwargs):
        # The operator unticks Return while the device is opening.
        server.set_return_enabled(False)
        stream = _TrackedAudioStream(events, "late", callback=kwargs["callback"])
        late.append(stream)
        return stream

    monkeypatch.setattr(server_mod.sd, "InputStream", input_stream_factory)

    with pytest.raises(RuntimeError, match="cancelled"):
        server._reopen_return_in_place()

    assert server._return_stream is None
    assert late[0].closed
    assert not server._return_stream_needs_reopen(now=time.monotonic())


def test_supervisor_logs_a_lasting_failure_once_per_window(server, monkeypatch):
    server._running = True
    with server._lock:
        server._outputs[4] = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    warnings = []
    monkeypatch.setattr(server_mod.logger, "warning", lambda *args, **kwargs: warnings.append(args))

    def failing_open(out):
        raise RuntimeError("device gone")

    monkeypatch.setattr(server, "_open_output_stream", failing_open)
    backoff = {}
    clock = {"now": time.monotonic()}
    for _ in range(120):
        server._supervise_streams_once(backoff, clock=lambda: clock["now"])
        clock["now"] += 1.0

    # 26 attempts in two minutes, two lines: the first failure, then one per
    # minute with the count of the hidden ones.
    assert backoff["output 4"][0] == 26
    assert len(warnings) == 2
    assert "suppressed" in warnings[1][-1]


def test_supervisor_backoff_starts_when_a_slow_attempt_ends(server, monkeypatch):
    server._running = True
    with server._lock:
        server._outputs[4] = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    clock = {"now": 100.0}

    def slow_failing_open(out):
        clock["now"] += 3.0
        raise RuntimeError("driver timeout")

    monkeypatch.setattr(server, "_open_output_stream", slow_failing_open)
    backoff = {}

    server._supervise_streams_once(backoff, clock=lambda: clock["now"])

    assert backoff == {"output 4": (1, 104.0)}


def test_supervisor_reports_only_streams_it_actually_reopened(server, monkeypatch):
    server._running = True
    output = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    with server._lock:
        server._outputs[4] = output
    infos = []
    monkeypatch.setattr(server_mod.logger, "info", lambda *args, **kwargs: infos.append(args))
    monkeypatch.setattr(server, "_open_output_stream", lambda out: pytest.fail("another opener holds the output"))

    with output.open_lock:
        server._supervise_streams_once({})

    assert infos == []


def test_snapshots_report_streams_whose_callbacks_stalled(server):
    server._running = True
    output = models_mod.OutputState(4, 0, PLATEAU_BUS_ID)
    output.stream = _ActiveStream()
    output.last_callback_monotonic = time.monotonic() - 2.0
    with server._lock:
        server._outputs[4] = output
    server._return_enabled = True
    with server._return_lock:
        server._return_stream = _ActiveStream()
        server._return_last_callback_monotonic = time.monotonic() - 2.0

    assert "ne répond plus" in server.get_outputs_snapshot()[4]["stream_error"]
    assert "ne répond plus" in server.get_stats_snapshot()["return_stream_error"]


def test_oversized_datagram_never_pauses_udp_reception(server, monkeypatch):
    class _OversizedDatagram(OSError):
        winerror = 10040  # WSAEMSGSIZE

    sleeps = []
    monkeypatch.setattr(
        mixer_mod,
        "time",
        SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=sleeps.append),
    )

    class _Socket:
        def __init__(self):
            self.calls = 0

        def recvfrom(self, size):
            self.calls += 1
            if self.calls == 1:
                raise _OversizedDatagram("message too long")
            if self.calls == 2:
                return _legacy_audio_packet(7, 1, 1, b"legacy"), ("127.0.0.1", 40000)
            server._stop.set()
            raise TimeoutError

        def close(self):
            return None

    server._sock.close()
    server._sock = _Socket()

    server._rx_loop()

    assert sleeps == []
    assert server._rx_invalid_packets == 2
    assert server._rx_socket_errors == 0


def test_bad_packets_are_logged_once_per_source_and_window(server, monkeypatch):
    warnings = []
    monkeypatch.setattr(server_mod.logger, "warning", lambda *args, **kwargs: warnings.append(args))

    for _ in range(50):
        server._log_bad_packet(("10.0.0.5", 5000), "bad magic")
    server._log_bad_packet(("10.0.0.6", 5000), "bad magic")

    assert len(warnings) == 2


def test_log_throttle_reports_suppressed_lines_once_the_window_reopens():
    throttle = models_mod.LogThrottle(10.0, max_keys=2)

    assert throttle.allow("a", now=0.0) == ""
    assert throttle.allow("a", now=1.0) is None
    assert throttle.allow("a", now=2.0) is None
    assert "2 similar" in throttle.allow("a", now=10.5)
    # The table stays bounded under a flood of distinct sources.
    for index in range(10):
        throttle.allow(f"spoofed-{index}", now=11.0)
    assert len(throttle._entries) <= 2


def test_hello_of_a_new_client_does_not_flush_other_audio(server):
    output = models_mod.OutputState(9, 0, PLATEAU_BUS_ID)
    output.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))
    with server._lock:
        server._outputs[9] = output
        epoch = server._routing_epoch
        server._mix_queue.put_nowait(mixer_mod.route_sources([]))
    observed = []

    def record_at_welcome():
        if not observed:
            observed.append((server._routing_epoch, output.ring.depth, server._mix_queue.qsize()))

    connection = _ScriptedConnection(_hello("brand-new-client"), b"", on_send=record_at_welcome)
    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    assert observed == [(epoch, 1, 1)]


@pytest.mark.parametrize("live", [True, False])
def test_hello_revokes_only_a_live_previous_session(server, live):
    client_uuid = "reconnecting-client"
    client_id = client_id_from_uuid(client_uuid)
    state = _client_state()
    state.client_uuid = client_uuid
    if not live:
        state.session_token = 0
        state.control_connected = False
    output = models_mod.OutputState(9, 0, PLATEAU_BUS_ID)
    output.ring.push(np.full((FRAME_SAMPLES,), 0.25, dtype=np.float32))
    with server._lock:
        server._clients[client_id] = state
        server._outputs[9] = output
        epoch = server._routing_epoch
    observed = []

    def record_at_welcome():
        if not observed:
            observed.append((server._routing_epoch - epoch, output.ring.depth))

    connection = _ScriptedConnection(_hello(client_uuid), b"", on_send=record_at_welcome)
    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    # A live session is revoked (epoch advanced) before the welcome; an
    # expired one was revoked when it expired. Mixed audio is never flushed.
    assert observed == ([(1, 1)] if live else [(0, 1)])


def test_config_push_cannot_overtake_the_welcome(server, monkeypatch):
    pushers = []

    def push_between_commit_and_welcome():
        pusher = threading.Thread(target=server._control_push_all_configs, daemon=True)
        pusher.start()
        pusher.join(timeout=0.2)
        pushers.append(pusher)

    # A new profile triggers an autosave between the hello commit and the
    # welcome: a fader move at that instant pushes a config concurrently.
    monkeypatch.setattr(server, "_autosave_preset", push_between_commit_and_welcome)
    connection = _ScriptedConnection(_hello("welcome-order-client"), b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))
    pushers[0].join(timeout=1.0)

    assert [message["type"] for message in _sent_messages(connection)][0] == "welcome"


def test_silent_control_session_is_closed_and_its_audio_revoked(server, monkeypatch):
    monkeypatch.setattr(control_mod, "CTRL_LIVENESS_TIMEOUT_S", 0.05)
    client_uuid = "half-open-client"
    client_id = client_id_from_uuid(client_uuid)

    class _SilentAfterHello(_ScriptedConnection):
        def recv(self, size):
            if self._chunks:
                return self._chunks.pop(0)
            time.sleep(0.005)
            raise TimeoutError

    connection = _SilentAfterHello(_hello(client_uuid))
    session = threading.Thread(
        target=server._ctrl_session_loop,
        args=(connection, ("127.0.0.1", 50000)),
        daemon=True,
    )
    session.start()
    session.join(timeout=2.0)

    assert not session.is_alive()
    assert connection.closed
    assert client_id not in server._ctrl_sessions
    assert server._clients[client_id].session_token == 0
    assert _sent_messages(connection)[-1]["type"] == "error"


def test_operator_faders_never_send_on_the_caller_thread(server, monkeypatch):
    pushes = []
    monkeypatch.setattr(server, "_control_push_all_configs", lambda: pushes.append(threading.current_thread()))
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)
    state = _client_state()
    with server._lock:
        server._clients[7] = state

    server.set_bus_gain(PLATEAU_BUS_ID, -6.0)
    server.set_bus_feed_to_regie(PLATEAU_BUS_ID, True)
    server.set_client_input_gain(7, 3.0)

    assert pushes == []
    worker = threading.Thread(target=server._config_push_loop, daemon=True)
    worker.start()
    deadline = time.monotonic() + 1.0
    while not pushes and time.monotonic() < deadline:
        time.sleep(0.005)
    server._stop.set()
    worker.join(timeout=1.0)

    # Coalesced: the three changes reach every client in one push.
    assert pushes == [worker]
    assert state.input_gain_db == pytest.approx(3.0)


def test_server_gain_setters_reject_non_finite_values(server, monkeypatch):
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)
    output = models_mod.OutputState(4, 0, PLATEAU_BUS_ID, gain_db=-3.0)
    with server._lock:
        server._outputs[4] = output

    server.set_bus_gain(PLATEAU_BUS_ID, float("nan"))
    server.set_output_gain(4, float("inf"))

    assert server.get_buses_snapshot()[PLATEAU_BUS_ID]["gain_db"] == 0.0
    assert output.gain_db == 0.0


def test_physical_outputs_accept_any_fixed_bus_through_one_factory(monkeypatch, tmp_path):
    monkeypatch.setattr(opus_codec_mod, "OpusEncoder", _FakeEncoder)
    monkeypatch.setattr(opus_codec_mod, "OpusDecoder", _FakeDecoder)
    srv = server_mod.IntercomServer(
        bind_ip="127.0.0.1",
        port=0,
        outputs=[
            {"device": 1, "bus_id": REGIE_BUS_ID, "gain_db": 99.0},
            {"device": 2, "bus_id": 99},
            {"device": None, "bus_id": PLATEAU_BUS_ID},
        ],
        preset_path=str(tmp_path / "server-preset.json"),
        discovery_enabled=False,
    )
    try:
        outputs = srv.get_outputs_snapshot()
    finally:
        srv.stop()

    # Régie is a valid physical bus, as in the GUI; invalid entries are skipped
    # and the gain is bounded instead of being silently remapped.
    assert [(o["device"], o["bus_id"], o["gain_db"]) for o in outputs.values()] == [(1, REGIE_BUS_ID, 20.0)]


# --- Server engine, second audit pass ----------------------------------------


class _PlcAwareDecoder:
    """Real frames decode to 0.25, PLC (empty payload) to 0.5, FEC recovery to 0.75."""

    def decode(self, payload, *, fec=False):
        value = 0.75 if fec else 0.5 if payload == b"" else 0.25
        return np.full((FRAME_SAMPLES,), value, dtype=np.float32)


def test_real_jitter_buffer_concealment_is_routed_on_the_talk_bus(server, monkeypatch):
    now = time.monotonic()
    state = _client_state(now=now, decoder=_PlcAwareDecoder())
    state.jb = new_realtime_jitter_buffer()
    with server._lock:
        server._clients[7] = state
    # Seq 7 is lost in the middle of the talk spurt (seq 8 rebuilds it from
    # its FEC), then the stream stops and PLC takes over. The packets fill
    # exactly the pre-buffer, so playout starts on the first of them.
    before_loss = list(range(7 - (JB_START_FRAMES - 2), 7))
    for sequence in (*before_loss, 8, 9):
        packet = _client_packet(7, bus_mask=1 << PLATEAU_BUS_ID, sequence_number=sequence)
        assert server._accept_udp_packet(packet, state.addr, now=now)

    played = ["real"] * len(before_loss) + ["fec", "real", "real"] + ["plc"] * JB_MAX_CONCEALED_FRAMES
    calls = _run_mix_ticks(server, monkeypatch, len(played) + 1)

    # Every concealment frame plays on the bus of the talk it continues,
    # instead of a hole; after the last one the stream is silent.
    kinds = {0.25: "real", 0.5: "plc", 0.75: "fec"}
    assert [[(kinds[float(s.frame[0])], s.bus_mask) for s in sources] for sources in calls] == [
        [(kind, 1 << PLATEAU_BUS_ID)] for kind in played
    ] + [[]]


def test_bus_meters_follow_the_mix_even_without_a_physical_output(server, monkeypatch):
    state = _client_state(decoder=_ValueDecoder(0.25))
    state.jb = _ScriptedJitter([_real_frame(1, 1 << VMIX_BUS_ID)])
    with server._lock:
        server._clients[7] = state

    _run_mix_ticks(server, monkeypatch, 1)
    bus_vu_dbfs = server.get_stats_snapshot()["bus_vu_dbfs"]

    assert bus_vu_dbfs[VMIX_BUS_ID] == pytest.approx(20.0 * np.log10(0.25), abs=0.01)
    assert bus_vu_dbfs[REGIE_BUS_ID] == -60.0
    assert bus_vu_dbfs[PLATEAU_BUS_ID] == -60.0


def test_client_snapshot_exposes_jitter_buffer_resets(server):
    state = _client_state()
    state.jb.reset()
    with server._lock:
        server._clients[7] = state

    assert server.get_clients_snapshot()[7]["jb_resets"] == 1


def _install_control_session(server, client_id):
    connection = _ScriptedConnection()
    with server._ctrl_lock:
        server._ctrl_sessions[client_id] = connection
        server._ctrl_send_locks[client_id] = threading.Lock()
    with server._lock:
        server._clients[client_id] = _client_state()
    return connection


def _pushed_configs(connection):
    configs = [message["config"] for message in _sent_messages(connection) if message["type"] == "update"]
    connection.sent.clear()
    return configs


def test_bus_changes_never_push_a_microphone_gain_back_to_clients(server, monkeypatch):
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)
    first = _install_control_session(server, 7)
    second = _install_control_session(server, 8)

    server.set_bus_gain(PLATEAU_BUS_ID, -6.0)
    server._control_push_all_configs()
    # The clients apply their gain locally: a bus change must not reset it.
    assert ["input_gain_db" in config for config in _pushed_configs(first) + _pushed_configs(second)] == [
        False,
        False,
    ]

    server.set_client_input_gain(7, 3.0)
    server._control_push_all_configs()
    assert [config.get("input_gain_db") for config in _pushed_configs(first)] == [3.0]
    assert ["input_gain_db" in config for config in _pushed_configs(second)] == [False]

    server._control_push_all_configs()
    assert ["input_gain_db" in config for config in _pushed_configs(first)] == [False]


def test_welcome_config_restores_the_saved_microphone_gain(server, monkeypatch):
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)
    client_uuid = "gain-restore-client"
    server._client_profiles[client_uuid] = models_mod.ClientProfile(client_uuid, input_gain_db=4.0)
    connection = _ScriptedConnection(_hello(client_uuid), b"")

    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    welcome, update = _sent_messages(connection)[:2]
    assert welcome["type"] == "welcome"
    assert "server_time_ms" not in welcome
    assert update["config"]["input_gain_db"] == 4.0


class _CountingLock:
    def __init__(self, lock):
        self.lock = lock
        self.entries = 0

    def __enter__(self):
        self.entries += 1
        return self.lock.__enter__()

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)


def test_ping_takes_the_engine_lock_once_and_answers_the_return_meter(server, monkeypatch):
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)
    client_uuid = "ping-client"
    client_id = client_id_from_uuid(client_uuid)
    ping = encode_control_message({"type": "ping"})
    entries_during_ping = []

    class _PingConnection(_ScriptedConnection):
        def recv(self, size):
            if isinstance(server._lock, _CountingLock):
                entries_during_ping.append(server._lock.entries)
                server._lock = server._lock.lock
            elif self._chunks and self._chunks[0] is ping:
                with server._lock:
                    server._clients[client_id].last_control_monotonic = 1.0
                server._lock = _CountingLock(server._lock)
            return super().recv(size)

    server._return_vu_dbfs = -18.0
    connection = _PingConnection(_hello(client_uuid), ping, b"")
    server._ctrl_session_loop(connection, ("127.0.0.1", 50000))

    pongs = [message for message in _sent_messages(connection) if message["type"] == "pong"]
    assert pongs == [{"type": "pong", "return_vu_dbfs": -18.0}]
    # Liveness check and refresh share one section: pings arrive at 10 Hz.
    assert entries_during_ping == [1]
    assert server._clients[client_id].last_control_monotonic > 1.0


def test_control_sessions_log_their_opening_and_end_with_the_reason(server, monkeypatch):
    monkeypatch.setattr(server, "_autosave_preset", lambda: None)
    lines = []
    monkeypatch.setattr(control_mod.logger, "info", lambda message, *args: lines.append(message.format(*args)))

    server._ctrl_session_loop(_ScriptedConnection(_hello("logged-client"), b""), ("127.0.0.1", 50000))
    server._ctrl_session_loop(_ScriptedConnection(b"{}\n"), ("127.0.0.1", 50001))

    client_id = client_id_from_uuid("logged-client")
    # A refused hello opened nothing: only the real session is logged.
    assert [line for line in lines if "control session" in line] == [
        f"control session opened: client {client_id} ('') from 127.0.0.1, audio port 41000",
        f"control session of client {client_id} from 127.0.0.1 ended: closed by the client",
    ]


def test_stereo_return_is_summed_to_mono_without_losing_one_side(server):
    server._return_enabled = True
    gate = threading.Event()
    gate.set()
    left_only = np.zeros((FRAME_SAMPLES, 2), dtype=np.float32)
    left_only[:, 0] = 0.5
    both = np.full((FRAME_SAMPLES, 2), 0.5, dtype=np.float32)
    out = np.empty(FRAME_SAMPLES, dtype=np.float32)

    for frame in (left_only, both):
        server._return_in_callback(frame, FRAME_SAMPLES, None, None, active_gate=gate)

    assert server._return_frames.pop_into(out) is not None
    np.testing.assert_allclose(out, 0.25, atol=1e-6)
    assert server._return_frames.pop_into(out) is not None
    np.testing.assert_allclose(out, 0.5, atol=1e-6)


def test_control_accept_survives_a_transient_error(server):
    class _FlakyListener:
        def __init__(self):
            self.calls = 0

        def accept(self):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionResetError(10054, "peer reset before accept")
            server._stop.set()
            raise OSError(10038, "socket closed by stop()")

    listener = _FlakyListener()
    server._ctrl_sock = listener

    server._ctrl_accept_loop()

    # The first error was ridden out: the loop only returned once stopping.
    assert listener.calls == 2


def test_start_runs_even_when_every_output_fails_to_open(server, monkeypatch):
    output = models_mod.OutputState(output_id=0, device=0, bus_id=PLATEAU_BUS_ID)
    with server._lock:
        server._outputs[0] = output

    def failing_open(out):
        out.stream_error = "carte occupée"
        raise RuntimeError("carte occupée")

    monkeypatch.setattr(server, "_open_output_stream", failing_open)

    server.start()

    assert server.get_stats_snapshot()["running"] is True
    assert server.get_outputs_snapshot()[0]["stream_error"] == "carte occupée"


def test_operator_gain_on_a_station_not_yet_connected_is_kept(server):
    client_uuid = "known-but-offline"
    client_id = client_id_from_uuid(client_uuid)
    with server._lock:
        server._client_profiles[client_uuid] = models_mod.ClientProfile(client_uuid)

    server.set_client_input_gain(client_id, 6.0)

    assert server.get_clients_snapshot()[client_id]["input_gain_db"] == pytest.approx(6.0)
