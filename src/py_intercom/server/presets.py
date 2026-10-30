"""Server preset persistence and client identity bookkeeping."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from loguru import logger

from ..common.audio import clamp_gain_db
from ..common.buses import (
    FIXED_BUS_IDS,
    FIXED_BUSES,
    PLATEAU_BUS_ID,
    REGIE_BUS_ID,
    require_bus,
)
from ..common.devices import DeviceInfo, list_devices, resolve_device
from ..common.identity import (
    client_id_from_uuid,
    find_client_id_collisions,
)
from ..common.jsonio import JsonFileStatus, read_json_file_result
from ..common.preset_schema import (
    PRESET_SCHEMA_VERSION,
    PresetSchemaError,
    migrate_preset,
    stamp_preset,
)
from .models import AudioBus, ClientProfile, PresetLoadError, PresetOutput, SavedDevice


@dataclass(frozen=True, slots=True)
class ParsedServerPreset:
    """Everything a server preset configures, resolved against one device inventory."""

    return_enabled: bool
    return_device: int | None
    return_saved: SavedDevice
    bus_gain_db: Mapping[int, float]
    bus_feed_to_regie: Mapping[int, bool]
    profiles: Mapping[str, ClientProfile]
    outputs: tuple[PresetOutput, ...]
    # Values corrected or ignored while parsing, one readable line each.
    warnings: tuple[str, ...] = ()


def _section(data: Mapping[str, object], key: str, kind: type, warnings: list[str]):
    value = data.get(key)
    if value is None:
        return kind()
    if not isinstance(value, kind):
        warnings.append(f"{key} ignored: expected a {kind.__name__}, got {type(value).__name__}")
        return kind()
    return value


def _saved_device_index(raw: object, context: str, warnings: list[str]) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        warnings.append(f"{context} device index {raw!r} ignored")
        return None


def _preset_gain_db(raw: object, context: str, warnings: list[str]) -> float:
    """Finite gain within the fader range; a corrected value is reported."""
    gain = clamp_gain_db(raw)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) != gain:
        warnings.append(f"{context} gain {raw!r} corrected to {gain:.1f} dB")
    return gain


def parse_server_preset(data: Mapping[str, object], devices: Sequence[DeviceInfo]) -> ParsedServerPreset:
    """Read a migrated server preset against a device inventory, without side effect.

    The engine loads what this returns and the GUI previews it while the
    server is stopped, so both always agree. A value that cannot be used falls
    back to its default and is reported in ``warnings``. An output whose card
    is absent is kept with ``device`` None, never dropped.
    """
    warnings: list[str] = []
    inventory = list(devices)

    return_saved = SavedDevice(
        index=_saved_device_index(data.get("return_input_device"), "Return input", warnings),
        name=str(data.get("return_input_device_name") or ""),
        hostapi=str(data.get("return_input_device_hostapi") or ""),
    )
    return_device = resolve_device(
        return_saved.name,
        return_saved.hostapi,
        return_saved.index,
        inventory,
        direction="input",
    )

    feed_by_id = {definition.bus_id: bool(definition.default_feed_to_regie) for definition in FIXED_BUSES}
    gain_by_id = {definition.bus_id: 0.0 for definition in FIXED_BUSES}
    for raw_bus_id, raw_bus in _section(data, "buses", dict, warnings).items():
        try:
            bus_id = int(raw_bus_id)
        except (TypeError, ValueError):
            warnings.append(f"bus {raw_bus_id!r} ignored: not a bus id")
            continue
        if bus_id not in FIXED_BUS_IDS or not isinstance(raw_bus, dict):
            warnings.append(f"bus {raw_bus_id!r} ignored: unknown bus or settings not an object")
            continue
        if bus_id != REGIE_BUS_ID:
            feed_by_id[bus_id] = bool(raw_bus.get("feed_to_regie", require_bus(bus_id).default_feed_to_regie))
        gain_by_id[bus_id] = _preset_gain_db(raw_bus.get("gain_db", 0.0), f"bus {bus_id}", warnings)

    profiles: dict[str, ClientProfile] = {}
    for raw_uuid, raw_profile in _section(data, "clients", dict, warnings).items():
        client_uuid = str(raw_uuid or "")
        if not client_uuid:
            warnings.append("client profile without UUID ignored")
            continue
        if not isinstance(raw_profile, dict):
            # The identity is kept: only its settings are lost.
            warnings.append(f"client {client_uuid} settings reset: not an object")
            raw_profile = {}
        profiles[client_uuid] = ClientProfile(
            client_uuid=client_uuid,
            name=str(raw_profile.get("name") or ""),
            input_gain_db=_preset_gain_db(raw_profile.get("input_gain_db", 0.0), f"client {client_uuid}", warnings),
        )

    outputs: list[PresetOutput] = []
    for position, raw_output in enumerate(_section(data, "outputs", list, warnings), start=1):
        context = f"output {position}"
        if not isinstance(raw_output, dict):
            warnings.append(f"{context} ignored: not an object")
            continue
        raw_bus = raw_output.get("bus_id", REGIE_BUS_ID)
        try:
            bus_id = int(raw_bus)
        except (TypeError, ValueError, OverflowError):
            warnings.append(f"{context} ignored: invalid bus {raw_bus!r}")
            continue
        if bus_id not in FIXED_BUS_IDS:
            # Presets older than the fixed buses can name a custom bus.
            # Skipping the output would erase it from the preset at the next
            # save: it is kept on Plateau for the operator to fix.
            warnings.append(f"{context} moved from unknown bus {raw_bus!r} to Plateau")
            bus_id = PLATEAU_BUS_ID
        saved = SavedDevice(
            index=_saved_device_index(raw_output.get("device"), context, warnings),
            name=str(raw_output.get("device_name") or ""),
            hostapi=str(raw_output.get("device_hostapi") or ""),
        )
        outputs.append(
            PresetOutput(
                device=resolve_device(saved.name, saved.hostapi, saved.index, inventory, direction="output"),
                saved=saved,
                bus_id=bus_id,
                gain_db=_preset_gain_db(raw_output.get("gain_db", 0.0), context, warnings),
            )
        )

    return ParsedServerPreset(
        return_enabled=bool(data.get("return_enabled", False)),
        return_device=return_device,
        return_saved=return_saved,
        bus_gain_db=gain_by_id,
        bus_feed_to_regie=feed_by_id,
        profiles=profiles,
        outputs=tuple(outputs),
        warnings=tuple(warnings),
    )


class PresetsMixin:
    def get_preset_paths_snapshot(self) -> dict:
        return {
            "server_preset": str(self._preset_path),
        }

    def _block_preset_writes(self, error: PresetLoadError) -> None:
        with self._preset_status_lock:
            self._preset_write_blocked = True
            self._preset_load_error = error

    def _clear_preset_write_block(self) -> None:
        with self._preset_status_lock:
            self._preset_write_blocked = False
            self._preset_load_error = None

    def _preset_write_error(self) -> PresetLoadError | None:
        with self._preset_status_lock:
            return self._preset_load_error if self._preset_write_blocked else None

    def _refresh_identity_collisions_locked(self) -> None:
        self._identity_collisions = find_client_id_collisions(self._client_profiles)

    def _identity_conflicts_locked(
        self,
        client_id: int,
        client_uuid: str,
    ) -> tuple[str, ...]:
        conflicts = {
            str(known_uuid)
            for known_uuid in self._client_profiles
            if str(known_uuid) != str(client_uuid) and int(client_id_from_uuid(known_uuid)) == int(client_id)
        }
        live = self._clients.get(int(client_id))
        if live is not None and str(live.client_uuid or "") and str(live.client_uuid) != str(client_uuid):
            conflicts.add(str(live.client_uuid))
        return tuple(sorted(conflicts))

    def load_preset(self) -> bool:
        p = self._preset_path
        result = read_json_file_result(p)
        if result.status is JsonFileStatus.MISSING:
            self._preset_loaded_source_version = None
            self._clear_preset_write_block()
            return False
        if result.status is not JsonFileStatus.OK:
            error = PresetLoadError(p, result.status, result.error)
            self._block_preset_writes(error)
            logger.error("{}", error)
            raise error
        try:
            migration = migrate_preset(result.value, preset_type="server")
        except PresetSchemaError as exc:
            error = PresetLoadError(p, JsonFileStatus.INVALID, str(exc))
            self._block_preset_writes(error)
            logger.error("{}", error)
            raise error from exc
        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=True, validate=False)
        except Exception as exc:
            logger.warning("audio device list unavailable while loading the preset: {}", exc)
            devices = []

        # Device names are resolved before taking the engine lock.
        parsed = parse_server_preset(migration.data, devices)
        for warning in parsed.warnings:
            logger.warning("server preset {}: {}", p, warning)
        unresolved_outputs = tuple(output for output in parsed.outputs if output.device is None)
        for output in unresolved_outputs:
            logger.warning("server preset output kept but not opened, its card is absent: {}", output.saved)
        unresolved_return = None
        if parsed.return_device is None and parsed.return_saved.is_set:
            # Kept as saved: the next save must not forget the Return card.
            unresolved_return = parsed.return_saved
            logger.warning("server preset Return input kept but not opened, its card is absent: {}", unresolved_return)
        identity_collisions = find_client_id_collisions(parsed.profiles)

        logger.debug(
            "load_preset: return_enabled={} return_input_device={}", parsed.return_enabled, parsed.return_device
        )
        with self._lock:
            self._buses = {
                definition.bus_id: AudioBus(
                    bus_id=definition.bus_id,
                    name=definition.name,
                    gain_db=float(parsed.bus_gain_db[definition.bus_id]),
                    feed_to_regie=bool(parsed.bus_feed_to_regie[definition.bus_id]),
                )
                for definition in FIXED_BUSES
            }
            self._return_enabled = bool(parsed.return_enabled)
            self._return_input_device = parsed.return_device
            self._unresolved_return = unresolved_return

            replaced_outputs = list(self._outputs.values())
            self._outputs.clear()
            self._next_output_id = 0
            for output in parsed.outputs:
                if output.device is not None:
                    self._add_output_locked(output.device, output.bus_id, output.gain_db)
            self._unresolved_outputs = unresolved_outputs

            # Profiles are persistent configuration only.  Loading them must
            # not allocate codecs, jitter buffers or fake live clients.
            self._client_profiles = {uuid: profile.copy() for uuid, profile in parsed.profiles.items()}
            self._identity_collisions = dict(identity_collisions)

        # Replaced streams stop outside the engine lock: a slow driver never
        # stalls reception or mixing. A running server's supervision opens the
        # loaded outputs.
        for out in replaced_outputs:
            self._close_output_stream(out)

        self._preset_loaded_source_version = int(migration.source_version)
        if migration.migrated:
            logger.info(
                "server preset schema migrated in memory: v{} -> v{} ({})",
                int(migration.source_version),
                int(PRESET_SCHEMA_VERSION),
                p,
            )
        if identity_collisions:
            logger.error(
                "server preset contains {} client identity collision(s): {}",
                len(identity_collisions),
                identity_collisions,
            )

        self._clear_preset_write_block()
        return True

    def _build_preset_data(self) -> dict:
        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=False, validate=False)
        except Exception as exc:
            logger.warning("audio device list unavailable while saving the preset: {}", exc)
            devices = []
        device_map = {int(d.index): d for d in devices}

        with self._lock:
            output_states = [
                {
                    "device": int(o.device),
                    "bus_id": int(o.bus_id),
                    "gain_db": float(o.gain_db),
                }
                for o in self._outputs.values()
            ]
            return_input_device = self._return_input_device
            return_enabled = bool(self._return_enabled)
            unresolved_outputs = self._unresolved_outputs
            unresolved_return = self._unresolved_return
            bus_snapshot = {
                definition.bus_id: AudioBus(
                    bus_id=definition.bus_id,
                    name=definition.name,
                    gain_db=float(self._buses[definition.bus_id].gain_db),
                    feed_to_regie=bool(self._buses[definition.bus_id].feed_to_regie),
                )
                for definition in FIXED_BUSES
            }
            profiles = {uuid: profile.copy() for uuid, profile in self._client_profiles.items()}
            for st in self._clients.values():
                u = str(st.client_uuid or "")
                if not u:
                    continue
                previous = profiles.get(u)
                profiles[u] = ClientProfile(
                    client_uuid=u,
                    name=str(st.name or (previous.name if previous is not None else "")),
                    input_gain_db=float(st.input_gain_db),
                )
            self._client_profiles = {uuid: profile.copy() for uuid, profile in profiles.items()}

        clients = {uuid: profile.to_preset() for uuid, profile in profiles.items()}

        outputs = []
        for o in output_states:
            dev_info = device_map.get(int(o["device"]))
            outputs.append(
                {
                    "device": int(o["device"]),
                    "device_name": str(getattr(dev_info, "name", "")) if dev_info is not None else "",
                    "device_hostapi": str(getattr(dev_info, "hostapi", "")) if dev_info is not None else "",
                    "bus_id": int(o["bus_id"]),
                    "gain_db": float(o["gain_db"]),
                }
            )
        # Outputs whose card was absent at loading are saved back unchanged.
        outputs.extend(output.to_preset() for output in unresolved_outputs)

        if return_input_device is not None:
            return_dev = device_map.get(int(return_input_device))
            return_saved = SavedDevice(
                index=int(return_input_device),
                name=str(getattr(return_dev, "name", "")) if return_dev is not None else "",
                hostapi=str(getattr(return_dev, "hostapi", "")) if return_dev is not None else "",
            )
        else:
            # A Return card absent since loading keeps its saved identity.
            return_saved = unresolved_return or SavedDevice()

        buses: dict[str, dict] = {
            str(definition.bus_id): {
                "name": definition.name,
                "feed_to_regie": (
                    False if definition.bus_id == REGIE_BUS_ID else bool(bus_snapshot[definition.bus_id].feed_to_regie)
                ),
                "gain_db": float(bus_snapshot[definition.bus_id].gain_db),
            }
            for definition in FIXED_BUSES
        }

        server_data = {
            "outputs": outputs,
            "buses": buses,
            "clients": clients,
            "return_enabled": bool(return_enabled),
            "return_input_device": return_saved.index,
            "return_input_device_name": return_saved.name,
            "return_input_device_hostapi": return_saved.hostapi,
        }

        return stamp_preset(server_data, preset_type="server")

    def save_preset(self) -> None:
        error = self._preset_write_error()
        if error is not None:
            raise error
        self._preset_writer.request()
        self._preset_writer.flush(raise_on_error=True)

    def _autosave_preset(self) -> None:
        error = self._preset_write_error()
        if error is not None:
            logger.warning(
                "server preset autosave blocked because the loaded file is invalid: {}",
                error.path,
            )
            return
        try:
            self._preset_writer.request()
        except Exception as e:
            logger.warning("autosave preset scheduling failed: {}", e)
