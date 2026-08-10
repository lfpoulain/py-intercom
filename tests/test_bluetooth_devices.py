from __future__ import annotations

from dataclasses import replace

from py_intercom.common import devices as devices_mod


HFP_NAME = "Casque (G435 Bluetooth Gaming Headset Hands-Free)"
A2DP_NAME = "Casque (G435 Bluetooth Gaming Headset Stereo)"


def _install_bluetooth_device_fakes(monkeypatch) -> None:
    """Expose the three PortAudio endpoints used by a classic BT headset.

    PortAudio commonly exposes A2DP as an output-only endpoint and HFP as two
    different input/output endpoints.  On WDM-KS, both HFP endpoints can have
    exactly the same display name, which is the important regression case.
    """

    monkeypatch.setattr(
        devices_mod.sd,
        "query_hostapis",
        lambda: [
            {"name": "Windows WASAPI"},
            {"name": "Windows WDM-KS"},
        ],
    )
    monkeypatch.setattr(
        devices_mod.sd,
        "query_devices",
        lambda: [
            {
                "name": A2DP_NAME,
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 44_100.0,
            },
            {
                "name": HFP_NAME,
                "hostapi": 1,
                "max_input_channels": 0,
                "max_output_channels": 1,
                "default_samplerate": 8_000.0,
            },
            {
                "name": HFP_NAME,
                "hostapi": 1,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 8_000.0,
            },
            {
                "name": "Interface USB 48 kHz",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 1,
                "default_samplerate": 48_000.0,
            },
        ],
    )

    # The A2DP endpoint represents a shared Windows API: its native format is
    # 44.1 kHz but the host accepts the engine's 48 kHz format.  In contrast,
    # raw WDM-KS HFP endpoints accept only 8 kHz and must remain hidden until
    # py-intercom has an explicit real-time sample-rate adapter.
    accepts_engine_rate = {
        ("output", 0),
        ("input", 3),
        ("output", 3),
    }

    def _check(direction: str, **kwargs) -> None:
        key = (str(direction), int(kwargs["device"]))
        if key not in accepts_engine_rate or float(kwargs["samplerate"]) != 48_000.0:
            raise RuntimeError("Invalid sample rate")

    monkeypatch.setattr(
        devices_mod.sd,
        "check_input_settings",
        lambda **kwargs: _check("input", **kwargs),
    )
    monkeypatch.setattr(
        devices_mod.sd,
        "check_output_settings",
        lambda **kwargs: _check("output", **kwargs),
    )


def _bluetooth_device_infos() -> list[devices_mod.DeviceInfo]:
    return [
        devices_mod.DeviceInfo(
            index=0,
            name=A2DP_NAME,
            hostapi="Windows WASAPI",
            max_input_channels=0,
            max_output_channels=2,
            default_samplerate=44_100,
            input_compatible=False,
            output_compatible=True,
        ),
        devices_mod.DeviceInfo(
            index=1,
            name=HFP_NAME,
            hostapi="Windows DirectSound",
            max_input_channels=0,
            max_output_channels=1,
            default_samplerate=8_000,
            input_compatible=False,
            output_compatible=True,
        ),
        devices_mod.DeviceInfo(
            index=2,
            name=HFP_NAME,
            hostapi="Windows DirectSound",
            max_input_channels=1,
            max_output_channels=0,
            default_samplerate=8_000,
            input_compatible=True,
            output_compatible=False,
        ),
        devices_mod.DeviceInfo(
            index=3,
            name="Interface USB 48 kHz",
            hostapi="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=1,
            default_samplerate=48_000,
            input_compatible=True,
            output_compatible=True,
        ),
    ]


def test_raw_native_rate_hfp_is_filtered_but_shared_bluetooth_is_selectable(
    monkeypatch,
) -> None:
    _install_bluetooth_device_fakes(monkeypatch)

    devices = devices_mod.list_devices(
        hostapi_substring=None,
        hard_refresh=False,
        validate=True,
    )

    assert [device.index for device in devices] == [0, 3]
    assert [device.default_samplerate for device in devices] == [
        44_100.0,
        48_000.0,
    ]
    assert devices[0].supports_input is False
    assert devices[0].supports_output is True


