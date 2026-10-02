"""Persisted, event-driven Home Assistant adapter for the heating policy."""

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
import logging
from time import monotonic

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, label_registry as lr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN, RECONCILE_SECONDS, VERSION
from .logic import Decision, RoomMemory, Settings, decide, room_blocked, temperature, zone_permission

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Room:
    key: str
    entity_id: str
    name: str
    zone_ids: tuple[str, ...]


class HeatingController:
    def __init__(self, hass, entry, zones):
        self.hass = hass
        self.entry = entry
        self.zones = {zone["id"]: zone for zone in zones}
        self.settings = Settings()
        self.rooms = {}
        self.memories = {}
        self.gates = {key: False for key in self.zones}
        self.controls = {}
        self.room_controls = {}
        self.snapshot = {}
        self.errors = {}
        self.startup_fault = None
        self._seeded = False
        self._store = Store(hass, 1, DOMAIN, atomic_writes=True)
        self._saved = None
        self._listeners = []
        self._room_listeners = []
        self._unsubs = []
        self._last_attempt = {}
        self._task = None
        self._dirty = False
        self._started = False
        self._closed = False

    async def async_load(self):
        data = await self._store.async_load() or {}
        try:
            settings = Settings(**data.get("settings", {}))
            settings.validate()
            self.settings = settings
        except (TypeError, ValueError):
            self.startup_fault = "Stored settings were invalid; reset to defaults and Off"
        for key, raw in data.get("rooms", {}).items():
            value = temperature(raw.get("target"))
            self.memories[key] = RoomMemory(
                blocked=raw.get("blocked") is not False,
                override=raw.get("override") is True,
                target=value if value is not None and 5 <= value <= 35 else self.settings.target,
            )
        for key in self.gates:
            self.gates[key] = data.get("gates", {}).get(key) is True
        self._seeded = data.get("seeded") is True
        registry = er.async_get(self.hass)
        labels = lr.async_get(self.hass)
        for zone in self.zones.values():
            if labels.async_get_label(zone["label"]) is None:
                labels.async_create(name=zone["label"], icon="mdi:radiator", color="orange")
            if not self._seeded:
                for entity_id in zone.get("initial_members", []):
                    if entity := registry.async_get(entity_id):
                        registry.async_update_entity(entity_id, labels=entity.labels | {zone["label"]})
                    else:
                        _LOGGER.warning("Initial heating thermostat %s is not registered", entity_id)
        self._seeded = True
        await self._refresh_rooms()
        self._publish()
        await self._store.async_save(self._serialize())

    @callback
    def async_start(self):
        self._started = self.hass.is_running

        @callback
        def started(_event):
            self._started = True
            self.async_request_reconcile()

        @callback
        def state_changed(event):
            entity_id = event.data.get("entity_id")
            if entity_id in {zone["sensor"] for zone in self.zones.values()} or any(
                room.entity_id == entity_id for room in self.rooms.values()
            ):
                self.async_request_reconcile()

        @callback
        def registry_changed(event):
            if event.event_type == "label_registry_updated" or event.data.get("entity_id", "").startswith("climate."):
                self.async_request_reconcile()

        @callback
        def interval_tick(_now):
            self.async_request_reconcile()

        async def stopping(_event):
            await self.async_stop(turn_off=False)

        self._unsubs.extend([
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, started),
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping),
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, state_changed),
            self.hass.bus.async_listen("entity_registry_updated", registry_changed),
            self.hass.bus.async_listen("label_registry_updated", registry_changed),
            async_track_time_interval(self.hass, interval_tick, timedelta(seconds=RECONCILE_SECONDS)),
        ])
        self.async_request_reconcile()

    async def async_stop(self, *, turn_off):
        if self._closed:
            return
        self._closed = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if turn_off:
            await asyncio.gather(*(self._turn_off(room.entity_id) for room in self.rooms.values()))
        await self._store.async_save(self._serialize())

    @callback
    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_add_room_listener(self, listener):
        self._room_listeners.append(listener)
        return lambda: self._room_listeners.remove(listener)

    @callback
    def async_register_control(self, key, entity_id, room_key=None):
        if room_key is None:
            self.controls[key] = entity_id
        else:
            self.room_controls.setdefault(room_key, {})[key] = entity_id
        self._publish()

    async def async_set_setting(self, key, value):
        try:
            updated = replace(self.settings, **{key: value})
            updated.validate()
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(str(err)) from err
        self.settings = updated
        await self._store.async_save(self._serialize())
        await self.async_reconcile_now()

    async def async_set_room(self, room_key, key, value):
        if room_key not in self.rooms:
            raise ServiceValidationError("This thermostat has no heating zone label")
        if key == "target":
            value = temperature(value)
            if value is None or not 5 <= value <= 35:
                raise ServiceValidationError("Custom target must be between 5 and 35 °C")
        elif key != "override" or not isinstance(value, bool):
            raise ServiceValidationError("Invalid room setting")
        setattr(self.memories[room_key], key, value)
        await self._store.async_save(self._serialize())
        await self.async_reconcile_now()

    async def async_reconcile_now(self):
        self.async_request_reconcile()
        if self._task is not None:
            await asyncio.shield(self._task)
        else:
            self._publish()

    @callback
    def async_request_reconcile(self):
        if self._closed:
            return
        self._dirty = True
        if self._started and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_task(self._run(), "central heating reconcile")

    async def _run(self):
        try:
            while self._dirty and not self._closed:
                self._dirty = False
                await self._refresh_rooms()
                self._update_gates()
                await asyncio.gather(*(self._apply(room) for room in list(self.rooms.values())))
                self._publish()
                data = self._serialize()
                if data != self._saved:
                    self._saved = data
                    self._store.async_delay_save(self._serialize, 1)
        except HomeAssistantError as err:
            self.startup_fault = str(err)
            _LOGGER.exception("Central heating reconciliation failed")
            self._publish()

    async def _refresh_rooms(self):
        registry = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        areas = ar.async_get(self.hass)
        rooms = {}
        for entity in registry.entities.values():
            if entity.domain != "climate":
                continue
            zone_ids = tuple(key for key, zone in self.zones.items() if zone["label"] in entity.labels)
            if not zone_ids:
                continue
            area_id = entity.area_id
            if not area_id and entity.device_id and (device := devices.async_get(entity.device_id)):
                area_id = device.area_id
            area = areas.async_get_area(area_id) if area_id else None
            state = self.hass.states.get(entity.entity_id)
            name = area.name if area else (state.name if state else entity.name or entity.entity_id)
            rooms[entity.id] = Room(entity.id, entity.entity_id, name, zone_ids)
            self.memories.setdefault(entity.id, RoomMemory(target=self.settings.target))
        for key in self.rooms.keys() - rooms.keys():
            await self._turn_off(self.rooms[key].entity_id)
        added = rooms.keys() - self.rooms.keys()
        self.rooms = rooms
        for listener in tuple(self._room_listeners):
            if added:
                listener([rooms[key] for key in added])

    def _water(self, zone):
        state = self.hass.states.get(zone["sensor"])
        return temperature(state.state, state.attributes.get("unit_of_measurement", "°C")) if state else None

    def _update_gates(self):
        for key, zone in self.zones.items():
            self.gates[key] = zone_permission(self._water(zone), self.gates[key], self.settings)

    def _room_temperature(self, state):
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return temperature(state.attributes.get("current_temperature"), self.hass.config.units.temperature_unit)

    def _decision(self, room):
        if self._started:
            self._update_gates()
        state = self.hass.states.get(room.entity_id)
        current = self._room_temperature(state)
        memory = self.memories[room.key]
        if self._started:
            memory.blocked = room_blocked(current, memory.blocked, self.settings)
        attributes = state.attributes if state else {}
        water = self._water(self.zones[room.zone_ids[0]])
        return decide(
            self.settings, memory, current, self.gates[room.zone_ids[0]],
            water_valid=water is not None and 0 <= water <= 120,
            membership_valid=len(room.zone_ids) == 1,
            available=state is not None and state.state not in ("unavailable", "unknown"),
            compatible="fan_only" in attributes.get("hvac_modes", []) and {"low", "medium"}.issubset(attributes.get("fan_modes", [])),
        )

    async def _turn_off(self, entity_id):
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("off", "unknown", "unavailable"):
            return
        try:
            async with asyncio.timeout(10):
                await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": "off"}, blocking=True)
        except (HomeAssistantError, TimeoutError) as err:
            _LOGGER.warning("Could not stop heating fan %s: %s", entity_id, err)

    async def _apply(self, room):
        decision = self._decision(room)
        state = self.hass.states.get(room.entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return
        if state.state == decision.hvac_mode and (decision.hvac_mode == "off" or state.attributes.get("fan_mode") == decision.fan_mode):
            self.errors.pop(room.key, None)
            return
        desired = (decision.hvac_mode, decision.fan_mode)
        previous, attempted_at = self._last_attempt.get(room.key, (None, 0))
        if previous == desired and monotonic() - attempted_at < 10:
            return
        self._last_attempt[room.key] = (desired, monotonic())
        try:
            async with asyncio.timeout(10):
                if decision.hvac_mode != "off":
                    if state.attributes.get("fan_mode") != decision.fan_mode:
                        await self.hass.services.async_call("climate", "set_fan_mode", {"entity_id": room.entity_id, "fan_mode": decision.fan_mode}, blocking=True)
                    # Temperature/mode may change while the device command is awaited.
                    # Recheck before powering a fan on; a room cutoff wins immediately.
                    latest = self._decision(room)
                    if latest.hvac_mode != "off" and latest.fan_mode != decision.fan_mode:
                        await self.hass.services.async_call("climate", "set_fan_mode", {"entity_id": room.entity_id, "fan_mode": latest.fan_mode}, blocking=True)
                        latest = self._decision(room)
                    decision = latest
                state = self.hass.states.get(room.entity_id)
                if state and state.state != decision.hvac_mode:
                    await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": room.entity_id, "hvac_mode": decision.hvac_mode}, blocking=True)
                self.errors.pop(room.key, None)
        except (HomeAssistantError, TimeoutError) as err:
            self.errors[room.key] = str(err) or "Device command timed out"
            _LOGGER.warning("Could not control heating fan %s: %s", room.entity_id, err)

    def _serialize(self):
        return {"settings": self.settings.serialize(), "gates": dict(self.gates), "rooms": {key: memory.serialize() for key, memory in self.memories.items()}, "seeded": self._seeded}

    @callback
    def _publish(self):
        room_data = []
        for room in sorted(self.rooms.values(), key=lambda item: item.name):
            state = self.hass.states.get(room.entity_id)
            decision = self._decision(room)
            memory = self.memories[room.key]
            target = memory.effective_target(self.settings)
            actual_mode = state.state if state else "unavailable"
            actual_fan = state.attributes.get("fan_mode") if state else None
            confirmed = actual_mode == decision.hvac_mode and (decision.hvac_mode == "off" or actual_fan == decision.fan_mode)
            room_data.append({
                "key": room.key, "entity_id": room.entity_id, "name": room.name,
                "zone_ids": list(room.zone_ids), "temperature": self._room_temperature(state),
                "effective_target": target, "target_source": "Custom" if memory.override else "Central",
                "custom_target": memory.target, "override": memory.override, "medium_below": target - 1,
                "blocked": memory.blocked, "desired_mode": decision.hvac_mode, "desired_fan": decision.fan_mode,
                "reported_mode": actual_mode, "reported_fan": actual_fan,
                "confirmed": confirmed, "reason": decision.reason, "error": self.errors.get(room.key),
                "controls": dict(self.room_controls.get(room.key, {})),
            })
        labels = lr.async_get(self.hass)
        self.snapshot = {
            "version": VERSION, "mode": self.settings.mode, "settings": self.settings.serialize(),
            "controls": dict(self.controls), "startup_fault": self.startup_fault,
            "zones": [{**zone, "water_temperature": self._water(zone), "allowed": self.gates[key], "label_missing": labels.async_get_label(zone["label"]) is None} for key, zone in self.zones.items()],
            "rooms": room_data,
            "running_fans": sum(room["reported_mode"] == "fan_only" for room in room_data),
        }
        for listener in tuple(self._listeners):
            listener()
