from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from py_intercom.common.bootstrap import prepare_windows_runtime
from py_intercom.common.opus_codec import OpusConfig, OpusDecoder, OpusEncoder


VALID_48K_FRAME_SAMPLES = (120, 240, 480, 960)


@pytest.fixture
def fake_opus(monkeypatch):
    encoder_instances = []
    decoder_instances = []
    decoder_ctl = SimpleNamespace(error=None, calls=[])

    class FakeEncoderBackend:
        def __init__(self, sample_rate, channels, application):
            self.sample_rate = sample_rate
            self.channels = channels
            self.application = application
            self.bitrate = None
            self.complexity = None
            self.result = b"fake-opus"
            self.calls = []
            encoder_instances.append(self)

        def encode(self, pcm, frame_samples):
            self.calls.append((pcm, frame_samples))
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    class FakeDecoderBackend:
        def __init__(self, sample_rate, channels):
            self.sample_rate = sample_rate
            self.channels = channels
            self.decoder_state = self
            self.decoded_pcm = None
            self.plc_pcm = None
            self.decode_error = None
            self.plc_error = None
            self.decoder_complexity = None
            self.decode_calls = []
            self.plc_calls = []
            decoder_instances.append(self)

        def decode(self, payload, frame_samples, decode_fec=False):
            self.decode_calls.append((payload, frame_samples, decode_fec))
            if self.decode_error is not None:
                raise self.decode_error
            if self.decoded_pcm is not None:
                return self.decoded_pcm
            return np.zeros((frame_samples,), dtype=np.int16).tobytes()

    def fake_plc_decode(
        decoder_state,
        payload,
        payload_length,
        frame_samples,
        *,
        decode_fec,
        channels,
    ):
        decoder_state.plc_calls.append(
            (payload, payload_length, frame_samples, decode_fec, channels)
        )
        if decoder_state.plc_error is not None:
            raise decoder_state.plc_error
        if decoder_state.plc_pcm is not None:
            return decoder_state.plc_pcm
        return np.zeros((frame_samples * channels,), dtype=np.int16).tobytes()

    opuslib = types.ModuleType("opuslib")
    opuslib.__path__ = []
    opuslib.APPLICATION_AUDIO = 2049
    opuslib.Encoder = FakeEncoderBackend
    opuslib.Decoder = FakeDecoderBackend

    api = types.ModuleType("opuslib.api")
    api.__path__ = []
    decoder_api = types.ModuleType("opuslib.api.decoder")
    decoder_api.decode = fake_plc_decode
    ctl_api = types.ModuleType("opuslib.api.ctl")
    ctl_api.set_complexity = object()
    ctl_api.get_complexity = object()

    def fake_decoder_ctl(decoder_state, request, *args):
        decoder_ctl.calls.append((decoder_state, request, args))
        if decoder_ctl.error is not None:
            raise decoder_ctl.error
        if request is ctl_api.set_complexity:
            decoder_state.decoder_complexity = int(args[0])
            return None
        if request is ctl_api.get_complexity:
            return decoder_state.decoder_complexity
        raise AssertionError("unexpected decoder CTL request")

    decoder_api.decoder_ctl = fake_decoder_ctl
    opuslib.api = api
    api.decoder = decoder_api
    api.ctl = ctl_api

    monkeypatch.setitem(sys.modules, "opuslib", opuslib)
    monkeypatch.setitem(sys.modules, "opuslib.api", api)
    monkeypatch.setitem(sys.modules, "opuslib.api.ctl", ctl_api)
    monkeypatch.setitem(sys.modules, "opuslib.api.decoder", decoder_api)

    return SimpleNamespace(
        encoder_instances=encoder_instances,
        decoder_instances=decoder_instances,
        decoder_ctl=decoder_ctl,
        ctl_api=ctl_api,
    )


