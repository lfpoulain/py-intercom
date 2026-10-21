"""Server preset persistence and client identity bookkeeping."""

from __future__ import annotations

from loguru import logger

from ..common.buses import (
    FIXED_BUS_IDS,
    FIXED_BUSES,
    REGIE_BUS_ID,
    require_bus,
)
from ..common.devices import list_devices, resolve_device
from ..common.identity import (
    client_id_from_uuid,
    find_client_id_collisions,
)
from ..common.jsonio import JsonFileStatus, read_json_file_result
from ..common.logging import best_effort
from ..common.preset_schema import (
    PRESET_SCHEMA_VERSION,
    PresetSchemaError,
    migrate_preset,
    stamp_preset,
)
from .models import AudioBus, ClientProfile, OutputState, PresetLoadError


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
            if str(known_uuid) != str(client_uuid)
            and int(client_id_from_uuid(known_uuid)) == int(client_id)
        }
        live = self._clients.get(int(client_id))
        if (
            live is not None
            and str(live.client_uuid or "")
            and str(live.client_uuid) != str(client_uuid)
        ):
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
        data = migration.data

        outputs = data.get("outputs") if isinstance(data.get("outputs"), list) else []
        buses = data.get("buses") if isinstance(data.get("buses"), dict) else {}
        clients = data.get("clients") if isinstance(data.get("clients"), dict) else {}
        profiles = {
            str(client_uuid): ClientProfile.from_preset(str(client_uuid), value)
            for client_uuid, value in clients.items()
            if str(client_uuid or "")
        }
        identity_collisions = find_client_id_collisions(profiles)
        return_enabled = bool(data.get("return_enabled", False))
        return_input_device = data.get("return_input_device")
        try:
            return_input_device = int(return_input_device) if return_input_device is not None else None
        except Exception:
            return_input_device = None
        return_input_device_name = str(data.get("return_input_device_name") or "") if isinstance(data, dict) else ""
        return_input_device_hostapi = str(data.get("return_input_device_hostapi") or "") if isinstance(data, dict) else ""

        try:
            devices = list_devices(hostapi_substring=None, hard_refresh=True, validate=False)
        except Exception:
            devices = []

        return_input_device = resolve_device(
            return_input_device_name,
            return_input_device_hostapi,
            return_input_device,
            devices,
            direction="input",
        )

        feed_by_id = {
            definition.bus_id: bool(definition.default_feed_to_regie)
            for definition in FIXED_BUSES
        }
        gain_by_id = {definition.bus_id: 0.0 for definition in FIXED_BUSES}
        if isinstance(buses, dict):
            for bus_id_str, b in buses.items():
                try:
                    bid = int(bus_id_str)
                except (TypeError, ValueError):
                    continue
                if bid not in FIXED_BUS_IDS or not isinstance(b, dict):
                    continue
                definition = require_bus(bid)
                if int(bid) != int(REGIE_BUS_ID):
                    feed_by_id[int(bid)] = bool(
                        b.get("feed_to_regie", definition.default_feed_to_regie)
                    )
                try:
                    gain_by_id[int(bid)] = float(b.get("gain_db", 0.0))
                except Exception:
                    gain_by_id[int(bid)] = 0.0

        logger.debug("load_preset: return_enabled={} return_input_device={}", bool(return_enabled), return_input_device)
        with self._lock:
            self._buses = {
                definition.bus_id: AudioBus(
                    bus_id=definition.bus_id,
                    name=definition.name,
                    gain_db=float(gain_by_id[definition.bus_id]),
                    feed_to_regie=(
                        False
                        if definition.bus_id == REGIE_BUS_ID
                        else bool(feed_by_id[definition.bus_id])
                    ),
                )
                for definition in FIXED_BUSES
            }
            self._return_enabled = bool(return_enabled)
            self._return_input_device = return_input_device

            if isinstance(outputs, list):
                fallback = self._default_output_bus_id()
                for out in list(self._outputs.values()):
                    with best_effort("close replaced output stream"):
                        if out.stream is not None:
                            out.stream.stop()
                            out.stream.close()
                self._outputs.clear()
                self._next_output_id = 0
                for o in outputs:
                    if not isinstance(o, dict):
                        continue
                    try:
                        bid = int(o.get("bus_id", 0))
                    except (TypeError, ValueError):
                        continue
                    if int(bid) not in FIXED_BUS_IDS:
                        bid = int(fallback)

                    dev = None
                    try:
                        raw_dev = o.get("device")
                        dev = int(raw_dev) if raw_dev is not None else None
                    except Exception:
                        dev = None
                    dev_name = str(o.get("device_name") or "")
                    dev_hostapi = str(o.get("device_hostapi") or "")
                    dev = resolve_device(
                        dev_name,
                        dev_hostapi,
                        dev,
                        devices,
                        direction="output",
                    )
                    if dev is None:
                        continue

                    oid = int(self._next_output_id)
                    self._next_output_id += 1
                    try:
                        output_gain_db = float(o.get("gain_db", 0.0))
                    except Exception:
                        output_gain_db = 0.0
                    self._outputs[oid] = OutputState(output_id=oid, device=int(dev), bus_id=bid, gain_db=float(output_gain_db))

            # Profiles are persistent configuration only.  Loading them must
            # not allocate codecs, jitter buffers or fake live clients.
            self._client_profiles = {uuid: profile.copy() for uuid, profile in profiles.items()}
            self._identity_collisions = dict(identity_collisions)

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
        except Exception:
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
            bus_snapshot = {
                definition.bus_id: self._buses.get(
                    definition.bus_id,
                    AudioBus(
                        definition.bus_id,
                        definition.name,
                        feed_to_regie=definition.default_feed_to_regie,
                    ),
                )
                for definition in FIXED_BUSES
            }
            profiles = {
                uuid: profile.copy()
                for uuid, profile in self._client_profiles.items()
            }
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
            self._client_profiles = {
                uuid: profile.copy()
                for uuid, profile in profiles.items()
            }

        clients = {
            uuid: profile.to_preset()
            for uuid, profile in profiles.items()
        }

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

        return_dev = device_map.get(int(return_input_device)) if return_input_device is not None else None

        buses: dict[str, dict] = {
            str(definition.bus_id): {
                "name": definition.name,
                "feed_to_regie": (
                    False
                    if definition.bus_id == REGIE_BUS_ID
                    else bool(bus_snapshot[definition.bus_id].feed_to_regie)
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
            "return_input_device": int(return_input_device) if return_input_device is not None else None,
            "return_input_device_name": str(getattr(return_dev, "name", "")) if return_dev is not None else "",
            "return_input_device_hostapi": str(getattr(return_dev, "hostapi", "")) if return_dev is not None else "",
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