def test_duplicate_hfp_names_restore_the_saved_directional_endpoints(
    monkeypatch,
) -> None:
    devices = _bluetooth_device_infos()

    assert (
        devices_mod.resolve_device(
            HFP_NAME,
            "Windows DirectSound",
            2,
            devices,
            direction="input",
        )
        == 2
    )
    assert (
        devices_mod.resolve_device(
            HFP_NAME,
            "Windows DirectSound",
            1,
            devices,
            direction="output",
        )
        == 1
    )


def test_device_labels_disambiguate_duplicate_bluetooth_endpoints() -> None:
    devices = _bluetooth_device_infos()

    output_label = devices_mod.format_device_label(devices[1])
    input_label = devices_mod.format_device_label(devices[2])

    for label in (output_label, input_label):
        assert HFP_NAME in label
        assert "Windows DirectSound" in label
        assert "8 kHz" in label
    assert "#1" in output_label
    assert "#2" in input_label
    assert output_label != input_label


def test_device_label_flattens_driver_line_breaks() -> None:
    device = devices_mod.DeviceInfo(
        index=90,
        name="Casque (Hands-Free\r\n;(G435 Bluetooth Gaming Headset))",
        hostapi="Windows WDM-KS",
        max_input_channels=1,
        max_output_channels=0,
        default_samplerate=8_000,
    )

    label = devices_mod.format_device_label(device)

    assert "\r" not in label
    assert "\n" not in label
    assert "Hands-Free ;(G435" in label


def test_directional_resolution_survives_portaudio_index_changes(monkeypatch) -> None:
    devices = _bluetooth_device_infos()

    # Simulate a new PortAudio enumeration where the old saved index no longer
    # exists.  The identical HFP names must still be disambiguated by direction.
    renumbered = [replace(device, index=int(device.index) + 10) for device in devices]

    assert (
        devices_mod.resolve_device(
            HFP_NAME,
            "Windows DirectSound",
            2,
            renumbered,
            direction="input",
        )
        == 12
    )
    assert (
        devices_mod.resolve_device(
            HFP_NAME,
            "Windows DirectSound",
            1,
            renumbered,
            direction="output",
        )
        == 11
    )


def test_recycled_fallback_index_never_overrides_saved_device_identity(
    monkeypatch,
) -> None:
    devices = _bluetooth_device_infos()

    # Index 3 still exists but now belongs to a USB interface.  A saved HFP
    # microphone must resolve by identity+direction, not by the recycled index.
    assert (
        devices_mod.resolve_device(
            HFP_NAME,
            "Windows DirectSound",
            3,
            devices,
            direction="input",
        )
        == 2
    )


def test_validation_is_recorded_independently_for_input_and_output(
    monkeypatch,
) -> None:
    shared_settings = object()
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
                "name": "Sortie uniquement au format moteur",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 2,
                "default_samplerate": 48_000.0,
            }
        ],
    )

    checked: list[tuple[str, dict]] = []

    def reject_input(**kwargs) -> None:
        checked.append(("input", dict(kwargs)))
        raise RuntimeError("input unsupported")

    def accept_output(**kwargs) -> None:
        checked.append(("output", dict(kwargs)))

    monkeypatch.setattr(devices_mod.sd, "check_input_settings", reject_input)
    monkeypatch.setattr(devices_mod.sd, "check_output_settings", accept_output)
    monkeypatch.setattr(
        devices_mod,
        "shared_stream_extra_settings",
        lambda _device: shared_settings,
    )

    devices = devices_mod.list_devices(validate=True)

    assert len(devices) == 1
    assert devices[0].supports_input is False
    assert devices[0].supports_output is True
    assert {direction for direction, _kwargs in checked} == {"input", "output"}
    assert all(kwargs["dtype"] == "float32" for _direction, kwargs in checked)
    assert all(
        kwargs["extra_settings"] is shared_settings
        for _direction, kwargs in checked
    )