@pytest.fixture(scope="module")
def real_opus_backend():
    # The packaged Windows runtime ships libopus outside the regular DLL search
    # path, so mirror the application bootstrap before importing opuslib.
    project_root = Path(__file__).resolve().parents[1]
    try:
        runtime = prepare_windows_runtime(project_root)
    except Exception as exc:  # pragma: no cover - depends on host loader policy
        pytest.skip(f"libopus bootstrap unavailable: {exc}")

    try:
        import opuslib  # noqa: F401
    except Exception as exc:
        pytest.skip(f"opuslib unavailable: {exc}")

    try:
        OpusEncoder()
        OpusDecoder()
    except RuntimeError as exc:
        pytest.skip(f"native libopus unavailable: {exc}")
    return runtime


@pytest.mark.parametrize("frame_samples", VALID_48K_FRAME_SAMPLES)
def test_fake_backend_accepts_required_48k_frame_durations(fake_opus, frame_samples):
    config = OpusConfig(frame_samples=frame_samples)
    encoder = OpusEncoder(config)
    source = np.linspace(-0.5, 0.5, frame_samples, dtype=np.float32)

    payload = encoder.encode(source)

    backend_encoder = fake_opus.encoder_instances[-1]
    pcm, encoded_samples = backend_encoder.calls[-1]
    assert payload == b"fake-opus"
    assert encoded_samples == frame_samples
    assert len(pcm) == frame_samples * 2
    assert backend_encoder.bitrate == config.bitrate
    assert backend_encoder.complexity == config.complexity

    decoder = OpusDecoder(config)
    decoded = decoder.decode(payload)
    assert decoded.shape == (frame_samples,)
    assert decoded.dtype == np.float32
    assert np.isfinite(decoded).all()
    assert fake_opus.decoder_instances[-1].decoder_complexity == config.decoder_complexity


@pytest.mark.parametrize(
    ("config", "exception"),
    (
        (OpusConfig(sample_rate=44_100), ValueError),
        (OpusConfig(channels=2), ValueError),
        (OpusConfig(frame_samples=121), ValueError),
        (OpusConfig(bitrate=499), ValueError),
        (OpusConfig(complexity=11), ValueError),
        (OpusConfig(decoder_complexity=11), ValueError),
    ),
)
def test_invalid_opus_configuration_fails_before_native_backend(fake_opus, config, exception):
    with pytest.raises(exception):
        OpusEncoder(config)
    assert fake_opus.encoder_instances == []


def test_decoder_configures_and_verifies_deep_plc_complexity(fake_opus):
    decoder = OpusDecoder(OpusConfig(decoder_complexity=7))
    backend = fake_opus.decoder_instances[-1]

    assert backend.decoder_complexity == 7
    assert fake_opus.decoder_ctl.calls == [
        (backend, fake_opus.ctl_api.set_complexity, (7,)),
        (backend, fake_opus.ctl_api.get_complexity, ()),
    ]
    assert decoder.config.decoder_complexity == 7


def test_decoder_fails_closed_when_deep_plc_ctl_is_unavailable(fake_opus):
    fake_opus.decoder_ctl.error = RuntimeError("unsupported decoder complexity")

    with pytest.raises(RuntimeError, match="Deep PLC Opus decoder"):
        OpusDecoder()


@pytest.mark.parametrize(
    "source",
    (
        np.zeros((120, 1), dtype=np.float32),
        np.zeros((119,), dtype=np.float32),
    ),
)
def test_encoder_rejects_non_mono_or_wrong_length_frames(fake_opus, source):
    encoder = OpusEncoder(OpusConfig(frame_samples=120))
    with pytest.raises(ValueError, match="mono with shape"):
        encoder.encode(source)


def test_encoder_rejects_non_floating_samples(fake_opus):
    encoder = OpusEncoder(OpusConfig(frame_samples=120))
    with pytest.raises(TypeError, match="floating-point"):
        encoder.encode(np.zeros((120,), dtype=np.int16))


