from __future__ import annotations

from py_intercom.common import audio_errors


def test_shared_stream_settings_are_wasapi_only(monkeypatch) -> None:
    marker = object()
    calls: list[dict] = []

    monkeypatch.setattr(
        audio_errors.sd,
        "query_hostapis",
        lambda index: {"name": "Windows WASAPI" if int(index) == 2 else "MME"},
    )

    def make_settings(**kwargs):
        calls.append(dict(kwargs))
        return marker

    monkeypatch.setattr(audio_errors.sd, "WasapiSettings", make_settings)

    assert audio_errors.shared_stream_extra_settings({"hostapi": 2}) is marker
    assert audio_errors.shared_stream_extra_settings({"hostapi": 0}) is None
    assert audio_errors.shared_stream_extra_settings(None) is None
    assert calls == [{"exclusive": False, "auto_convert": True}]


def test_shared_stream_settings_fall_back_when_wasapi_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        audio_errors.sd,
        "query_hostapis",
        lambda _index: {"name": "Windows WASAPI"},
    )

    def unavailable(**_kwargs) -> None:
        raise RuntimeError("not available")

    monkeypatch.setattr(audio_errors.sd, "WasapiSettings", unavailable)

    assert audio_errors.shared_stream_extra_settings({"hostapi": 2}) is None


def test_bluetooth_error_explains_valid_duplex_pairing() -> None:
    message = audio_errors.friendly_audio_open_error(
        RuntimeError("Device unavailable"),
        kind="output",
        dev_info={
            "name": "Casque (@System32\\drivers\\bthhfenum.sys; G435)",
            "hostapi": 3,
        },
        device=92,
        sr=48_000,
        ch=1,
    )

    assert "mains-libres/HFP du même casque" in message
    assert "A2DP et le micro HFP" in message
    assert "dans ce cas seulement" in message
