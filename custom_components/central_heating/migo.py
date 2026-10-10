"""Narrow optional miGo bridge. Uses runtime API, never copies credentials.

Native schedule transport is supplied only after its protocol is verified. An
unsupported installation stays read-only, with an explicit blocked status.
"""

import asyncio
from copy import deepcopy
from dataclasses import fields
from datetime import datetime
from time import time
from zoneinfo import ZoneInfo

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .comfort import ComfortMemory, ComfortSettings, Correction, validate_schedule, weighted_average
from .logic import temperature
from .schedule import build_native, canonical, checked, native_entries, request_schedule, unrelated_schedules, validate_native


class MigoBridge:
    def __init__(self, controller):
        self.controller = controller
        self.hass = controller.hass
        self.settings = ComfortSettings()
        self.memory = ComfortMemory()
        self.schedule = [{"day": 0, "time": "00:00", "target": 22.0}]
        self.correction = Correction()
        self.snapshot = {}
        self.fault = None
        self.last_poll = None
        self.native_room = None
        self.native_home = None
        self.config_home = None
        self.last_config_poll = None
        self.last_attempt = None
        self._lock = asyncio.Lock()
        self.discovery = []

    def load(self, data):
        try:
            settings = ComfortSettings(**data.get("settings", {}))
            settings.validate(self.water_entities)
            if any(entity in self._reference_aliases(settings.migo_entity) and weight > 0 for entity, weight in settings.room_sensors.items()):
                raise ValueError("Stored reference alias included in average")
            schedule = validate_schedule(data.get("schedule", self.schedule))
            memory = ComfortMemory(**data.get("memory", {}))
            if memory.owner not in ("miGo", "HA", "Release pending"):
                raise ValueError("Invalid stored ownership")
            for key in ("manual_target", "manual_expires_at", "last_command_at", "last_command", "native_expires_at"):
                value = getattr(memory, key)
                if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or temperature(value) is None):
                    raise ValueError("Invalid stored controller memory")
            if memory.manual_target is not None and not 7 <= memory.manual_target <= 30:
                raise ValueError("Invalid manual target")
            if memory.confirmed_schedule:
                memory.confirmed_schedule = validate_schedule(memory.confirmed_schedule)
            for key in ("schedule_version", "confirmed_version"):
                if not isinstance(getattr(memory, key), int) or isinstance(getattr(memory, key), bool) or getattr(memory, key) < 0:
                    raise ValueError("Invalid schedule version")
            if memory.confirmed_version > memory.schedule_version:
                raise ValueError("Invalid confirmed version")
            for key in ("native_schedule", "pending_sync", "pending_create"):
                if not isinstance(getattr(memory, key), dict):
                    raise ValueError("Invalid stored schedule state")
            for key in ("managed_schedule_id", "managed_name", "native_timezone"):
                if not isinstance(getattr(memory, key), str):
                    raise ValueError("Invalid managed schedule identity")
            if memory.native_timezone:
                ZoneInfo(memory.native_timezone)
            if memory.owner == "HA" and (not memory.confirmed_schedule or not memory.native_schedule or not memory.managed_schedule_id or memory.duration_confirmed is not True):
                raise ValueError("Stored ownership lacks verified fallback")
            if memory.pending_sync:
                pending = memory.pending_sync
                if set(pending) != {"payload", "unrelated", "selected", "default", "schedule", "version"} or not isinstance(pending["payload"], dict) or not isinstance(pending["unrelated"], dict) or not isinstance(pending["version"], int) or not 0 <= pending["version"] <= memory.schedule_version:
                    raise ValueError("Invalid pending sync state")
                validate_schedule(pending["schedule"])
            if memory.pending_create and (set(memory.pending_create) != {"payload", "unrelated"} or not isinstance(memory.pending_create["payload"], dict) or not isinstance(memory.pending_create["unrelated"], dict)):
                raise ValueError("Invalid pending schedule creation")
            if not isinstance(memory.pending_command, bool):
                raise ValueError("Invalid pending native command")
            if memory.pending_command:
                # A crash during a write cannot prove its target/expiry signature.
                memory.owner = "miGo"
                memory.last_command = memory.native_expires_at = None
                self.fault = "Unconfirmed native command across restart; miGo retains ownership"
            self.settings, self.schedule, self.memory = settings, schedule, memory
        except (TypeError, ValueError, AttributeError, KeyError):
            self.fault = "Invalid stored comfort settings; miGo retains ownership"

    @property
    def water_entities(self):
        entities = {zone["sensor"] for zone in self.controller.zones.values()}
        for entity in er.async_get(self.hass).entities.values():
            if getattr(entity, "platform", None) == "central_heating" and str(getattr(entity, "unique_id", "")).endswith("_water_temperature"):
                entities.add(entity.entity_id)
        return entities | {f"sensor.central_heating_{key}_water_temperature" for key in self.controller.zones}

    @property
    def watched_entities(self):
        return set(self.settings.room_sensors) | {self.settings.migo_entity}

    @property
    def reference_aliases(self):
        return self._reference_aliases(self.settings.migo_entity)

    def _reference_aliases(self, entity_id):
        registry = er.async_get(self.hass)
        selected = registry.async_get(entity_id)
        entry_id = getattr(selected, "config_entry_id", None)
        return {entity.entity_id for entity in registry.entities.values()
                if entry_id and getattr(entity, "config_entry_id", None) == entry_id and getattr(entity, "platform", None) == "migo_netatmo"}

    def serialize(self):
        return {"settings": self.settings.serialize(), "schedule": deepcopy(self.schedule), "memory": self.memory.serialize()}

    def _api(self):
        entity = er.async_get(self.hass).async_get(self.settings.migo_entity)
        entry_id = getattr(entity, "config_entry_id", None)
        entries = getattr(self.hass, "config_entries", None)
        entry = entries.async_get_entry(entry_id) if entries and entry_id else None
        if entry is None or entry.domain != "migo_netatmo":
            raise ValueError("Select the installed migo_netatmo climate entity")
        runtime = entry.runtime_data
        api = getattr(runtime, "api", None)
        required = ("get_homes_data", "get_home_status", "set_room_state", "set_manual_setpoint_duration")
        if api is None or not all(callable(getattr(api, key, None)) for key in required):
            raise ValueError("Unsupported miGo runtime API")
        return api

    async def _read(self, api):
        """Never trust cached or optimistic coordinator state for confirmation."""
        response = checked(await api.get_home_status(self.settings.home_id))
        home = response.get("body", {}).get("home", {})
        if home.get("id") != self.settings.home_id:
            raise ValueError("miGo returned a different home")
        room = next((room for room in home.get("rooms", []) if room.get("id") == self.settings.room_id), None)
        if room is None:
            raise ValueError("miGo room not found")
        self.native_room, self.native_home, self.last_poll = room, home, time()
        return room

    async def _read_config(self, api):
        response = checked(await api.get_homes_data())
        home = next((home for home in response.get("body", {}).get("homes", []) if home.get("id") == self.settings.home_id), None)
        if home is None:
            raise ValueError("miGo home not found")
        if self.settings.room_id not in {room.get("id") for room in home.get("rooms", [])}:
            raise ValueError("Configured miGo room does not belong to home")
        self.config_home, self.last_config_poll = home, time()
        return home

    async def configure(self, values):
        async with self._lock:
            unknown = set(values) - {field.name for field in fields(ComfortSettings)}
            if unknown:
                raise ServiceValidationError("Unknown comfort setting")
            try:
                settings = ComfortSettings(**{**self.settings.serialize(), **values})
                settings.validate(self.water_entities)
                if any(entity in self._reference_aliases(settings.migo_entity) and weight > 0 for entity, weight in settings.room_sensors.items()):
                    raise ValueError("miGo reference aliases cannot contribute to the average")
            except (TypeError, ValueError) as err:
                raise ServiceValidationError(str(err)) from err
            mapping = ("migo_entity", "home_id", "room_id", "schedule_id")
            if self.memory.owner != "miGo" and any(getattr(settings, key) != getattr(self.settings, key) for key in mapping):
                raise ServiceValidationError("Relinquish control before changing miGo mapping")
            if settings.manual_minutes != self.settings.manual_minutes and self.memory.owner == "HA":
                await self._set_duration(self._api(), settings.manual_minutes)
            if any(getattr(settings, key) != getattr(self.settings, key) for key in mapping):
                self.memory = ComfortMemory()
                self.native_room = self.native_home = self.config_home = None
                self.last_poll = self.last_config_poll = self.last_attempt = None
            if self.memory.owner == "HA" and self.native_room and not settings.minimum <= self.native_room.get("therm_setpoint_temperature", 0) <= settings.maximum:
                self.memory.owner = "Release pending"
                await self._save()
                await self._release(self._api())
            self.settings = settings
            self.correction.reset()
            self.fault = None
            await self._save()
        await self.controller.async_reconcile_now()

    async def _set_duration(self, api, minutes):
        checked(await api.set_manual_setpoint_duration(self.settings.home_id, minutes))
        home = await self._read_config(api)
        if home.get("therm_setpoint_default_duration") != minutes:
            raise ValueError("Shared native duration read-back mismatch")
        self.memory.duration_confirmed = True

    async def discover(self):
        async with self._lock:
            response = checked(await self._api().get_homes_data())
            self.discovery = [{"id": home["id"], "name": home.get("name", home["id"]), "timezone": home.get("timezone"),
                               "rooms": [{"id": room["id"], "name": room.get("name", room["id"])} for room in home.get("rooms", [])],
                               "schedules": [{"id": item["id"], "name": item.get("name", item["id"])} for item in home.get("schedules", []) if item.get("type") == "therm"]}
                              for home in response.get("body", {}).get("homes", [])]
            self.snapshot["discovery"] = deepcopy(self.discovery)
        self.controller._publish()

    async def prepare_schedule(self):
        """Explicit action creates a dedicated clone; never overwrites its source."""
        async with self._lock:
            if self.memory.owner != "miGo":
                raise ServiceValidationError("Relinquish before preparing a native schedule")
            api = self._api()
            home = await self._read_config(api)
            if self.memory.managed_schedule_id:
                self._managed(home)
                return  # Preparing twice must not import over saved HA edits.
            if not home.get("timezone"):
                raise ServiceValidationError("Native home timezone unavailable")
            ZoneInfo(home["timezone"])
            source = next((item for item in home.get("schedules", []) if item.get("id") == self.settings.schedule_id), None)
            if source is None:
                raise ServiceValidationError("Choose the native thermal schedule to copy")
            validate_native(source, home, self.settings.room_id)
            name = f"Central Heating HA {self.controller.entry.entry_id}"
            # Persist identification before creating: a crash cannot create duplicates.
            self.memory.managed_name = name
            expected = {**canonical(source), "name": name}
            if not self.memory.pending_create:
                self.memory.pending_create = {"payload": deepcopy(expected), "unrelated": unrelated_schedules(home, "")}
            await self._save()
            matches = [item for item in home.get("schedules", []) if item.get("name") == name and item.get("type") == "therm"]
            if not matches:
                payload = {"home_id": self.settings.home_id, "name": name, **canonical(source)}
                payload["name"] = name
                await request_schedule(api, "createnewhomeschedule", payload)
                home = await self._read_config(api)
                matches = [item for item in home.get("schedules", []) if item.get("name") == name and item.get("type") == "therm"]
            if len(matches) != 1 or not matches[0].get("id"):
                raise ServiceValidationError("Native schedule creation not confirmed uniquely")
            managed = matches[0]
            expected = self.memory.pending_create["payload"]
            if canonical(managed) != expected:
                raise ServiceValidationError("Managed schedule differs from source; inspect it natively before retrying")
            if unrelated_schedules(home, managed["id"]) != self.memory.pending_create["unrelated"]:
                raise ServiceValidationError("Original/unrelated native schedule changed during creation; inspect natively")
            self.memory.managed_schedule_id = managed["id"]
            self.memory.native_schedule = canonical(managed)
            self.memory.native_timezone = home["timezone"]
            self.schedule = native_entries(managed, self.settings.room_id)
            self.memory.schedule_version += 1
            self.memory.confirmed_version = self.memory.schedule_version
            self.memory.confirmed_schedule = deepcopy(self.schedule)
            self.memory.pending_create = {}
            self.fault = None
            await self._save()
        await self.controller.async_reconcile_now()

    def _managed(self, home):
        if home.get("timezone") != self.memory.native_timezone:
            raise ValueError("Native home timezone changed; inspect schedule before re-enabling")
        managed = next((item for item in home.get("schedules", []) if item.get("id") == self.memory.managed_schedule_id), None)
        if managed is None or managed.get("name") != self.memory.managed_name:
            raise ValueError("Managed native schedule missing or renamed")
        if canonical(managed) != self.memory.native_schedule:
            raise ValueError("Native schedule changed externally; not overwritten")
        return managed

    async def _sync_schedule(self, api):
        home = await self._read_config(api)
        pending = self.memory.pending_sync
        if pending:
            candidate = next((item for item in home.get("schedules", []) if item.get("id") == self.memory.managed_schedule_id), None)
            if candidate and canonical(candidate) == pending["payload"]:
                unrelated = unrelated_schedules(home, self.memory.managed_schedule_id)
                if unrelated != pending["unrelated"] or candidate.get("selected") != pending["selected"] or candidate.get("default") != pending["default"] or candidate.get("type") != "therm":
                    raise ValueError("Unrelated schedule changed; pending sync requires inspection")
                self.memory.native_schedule = deepcopy(pending["payload"])
                self.memory.confirmed_schedule = deepcopy(pending["schedule"])
                self.memory.confirmed_version = pending["version"]
                self.memory.pending_sync = {}
                await self._save()
                if self.memory.confirmed_version == self.memory.schedule_version:
                    self.fault = None
                    return
        managed = self._managed(home)
        payload = build_native(self.schedule, managed, home, self.settings.room_id)
        # Avoid accumulating unused generated custom zones on each edit.
        payload["zones"] = [zone for zone in payload["zones"] if zone.get("type") != 4 or not str(zone.get("name", "")).startswith("HA ") or zone["id"] in {slot["zone_id"] for slot in payload["timetable"]}]
        before = unrelated_schedules(home, managed["id"])
        expected = {"name": managed["name"], **payload}
        self.memory.pending_sync = {"payload": expected, "unrelated": before, "selected": managed.get("selected"), "default": managed.get("default"), "schedule": deepcopy(self.schedule), "version": self.memory.schedule_version}
        await self._save()
        await request_schedule(api, "synchomeschedule", {"home_id": self.settings.home_id, "schedule_id": managed["id"], "name": managed["name"], **payload})
        after = await self._read_config(api)
        confirmed = next((item for item in after.get("schedules", []) if item.get("id") == managed["id"]), None)
        if confirmed is None or canonical(confirmed) != expected or native_entries(confirmed, self.settings.room_id) != self.schedule:
            raise ValueError("Native schedule read-back mismatch; pending")
        if before != unrelated_schedules(after, managed["id"]) or confirmed.get("selected") != managed.get("selected") or confirmed.get("default") != managed.get("default") or confirmed.get("type") != "therm":
            raise ValueError("Unrelated native schedule changed during sync; inspect natively")
        self.memory.native_schedule = expected
        self.memory.confirmed_schedule = deepcopy(self.schedule)
        self.memory.confirmed_version = self.memory.schedule_version
        self.memory.pending_sync = {}
        self.fault = None

    async def set_schedule(self, entries):
        if not self.memory.managed_schedule_id:
            raise ServiceValidationError("Prepare the dedicated native schedule copy before saving; your draft has not been changed")
        try:
            schedule = validate_schedule(entries)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        async with self._lock:
            self.schedule = schedule
            self.memory.schedule_version += 1
            self.fault = "Native schedule sync pending; correction paused"
            await self._save()
            try:
                async with asyncio.timeout(30):
                    await self._sync_schedule(self._api())
            except Exception as err:
                self.fault = str(err)
            await self._save()
        await self.controller.async_reconcile_now()

    async def retry_schedule(self):
        async with self._lock:
            try:
                async with asyncio.timeout(30):
                    await self._sync_schedule(self._api())
            except Exception as err:
                self.fault = str(err)
                raise ServiceValidationError(self.fault) from err
            finally:
                await self._save()
        await self.controller.async_reconcile_now()

    async def set_manual(self, target, minutes):
        if not self.settings.migo_entity:
            raise ServiceValidationError("Configure miGo before using whole-home timed targets")
        target, minutes = temperature(target), temperature(minutes)
        if target is None or not 7 <= target <= 30 or target * 2 % 1 or minutes is None or not 1 <= minutes <= 720:
            raise ServiceValidationError("Choose a 7–30°C target and 1–720 minute duration")
        async with self._lock:
            self.memory.manual_target = target
            self.memory.manual_expires_at = time() + minutes * 60
            self.correction.reset()
            await self._save()
        await self.controller.async_reconcile_now()

    async def cancel_manual(self):
        async with self._lock:
            self.memory.manual_target = self.memory.manual_expires_at = None
            self.correction.reset()
            await self._save()
        await self.controller.async_reconcile_now()

    async def enable(self):
        async with self._lock:
            if not self.memory.confirmed_schedule or self.memory.confirmed_version != self.memory.schedule_version:
                raise ServiceValidationError("Confirm the real native fallback schedule before enabling HA correction")
            api = self._api()
            home = await self._read_config(api)
            managed = self._managed(home)
            if not home.get("timezone"):
                raise ServiceValidationError("Native home timezone unavailable; cannot safely align schedules")
            ZoneInfo(home["timezone"])
            if home.get("therm_mode") != "schedule" or home.get("temperature_control_mode", "heating") != "heating":
                raise ServiceValidationError("Exit native away/frost/DHW mode before enabling")
            if not managed.get("selected"):
                checked(await api.switch_home_schedule(self.settings.home_id, self.memory.managed_schedule_id))
                home = await self._read_config(api)
                if not self._managed(home).get("selected"):
                    raise ServiceValidationError("Managed fallback schedule selection not confirmed")
            await self._set_duration(api, self.settings.manual_minutes)
            await self._read(api)
            if self.native_room.get("therm_setpoint_mode") not in ("home", "schedule"):
                raise ServiceValidationError("Cancel native override before enabling HA")
            actual = temperature(self.native_room.get("therm_setpoint_temperature"))
            if actual is None or not self.settings.minimum <= actual <= self.settings.maximum:
                raise ServiceValidationError("Set the native miGo target within output bounds before enabling; HA will not bypass the 0.5°C slew limit")
            self.memory.last_command = None
            self.memory.native_expires_at = None
            self.memory.pending_command = False
            self.memory.owner = "HA"
            self.fault = None
            await self._save()
        await self.controller.async_reconcile_now()

    async def relinquish(self):
        async with self._lock:
            # Save intent before the network call: a restart retries cancellation.
            self.memory.owner = "Release pending"
            await self._save()
            try:
                await self._release(self._api())
                self.fault = None
            except Exception as err:
                self.fault = f"Native cancellation not confirmed: {err}"
                raise ServiceValidationError(self.fault) from err
            finally:
                await self._save()
        await self.controller.async_reconcile_now()

    async def _release(self, api):
        current = await self._read(api)
        if current.get("therm_setpoint_mode") != "manual" or self.memory.last_command is None or self.memory.native_expires_at is None or temperature(current.get("therm_setpoint_temperature")) != self.memory.last_command or temperature(current.get("therm_setpoint_end_time")) != self.memory.native_expires_at:
            self.memory.owner = "miGo"
            self.memory.native_expires_at = None
            self.correction.reset()
            return  # Native user state is not ours to cancel.
        checked(await api.set_room_state(self.settings.home_id, self.settings.room_id, mode="home"))
        room = await self._read(api)
        if room.get("therm_setpoint_mode") not in ("home", "schedule") or room.get("therm_setpoint_end_time"):
            raise ValueError("Room manual override still active")
        self.memory.owner = "miGo"
        self.memory.native_expires_at = None
        self.correction.reset()

    def _physical_reference(self, now):
        """Only the room's thermostat RF evidence counts, never gateway heartbeat."""
        room = next((room for room in self.config_home.get("rooms", []) if room.get("id") == self.settings.room_id), {})
        ids = {module["id"] for module in self.config_home.get("modules", [])
               if module.get("type") == "NAThermVaillant" and (module.get("room_id") == self.settings.room_id or module.get("id") in room.get("module_ids", []))}
        observed = False
        for module in self.native_home.get("modules", []):
            if module.get("id") not in ids:
                continue
            if module.get("reachable") is False:
                return False, "Native thermostat RF unavailable"
            for key in ("last_seen", "last_message"):
                stamp = temperature(module.get(key))
                if stamp is not None and stamp > 0:
                    observed = True
                    if not -60 <= now - stamp <= self.settings.freshness_seconds:
                        return False, "Native thermostat RF report stale"
        return True, "Native RF report checked" if observed else "Cloud poll and room availability checked; physical report timestamp unavailable"

    async def _save(self):
        data = self.controller._serialize()
        await self.controller._store.async_save(data)
        self.controller._saved = data

    async def reconcile(self):
        async with self._lock:
            async with asyncio.timeout(30):
                await self._reconcile()

    async def _reconcile(self):
        now = time()
        local = datetime.fromtimestamp(now, ZoneInfo(self.memory.native_timezone or getattr(self.hass.config, "time_zone", "Europe/Warsaw")))
        target, source = self.memory.target(self.schedule, now, local)
        if self.settings.migo_entity or self.memory.schedule_version:
            self.controller.settings.target = target
        else:
            target, source = self.controller.settings.target, "Central"
        weighted, coverage, sensors = weighted_average(self.settings, self.hass.states, now, self.hass.config.units.temperature_unit)
        public_memory = {key: getattr(self.memory, key) for key in ("owner", "manual_target", "manual_expires_at", "last_command_at", "last_command", "native_expires_at", "schedule_version", "confirmed_version")}
        self.snapshot = {"settings": self.settings.serialize(), "schedule": deepcopy(self.schedule),
                         **public_memory, "native_schedule_prepared": bool(self.memory.managed_schedule_id), "native_schedule_confirmed": bool(self.memory.confirmed_schedule), "effective_target": target, "target_source": source,
                         "weighted_temperature": weighted, "coverage": coverage, "sensors": sensors,
                         "migo_weight": 0, "status": self.fault or "miGo owns native heating", "reference_temperature": None,
                         "excluded_entities": sorted(self.water_entities | self.reference_aliases), "discovery": deepcopy(self.discovery)}
        self.snapshot["timezone"] = self.memory.native_timezone or getattr(self.hass.config, "time_zone", "Europe/Warsaw")
        if not self.settings.migo_entity:
            return
        try:
            api = self._api()
            if self.last_poll is None or now - self.last_poll >= self.settings.poll_seconds:
                await self._read(api)
            if self.last_config_poll is None or now - self.last_config_poll >= self.settings.poll_seconds:
                await self._read_config(api)
            now = time()
            if self.memory.owner == "Release pending":
                if self.last_attempt is None or now - self.last_attempt >= 60:
                    self.last_attempt = now
                    await self._release(api)
                return
            room = self.native_room or {}
            reference = temperature(room.get("therm_measured_temperature"))
            actual = temperature(room.get("therm_setpoint_temperature"))
            reference_ok = room.get("reachable") is not False and reference is not None and -20 <= reference <= 50 and self.last_poll is not None and now - self.last_poll <= self.settings.freshness_seconds
            physical_ok, physical_status = self._physical_reference(now)
            reference_ok = reference_ok and physical_ok
            self.snapshot["reference_freshness"] = physical_status
            self.snapshot["reference_temperature"] = reference if reference_ok else None
            self.snapshot["native_target"] = actual
            expiry = temperature(room.get("therm_setpoint_end_time"))
            if self.memory.owner != "HA":
                self.snapshot["status"] = self.fault or "miGo owns native heating"
                return
            if not self.memory.confirmed_schedule or self.memory.confirmed_version != self.memory.schedule_version:
                raise ValueError("Native fallback schedule pending; correction paused")
            if self.config_home.get("therm_setpoint_default_duration") != self.settings.manual_minutes:
                self.memory.owner = "miGo"
                raise ValueError("Native shared duration changed externally; miGo owns heating")
            managed = self._managed(self.config_home)
            if not managed.get("selected"):
                self.memory.owner = "miGo"
                raise ValueError("Native schedule changed; miGo owns heating")
            if self.config_home.get("therm_mode") != "schedule" or self.config_home.get("temperature_control_mode", "heating") != "heating" or room.get("therm_setpoint_mode") in ("off", "hg", "away", "max"):
                self.memory.owner = "miGo"
                raise ValueError("Native away/frost/DHW mode; correction paused")
            if room.get("therm_setpoint_mode") == "manual" and (self.memory.last_command is None or self.memory.native_expires_at is None or actual != self.memory.last_command or expiry != self.memory.native_expires_at):
                self.memory.owner = "miGo"
                raise ValueError("Native override changed externally; miGo owns heating")
            if room.get("therm_setpoint_mode") not in ("manual", "home", "schedule"):
                self.memory.owner = "miGo"
                raise ValueError("Unrecognized native mode; miGo owns heating")
            if self.memory.last_command is not None and room.get("therm_setpoint_mode") in ("home", "schedule") and self.memory.native_expires_at is not None and now < self.memory.native_expires_at:
                self.memory.owner = "miGo"
                raise ValueError("Native override cancelled externally; miGo owns heating")
            self.memory.native_expires_at = expiry
            if weighted is None or not reference_ok or actual is None:
                self.correction.reset()
                raise ValueError("Insufficient fresh room coverage or invalid miGo reference; correction paused")
            if not self.settings.minimum <= actual <= self.settings.maximum:
                raise ValueError("Native output outside configured bounds; correction paused")
            proposal = self.correction.evaluate(self.settings, self.memory, weighted, reference, target, now, actual)
            self.snapshot.update(proposal)
            renew = room.get("therm_setpoint_mode") == "manual" and expiry is not None and expiry - now <= max(120, self.settings.poll_seconds * 2)
            desired = proposal["requested_output"]
            change = proposal["due"] and desired != actual
            if change or renew or (proposal["due"] and room.get("therm_setpoint_mode") != "manual"):
                if self.last_attempt is not None and now - self.last_attempt < 60:
                    return
                # The minute polling cache is for sensing, never authorization
                # to overwrite a possibly newer native user command.
                latest = await self._read(api)
                latest_config = await self._read_config(api)
                self._managed(latest_config)
                if latest_config.get("therm_mode") != "schedule" or latest_config.get("temperature_control_mode", "heating") != "heating" or latest_config.get("therm_setpoint_default_duration") != self.settings.manual_minutes or not self._managed(latest_config).get("selected"):
                    self.memory.owner = "miGo"
                    raise ValueError("Native mode/schedule changed before command; miGo owns heating")
                if latest.get("therm_setpoint_mode") not in ("manual", "home", "schedule") or (latest.get("therm_setpoint_mode") == "manual" and (self.memory.last_command is None or self.memory.native_expires_at is None or temperature(latest.get("therm_setpoint_temperature")) != self.memory.last_command or temperature(latest.get("therm_setpoint_end_time")) != self.memory.native_expires_at)):
                    self.memory.owner = "miGo"
                    raise ValueError("New native user override before command; miGo owns heating")
                signature_fields = ("therm_setpoint_mode", "therm_setpoint_temperature", "therm_setpoint_end_time")
                if any(latest.get(key) != room.get(key) for key in signature_fields):
                    self.snapshot["status"] = "Native state changed; resampling before correction"
                    return
                physical_ok, _ = self._physical_reference(time())
                if latest.get("reachable") is False or not physical_ok:
                    raise ValueError("Native reference became unavailable before command")
                latest_reference = temperature(latest.get("therm_measured_temperature"))
                latest_weighted, latest_coverage, latest_sensors = weighted_average(self.settings, self.hass.states, time(), self.hass.config.units.temperature_unit)
                if latest_reference is None or not -20 <= latest_reference <= 50 or latest_weighted is None:
                    raise ValueError("Ambient data became invalid before command")
                if latest_reference != reference or latest_weighted != weighted:
                    proposal = self.correction.evaluate(self.settings, self.memory, latest_weighted, latest_reference, target, time(), actual)
                    self.snapshot.update(proposal, weighted_temperature=latest_weighted, coverage=latest_coverage, sensors=latest_sensors)
                    desired = proposal["requested_output"]
                    change = proposal["due"] and desired != actual
                    if not change and not renew and room.get("therm_setpoint_mode") == "manual":
                        return
                if change and self.memory.last_command_at is not None and time() - self.memory.last_command_at < self.settings.correction_seconds:
                    return
                # Renewal extends the current native value, never sneaks a correction past the limiter.
                command = desired if change else actual
                if not self.settings.minimum <= command <= self.settings.maximum:
                    raise ValueError("Native output outside configured bounds; correction released")
                self.last_attempt = time()
                ordinary = change or room.get("therm_setpoint_mode") != "manual"
                if ordinary:
                    self.memory.last_command_at = time()
                    self.memory.last_command = command
                    self.memory.pending_command = True
                    await self._save()  # crash-safe conservative command reservation
                old_expiry = expiry
                sent_at = time()
                checked(await api.set_room_state(self.settings.home_id, self.settings.room_id, mode="manual", temp=command))
                confirmed = await self._read(api)
                end = temperature(confirmed.get("therm_setpoint_end_time"))
                if confirmed.get("therm_setpoint_mode") != "manual" or temperature(confirmed.get("therm_setpoint_temperature")) != command or end is None or end <= now + 60:
                    raise ValueError("Native target/expiry not confirmed")
                # Target+mode read-back identifies this command even when the
                # backend applied the wrong duration: safety can cancel it.
                self.memory.native_expires_at = end
                if ordinary:
                    # A slow read/write never shortens physical command spacing.
                    self.memory.last_command_at = time()
                self.memory.pending_command = False
                if renew and old_expiry is not None and end <= old_expiry:
                    raise ValueError("Same-value renewal did not extend actual native expiry")
                if not sent_at + self.settings.manual_minutes * 60 - 60 <= end <= time() + self.settings.manual_minutes * 60 + 60:
                    raise ValueError("Native expiry does not match confirmed shared duration")
                self.memory.native_expires_at = end
                await self._save()
            self.fault = None
            self.snapshot["status"] = "HA correction active" if proposal["due"] else "Waiting for 10-minute actuator limiter"
        except Exception as err:
            self.fault = str(err)
            self.snapshot["status"] = self.fault
            # Invalid sensing/pending fallback must not retain a stale high bias.
            # Only cancel our own override, never an externally changed one.
            if self.memory.owner == "HA" and self.memory.last_command is not None:
                self.memory.owner = "Release pending"
                await self._save()
                try:
                    await self._release(api)
                except Exception:
                    self.fault += "; cancellation pending, native expiry is fallback"
        finally:
            self.snapshot.update({"owner": self.memory.owner, "native_expires_at": self.memory.native_expires_at})
            self.snapshot["native_target"] = temperature((self.native_room or {}).get("therm_setpoint_temperature"))
            self.snapshot["status"] = self.fault or self.snapshot["status"]
