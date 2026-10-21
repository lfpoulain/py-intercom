from __future__ import annotations

from py_intercom.client.hotkeys import PttHotkeyState
from py_intercom.common import devices as devices_mod
from py_intercom.common.constants import CHANNELS, SAMPLE_RATE


def test_momentary_hotkey_tracks_the_configured_chord() -> None:
    state = PttHotkeyState({1: ("ptt", "Ctrl+T")})

    assert state.press("key:ctrl_l") == []
    assert state.press("char:t") == [(1, True)]
    assert state.press("char:t") == []
    assert state.release("char:t") == [(1, False)]
    assert state.release("key:ctrl_l") == []


def test_toggle_hotkey_ignores_unrelated_keys_while_chord_is_held() -> None:
    state = PttHotkeyState({2: ("toggle", "Ctrl+T")})

    assert state.press("key:ctrl_l") == []
    assert state.press("char:t") == [(2, True)]
    assert state.press("char:x") == []
    assert state.release("char:x") == []
    assert state.release("char:t") == []
    assert state.press("char:t") == [(2, False)]


def test_release_all_deactivates_momentary_and_toggle_buses() -> None:
    state = PttHotkeyState(
        {
            1: ("ptt", "F1"),
            2: ("toggle", "F2"),
            3: ("always_on", "F3"),
        }
    )

    assert state.press("key:f1") == [(1, True)]
    assert state.press("key:f2") == [(2, True)]

    assert sorted(state.release_all()) == [(1, False), (2, False)]
    assert state.active_buses() == {}


def test_device_validation_uses_the_engine_audio_format(monkeypatch) -> None:
    checked: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        devices_mod.sd,
        "query_hostapis",
        lambda: [{"name": "Windows WASAPI"}],
    )
    monkeypatch.setattr(
        devices_mod.sd,
        "query_devices",
        lambda: [
            {
                "name": "Duplex",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 44100.0,
            }
        ],
    )

    def check_output_settings(**kwargs) -> None:
        checked.append(("output", dict(kwargs)))

    def check_input_settings(**kwargs) -> None:
        checked.append(("input", dict(kwargs)))

    monkeypatch.setattr(devices_mod.sd, "check_output_settings", check_output_settings)
    monkeypatch.setattr(devices_mod.sd, "check_input_settings", check_input_settings)

    result = devices_mod.list_devices(hard_refresh=False, validate=True)

    assert len(result) == 1
    assert {kind for kind, _kwargs in checked} == {"input", "output"}
    for _kind, kwargs in checked:
        assert kwargs["channels"] == CHANNELS
        assert kwargs["samplerate"] == float(SAMPLE_RATE)


def test_device_is_hidden_when_48khz_mono_is_not_supported(monkeypatch) -> None:
    monkeypatch.setattr(
        devices_mod.sd,
        "query_hostapis",
        lambda: [{"name": "Windows WASAPI"}],
    )
    monkeypatch.setattr(
        devices_mod.sd,
        "query_devices",
        lambda: [
            {
                "name": "Unsupported",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 1,
                "default_samplerate": 44100.0,
            }
        ],
    )

    def unsupported(**_kwargs) -> None:
        raise RuntimeError("48 kHz is unsupported")

    monkeypatch.setattr(devices_mod.sd, "check_output_settings", unsupported)
    monkeypatch.setattr(devices_mod.sd, "check_input_settings", unsupported)

    assert devices_mod.list_devices(hard_refresh=False, validate=True) == []