@pytest.mark.parametrize("invalid_value", (np.nan, np.inf, -np.inf))
def test_encoder_rejects_non_finite_samples(fake_opus, invalid_value):
    encoder = OpusEncoder(OpusConfig(frame_samples=120))
    source = np.zeros((120,), dtype=np.float32)
    source[17] = invalid_value
    with pytest.raises(ValueError, match="finite"):
        encoder.encode(source)


def test_encoder_accepts_float64_and_sends_mono_int16_pcm(fake_opus):
    encoder = OpusEncoder(OpusConfig(frame_samples=120))
    encoder.encode(np.full((120,), 0.25, dtype=np.float64))
    pcm, frame_samples = fake_opus.encoder_instances[-1].calls[-1]
    assert frame_samples == 120
    assert np.frombuffer(pcm, dtype=np.int16).shape == (120,)


@pytest.mark.parametrize("result", (None, "opus", np.zeros((1,), dtype=np.uint8)))
def test_encoder_rejects_non_bytes_backend_output(fake_opus, result):
    encoder = OpusEncoder(OpusConfig(frame_samples=120))
    fake_opus.encoder_instances[-1].result = result
    with pytest.raises(TypeError, match="non-bytes"):
        encoder.encode(np.zeros((120,), dtype=np.float32))


def test_encoder_rejects_empty_or_oversized_backend_payload(fake_opus):
    encoder = OpusEncoder(OpusConfig(frame_samples=120))
    backend = fake_opus.encoder_instances[-1]
    source = np.zeros((120,), dtype=np.float32)

    backend.result = b""
    with pytest.raises(RuntimeError, match="empty"):
        encoder.encode(source)

    backend.result = b"x" * 1_276
    with pytest.raises(RuntimeError, match="oversized"):
        encoder.encode(source)


def test_short_decoded_duration_is_padded_with_silence(fake_opus):
    decoder = OpusDecoder(OpusConfig(frame_samples=960))
    backend = fake_opus.decoder_instances[-1]
    short_pcm = np.array((16_384, -16_384) * 60, dtype=np.int16)
    backend.decoded_pcm = short_pcm.tobytes()

    frame = decoder.decode(b"short-duration-packet")

    assert frame.shape == (960,)
    np.testing.assert_allclose(frame[:120], short_pcm.astype(np.float32) / 32_768.0)
    np.testing.assert_array_equal(frame[120:], np.zeros((840,), dtype=np.float32))


def test_long_fake_decoded_duration_is_truncated_to_engine_contract(fake_opus):
    decoder = OpusDecoder(OpusConfig(frame_samples=120))
    backend = fake_opus.decoder_instances[-1]
    backend.decoded_pcm = np.arange(960, dtype=np.int16).tobytes()

    frame = decoder.decode(b"long-duration-packet")

    assert frame.shape == (120,)
    np.testing.assert_allclose(frame, np.arange(120, dtype=np.float32) / 32_768.0)


def test_plc_uses_null_packet_and_pads_short_native_output(fake_opus):
    decoder = OpusDecoder(OpusConfig(frame_samples=480))
    backend = fake_opus.decoder_instances[-1]
    backend.plc_pcm = np.full((120,), 1_000, dtype=np.int16).tobytes()

    frame = decoder.decode(b"")

    assert backend.plc_calls == [(None, 0, 480, False, 1)]
    assert frame.shape == (480,)
    np.testing.assert_allclose(frame[:120], 1_000.0 / 32_768.0)
    np.testing.assert_array_equal(frame[120:], np.zeros((360,), dtype=np.float32))


def test_plc_falls_back_to_high_level_decoder_without_native_state(fake_opus):
    decoder = OpusDecoder(OpusConfig(frame_samples=120))
    backend = fake_opus.decoder_instances[-1]
    backend.decoder_state = None

    frame = decoder.decode(b"")

    assert backend.decode_calls == [(b"", 120, False)]
    assert frame.shape == (120,)


@pytest.mark.parametrize("payload", (None, "opus", [1, 2, 3]))
def test_decoder_rejects_non_bytes_payload(fake_opus, payload):
    decoder = OpusDecoder(OpusConfig(frame_samples=120))
    with pytest.raises(TypeError, match="bytes-like"):
        decoder.decode(payload)


