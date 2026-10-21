from __future__ import annotations

from py_intercom.client.gui import (
    ClientWindow,
    _preset_requires_all_devices,
    new_client_preset,
)
from py_intercom.common.devices import DeviceInfo, format_device_label


class _FakeCombo:
    """Minimal QComboBox model, including Qt's implicit first selection."""

    def __init__(self) -> None:
        self.items: list[tuple[str, object]] = []
        self.current_index = -1

    def blockSignals(self, _blocked: bool) -> None:  # noqa: N802
        return

    def clear(self) -> None:
        self.items.clear()
        self.current_index = -1

    def addItem(self, label: str, data: object) -> None:  # noqa: N802
        self.items.append((str(label), data))
        if self.current_index < 0:
            self.current_index = 0

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        self.current_index = int(index)

    def currentData(self):  # noqa: N802
        if 0 <= self.current_index < len(self.items):
            return self.items[self.current_index][1]
        return None

    def findData(self, expected) -> int:  # noqa: N802
        for index, (_label, data) in enumerate(self.items):
            if data == expected:
                return index
        return -1


class _FakeButton:
    def __init__(self) -> None:
        self.enabled = True
        self.tooltip = ""

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        self.enabled = bool(enabled)

    def setToolTip(self, text: str) -> None:  # noqa: N802
        self.tooltip = str(text)


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:  # noqa: N802
        self.text = str(text)


class _FakeWindow:
    def __init__(self, preset: dict) -> None:
        self._preset = dict(preset)
        self._device_refresh_timed_out_flag = False
        self._last_device_error = ""
        self._last_device_count = 0
        self._device_cache: dict[int, object] = {}
        self._device_thread = None
        self._connected = False
        self._input_device = _FakeCombo()
        self._output_device = _FakeCombo()
        self._connect_btn = _FakeButton()
        self._preflight_btn = _FakeButton()
        self._device_status = _FakeLabel()
        self.refresh_requests = 0

    def _preset_get(self, key: str, default=None):
        return self._preset.get(key, default)

    def _preset_set(self, key: str, value) -> None:
        self._preset[str(key)] = value

    def _update_device_selection_state(self) -> None:
        ClientWindow._update_device_selection_state(self)

    def _start_device_refresh(self) -> None:
        self.refresh_requests += 1


def _refresh(window: _FakeWindow, devices: list[DeviceInfo]) -> None:
    ClientWindow._on_devices_refreshed(window, devices, "")


def test_non_wasapi_preset_automatically_requires_full_inventory() -> None:
    assert _preset_requires_all_devices(
        {
            "input_device_hostapi": "Windows DirectSound",
            "output_device_hostapi": "Windows WASAPI",
        }
    )
    assert not _preset_requires_all_devices(
        {
            "input_device_hostapi": "Windows WASAPI",
            "output_device_hostapi": "Windows WASAPI",
        }
    )
    assert new_client_preset("uuid")["show_all_devices"] is False


def test_show_all_toggle_is_persisted_before_refresh() -> None:
    window = _FakeWindow({"show_all_devices": False})

    ClientWindow._on_show_all_devices_changed(window, 2)

    assert window._preset["show_all_devices"] is True
    assert window.refresh_requests == 1


def test_duplicate_bluetooth_twins_restore_by_direction_and_show_the_api() -> None:
    twin_name = "Casque (G435 Hands-Free)"
    devices = [
        DeviceInfo(
            index=92,
            name=twin_name,
            hostapi="Windows WDM-KS",
            max_input_channels=0,
            max_output_channels=1,
            input_compatible=False,
            output_compatible=True,
        ),
        DeviceInfo(
            index=93,
            name=twin_name,
            hostapi="Windows WDM-KS",
            max_input_channels=1,
            max_output_channels=0,
            input_compatible=True,
            output_compatible=False,
        ),
    ]
    window = _FakeWindow(
        {
            "input_device": 93,
            "input_device_name": twin_name,
            "input_device_hostapi": "Windows WDM-KS",
            "output_device": 92,
            "output_device_name": twin_name,
            "output_device_hostapi": "Windows WDM-KS",
        }
    )

    _refresh(window, devices)

    assert window._input_device.currentData() == 93
    assert window._output_device.currentData() == 92
    assert window._connect_btn.enabled is True
    assert window._input_device.items == [
        (f"{twin_name} — Windows WDM-KS — #93", 93)
    ]
    assert window._output_device.items == [
        (f"{twin_name} — Windows WDM-KS — #92", 92)
    ]


def test_direction_compatibility_filters_each_selector_independently() -> None:
    devices = [
        DeviceInfo(
            index=4,
            name="Duplex partiel",
            hostapi="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=2,
            input_compatible=False,
            output_compatible=True,
        ),
        DeviceInfo(
            index=5,
            name="Micro valide",
            hostapi="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            input_compatible=True,
            output_compatible=False,
        ),
    ]
    window = _FakeWindow({})

    _refresh(window, devices)

    assert [data for _label, data in window._input_device.items] == [5]
    assert [data for _label, data in window._output_device.items] == [4]


def test_missing_saved_devices_never_select_the_first_entries_implicitly() -> None:
    devices = [
        DeviceInfo(
            index=7,
            name="Micro de secours",
            hostapi="Windows WASAPI",
            max_input_channels=1,
            max_output_channels=0,
            input_compatible=True,
            output_compatible=False,
        ),
        DeviceInfo(
            index=8,
            name="Sortie de secours",
            hostapi="Windows WASAPI",
            max_input_channels=0,
            max_output_channels=2,
            input_compatible=False,
            output_compatible=True,
        ),
    ]
    window = _FakeWindow(
        {
            "input_device": 70,
            "input_device_name": "Micro disparu",
            "input_device_hostapi": "Windows WASAPI",
            "output_device": 80,
            "output_device_name": "Sortie disparue",
            "output_device_hostapi": "Windows WASAPI",
        }
    )

    _refresh(window, devices)

    assert window._input_device.currentData() is None
    assert window._output_device.currentData() is None
    assert window._connect_btn.enabled is False
    assert "microphone et casque" in window._device_status.text


def test_device_label_disambiguates_name_hostapi_and_index() -> None:
    device = DeviceInfo(
        index=21,
        name="Casque",
        hostapi="Windows DirectSound",
        max_input_channels=1,
        max_output_channels=0,
    )

    assert format_device_label(device) == "Casque — Windows DirectSound — #21"