def test_decoder_rejects_oversized_payload_before_native_backend(fake_opus):
    decoder = OpusDecoder(OpusConfig(frame_samples=120))
    backend = fake_opus.decoder_instances[-1]
    with pytest.raises(ValueError, match="1275-byte"):
        decoder.decode(b"x" * 1_276)
    assert backend.decode_calls == []


@pytest.mark.parametrize("pcm", (b"\x00", "not-pcm"))
def test_decoder_rejects_malformed_native_pcm(fake_opus, pcm):
    decoder = OpusDecoder(OpusConfig(frame_samples=120))
    fake_opus.decoder_instances[-1].decoded_pcm = pcm
    exception = ValueError if isinstance(pcm, bytes) else TypeError
    with pytest.raises(exception):
        decoder.decode(b"opus")


def test_invalid_opus_payload_error_is_not_hidden(fake_opus):
    decoder = OpusDecoder(OpusConfig(frame_samples=120))
    fake_opus.decoder_instances[-1].decode_error = ValueError("corrupted Opus packet")
    with pytest.raises(ValueError, match="corrupted Opus packet"):
        decoder.decode(b"invalid-opus")


@pytest.mark.parametrize("frame_samples", VALID_48K_FRAME_SAMPLES)
def test_real_opus_round_trip_for_required_48k_durations(real_opus_backend, frame_samples):
    config = OpusConfig(frame_samples=frame_samples)
    encoder = OpusEncoder(config)
    decoder = OpusDecoder(config)
    time_axis = np.arange(frame_samples, dtype=np.float32) / np.float32(48_000.0)
    source = (0.2 * np.sin(2.0 * np.pi * 1_000.0 * time_axis)).astype(np.float32)

    payload = encoder.encode(source)
    decoded = decoder.decode(payload)

    assert 0 < len(payload) <= 1_275
    assert decoded.shape == (frame_samples,)
    assert decoded.dtype == np.float32
    assert np.isfinite(decoded).all()


@pytest.mark.parametrize("frame_samples", VALID_48K_FRAME_SAMPLES)
def test_real_opus_plc_is_exact_finite_frame(real_opus_backend, frame_samples):
    config = OpusConfig(frame_samples=frame_samples)
    encoder = OpusEncoder(config)
    decoder = OpusDecoder(config)
    decoder.decode(encoder.encode(np.zeros((frame_samples,), dtype=np.float32)))

    concealed = decoder.decode(b"")

    assert concealed.shape == (frame_samples,)
    assert concealed.dtype == np.float32
    assert np.isfinite(concealed).all()


def test_real_opus_rejects_corrupted_packet(real_opus_backend):
    import opuslib

    decoder = OpusDecoder(OpusConfig(frame_samples=480))
    with pytest.raises(opuslib.OpusError):
        # Code-3 framing with 63 frames is invalid (Opus allows at most 48).
        decoder.decode(b"\xff\x3f")


def test_real_short_duration_packet_is_safely_padded(real_opus_backend):
    source = np.zeros((120,), dtype=np.float32)
    packet = OpusEncoder(OpusConfig(frame_samples=120)).encode(source)

    decoded = OpusDecoder(OpusConfig(frame_samples=960)).decode(packet)

    assert decoded.shape == (960,)
    assert decoded.dtype == np.float32
    assert np.isfinite(decoded).all()
    np.testing.assert_array_equal(decoded[120:], np.zeros((840,), dtype=np.float32))


def test_real_long_duration_packet_fails_closed_on_short_decoder(real_opus_backend):
    import opuslib

    source = np.zeros((960,), dtype=np.float32)
    packet = OpusEncoder(OpusConfig(frame_samples=960)).encode(source)
    decoder = OpusDecoder(OpusConfig(frame_samples=120))

    with pytest.raises(opuslib.OpusError):
        decoder.decode(packet)
