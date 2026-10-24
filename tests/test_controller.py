"""Exercise asynchronous device commands with an in-memory HA adapter."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1] / "custom_components/central_heating"


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    sys.modules[name] = result
    return result


class HomeAssistantError(Exception):
    pass


class ServiceValidationError(HomeAssistantError):
    pass


class Store:
    def __init__(self, hass, version, key, **kwargs):
        self.hass = hass
        self.key = key

    async def async_load(self):
        return deepcopy(self.hass.storage.get(self.key))

    async def async_save(self, data):
        self.hass.storage[self.key] = deepcopy(data)

    def async_delay_save(self, callback, delay):
        self.hass.storage[self.key] = deepcopy(callback())


module("homeassistant")
module("homeassistant.components")
module("homeassistant.components.persistent_notification",
       async_create=lambda hass, message, title, notification_id: (
           hass.notifications.update({notification_id: {"message": message, "title": title}}),
           hass.notification_events.append(("create", notification_id))),
       async_dismiss=lambda hass, notification_id: (
           hass.notifications.pop(notification_id, None),
           hass.notification_events.append(("dismiss", notification_id))))
module("homeassistant.const", EVENT_HOMEASSISTANT_STARTED="started", EVENT_HOMEASSISTANT_STOP="stop", EVENT_STATE_CHANGED="state_changed", EVENT_STATE_REPORTED="state_reported")
def callback(function):
    function._hass_callback = True
    return function


def track_interval(hass, action, interval):
    # HA sends an unmarked synchronous function to an executor thread.
    assert getattr(action, "_hass_callback", False), "Timer must run in HA's event loop"
    hass.interval_action = action
    return lambda: None


module("homeassistant.core", callback=callback)
module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError, ServiceValidationError=ServiceValidationError)
helpers = module("homeassistant.helpers")
for name in ("area_registry", "device_registry", "entity_registry", "label_registry"):
    fake = module(f"homeassistant.helpers.{name}", async_get=lambda hass, key=name: getattr(hass, key))
    setattr(helpers, name, fake)
module("homeassistant.helpers.event", async_track_time_interval=track_interval)
module("homeassistant.helpers.storage", Store=Store)
package = module("adapter_under_test")
package.__path__ = [str(ROOT)]
for name in ("const", "logic", "water", "controller"):
    spec = importlib.util.spec_from_file_location(f"adapter_under_test.{name}", ROOT / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
controller_module = sys.modules["adapter_under_test.controller"]
logic = sys.modules["adapter_under_test.logic"]


class Registry:
    def __init__(self):
        self.entities = {}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def async_update_entity(self, entity_id, **values):
        for key, value in values.items():
            setattr(self.entities[entity_id], key, value)

    def add(self, entity_id, labels):
        self.entities[entity_id] = SimpleNamespace(
            id=entity_id, entity_id=entity_id, domain=entity_id.split(".")[0], labels=set(labels),
            area_id=None, device_id=None, name=entity_id,
        )


class Labels:
    def __init__(self):
        self.labels = set()

    def async_get_label(self, name):
        return name if name in self.labels else None

    def async_create(self, name, **kwargs):
        self.labels.add(name)


class States:
    def __init__(self):
        self.values = {}
        self.report = 0

    def get(self, entity_id):
        return self.values.get(entity_id)

    def water(self, entity_id, value):
        self.report += 1
        self.values[entity_id] = SimpleNamespace(state=str(value), attributes={"unit_of_measurement": "°C"}, name=entity_id, last_reported=datetime.fromtimestamp(self.report, UTC))

    def room(self, entity_id, temperature=20, mode="off", speed="auto"):
        self.values[entity_id] = SimpleNamespace(state=mode, name=entity_id, attributes={
            "current_temperature": temperature, "fan_mode": speed,
            "hvac_modes": ["off", "fan_only", "heat"], "fan_modes": ["low", "medium", "high", "auto"],
        })


class Services:
    def __init__(self, hass):
        self.hass = hass
        self.calls = []
        self.hook = None
        self.fail = None

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, deepcopy(data)))
        if data["entity_id"] == self.fail:
            raise HomeAssistantError("Connection lost")
        state = self.hass.states.get(data["entity_id"])
        if service == "set_fan_mode":
            state.attributes["fan_mode"] = data["fan_mode"]
        elif service == "set_hvac_mode":
            state.state = data["hvac_mode"]
        else:
            raise AssertionError(f"Unexpected thermostat service: {service}")
        if self.hook:
            await self.hook(service, data)


class Bus:
    def __init__(self):
        self.listeners = {}

    def async_listen(self, event_type, action, event_filter=None):
        self.listeners[event_type] = (action, event_filter)
        return lambda: self.listeners.pop(event_type, None)

    def async_listen_once(self, *args):
        return lambda: None

    def fire(self, event_type, data):
        action, event_filter = self.listeners[event_type]
        if event_filter is None or event_filter(data):
            return action(SimpleNamespace(event_type=event_type, data=data))


class Hass:
    def __init__(self):
        self.storage = {}
        self.notifications = {}
        self.notification_events = []
        self.states = States()
        self.services = Services(self)
        self.entity_registry = Registry()
        self.label_registry = Labels()
        self.device_registry = SimpleNamespace(async_get=lambda _: None)
        self.area_registry = SimpleNamespace(async_get_area=lambda _: None)
        self.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"), language="en")
        self.is_running = True
        self.bus = Bus()

    def async_create_task(self, coroutine, name):
        return asyncio.create_task(coroutine, name=name)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = 1000
        for name, clock in (("monotonic", lambda: self.clock), ("time", lambda: 1800000000 + self.clock)):
            patcher = patch.object(controller_module, name, clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hass = Hass()
        self.hass.entity_registry.add("climate.office", ["heating_upstairs"])
        self.hass.states.room("climate.office")
        self.hass.states.water("sensor.upstairs", 34)
        self.hass.states.water("sensor.downstairs", 29)
        self.entry = SimpleNamespace(entry_id="test")
        self.zones = [
            {"id": "upstairs", "name": "Upstairs", "sensor": "sensor.upstairs", "label": "heating_upstairs", "initial_members": []},
            {"id": "downstairs", "name": "Downstairs", "sensor": "sensor.downstairs", "label": "heating_downstairs", "initial_members": []},
        ]
        self.controller = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await self.controller.async_load()
        self.controller._started = True
        self.controller.settings.mode = "Manual"

    async def run_controller(self):
        await self.controller.async_reconcile_now()

    def fan_calls(self):
        return [(service, data.get("fan_mode") or data.get("hvac_mode")) for _, service, data in self.hass.services.calls]

    async def test_quiet_hours_use_ha_timezone_and_preserve_schedule_when_toggled(self):
        self.hass.config.time_zone = "Europe/Warsaw"
        instant = datetime(2026, 10, 19, 20, 0, tzinfo=UTC).timestamp()
        with patch.object(controller_module, "time", lambda: instant):
            await self.controller.async_set_quiet_hours("climate.office", True, "22:00", "07:00", [0])
            room = self.controller.snapshot["rooms"][0]
            self.assertEqual(self.controller.snapshot["timezone"], "Europe/Warsaw")
            self.assertTrue(room["quiet_active"])
            await self.controller.async_set_room("climate.office", "quiet_enabled", False)
            self.assertFalse(self.controller.snapshot["rooms"][0]["quiet_active"])
            await self.controller.async_set_room("climate.office", "quiet_enabled", True)
            memory = self.controller.memories["climate.office"]
            self.assertEqual((memory.quiet_start, memory.quiet_end, memory.quiet_days), ("22:00", "07:00", [0]))
            self.assertTrue(self.controller.snapshot["rooms"][0]["quiet_active"])
        restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await restored.async_load()
        self.assertEqual(restored.memories["climate.office"].quiet_days, [0])
        self.assertTrue(restored.memories["climate.office"].quiet_enabled)

    async def test_quiet_is_per_room_and_boundary_restores_adaptive_speed(self):
        self.hass.entity_registry.add("climate.bedroom", ["heating_upstairs"])
        self.hass.states.room("climate.bedroom")
        await self.prepare_adaptive_water()
        self.hass.config.time_zone = "Europe/Warsaw"
        instant = datetime(2026, 10, 19, 20, 0, tzinfo=UTC).timestamp()
        with patch.object(controller_module, "time", lambda: instant):
            await self.controller.async_set_quiet_hours("climate.office", True, "22:00", "07:00", [0])
            self.assertEqual(self.hass.states.get("climate.office").attributes["fan_mode"], "low")
            self.assertEqual(self.hass.states.get("climate.bedroom").attributes["fan_mode"], "medium")
            self.clock += 11
            instant = datetime(2026, 10, 20, 5, 0, tzinfo=UTC).timestamp()
            await self.run_controller()
            self.assertEqual(self.hass.states.get("climate.office").attributes["fan_mode"], "medium")
            self.assertFalse(next(room for room in self.controller.snapshot["rooms"] if room["entity_id"] == "climate.office")["quiet_active"])

    async def test_quiet_invalid_update_is_atomic(self):
        await self.controller.async_set_quiet_hours("climate.office", True, "21:30", "06:30", [0, 4])
        before = deepcopy(self.controller._serialize())
        for entity, enabled, start, end, days in [
            ("climate.missing", True, "22:00", "07:00", [0]),
            ("climate.office", 1, "22:00", "07:00", [0]),
            ("climate.office", True, "24:00", "07:00", [0]),
            ("climate.office", True, "7:00", "07:00", [0]),
            ("climate.office", True, "22:00", "22:00", [0]),
            ("climate.office", True, "22:00", "07:00", []),
            ("climate.office", True, "22:00", "07:00", [0, 0]),
            ("climate.office", True, "22:00", "07:00", [True]),
            ("climate.office", True, "22:00", "07:00", [7]),
            ("climate.office", True, "22:00", "07:00", "0"),
        ]:
            with self.subTest(entity=entity, days=days, start=start):
                with self.assertRaises(ServiceValidationError):
                    await self.controller.async_set_quiet_hours(entity, enabled, start, end, days)
                self.assertEqual(self.controller._serialize(), before)

    async def test_legacy_and_invalid_quiet_storage_preserve_other_room_settings(self):
        for quiet in ({}, {"quiet_enabled": True, "quiet_start": "invalid"}, {"quiet_enabled": True, "quiet_days": [False]}):
            self.hass.storage["central_heating"] = {"rooms": {"climate.office": {
                "target": 23, "override": True, "room_maximum": 25, **quiet,
            }}, "seeded": True}
            restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
            await restored.async_load()
            memory = restored.memories["climate.office"]
            self.assertFalse(memory.quiet_enabled)
            self.assertEqual((memory.quiet_start, memory.quiet_end, memory.quiet_days), ("22:00", "07:00", list(range(7))))
            self.assertEqual((memory.target, memory.override, memory.room_maximum), (23, True, 25))

    async def test_quiet_change_during_speed_command_rechecks_before_power_on(self):
        await self.prepare_adaptive_water()
        self.clock += 11
        self.hass.states.room("climate.office")
        self.hass.services.calls.clear()
        self.hass.config.time_zone = "Europe/Warsaw"
        instant = datetime(2026, 10, 19, 20, 0, tzinfo=UTC).timestamp()
        async def enable_quiet(service, data):
            self.controller.memories["climate.office"].quiet_enabled = True
        self.hass.services.hook = enable_quiet
        with patch.object(controller_module, "time", lambda: instant):
            await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "medium"), ("set_fan_mode", "low"), ("set_hvac_mode", "fan_only")])
        self.assertEqual(self.controller.snapshot["rooms"][0]["reason"], "Quiet hours · low")

    async def test_reported_stop_then_restart_counts_when_final_published_mode_is_unchanged(self):
        self.controller.async_start()
        await self.run_controller()
        before = self.controller.snapshot["rooms"][0]["fan_starts"]
        self.clock += 10
        self.hass.states.room("climate.office", mode="off", speed="low")
        self.hass.bus.fire("state_changed", {"entity_id": "climate.office", "new_state": self.hass.states.get("climate.office")})
        # Reconciliation restarts this fan before it next publishes its snapshot.
        await self.run_controller()
        room = self.controller.snapshot["rooms"][0]
        self.assertEqual(room["reported_mode"], "fan_only")
        self.assertEqual(room["fan_starts"], before + 1)
        # A managed sensor update is not mistaken for a fan transition.
        self.hass.bus.fire("state_changed", {"entity_id": "sensor.heating_office_fan_starts", "new_state": SimpleNamespace(state="off")})
        self.assertEqual(self.controller.telemetry.snapshot("climate.office")["fan_starts"], before + 1)
        await self.controller.async_stop(turn_off=False)

    async def test_unassigned_room_time_does_not_accrue_when_room_is_readded(self):
        await self.run_controller()
        self.clock += 60
        await self.run_controller()
        before = self.controller.telemetry.snapshot("climate.office")["low_runtime_hours"]
        self.hass.entity_registry.entities["climate.office"].labels.clear()
        await self.run_controller()
        self.clock += 3600
        # A fan used outside central heating cannot contribute that unmanaged hour.
        self.hass.states.room("climate.office", mode="fan_only", speed="low")
        self.hass.entity_registry.entities["climate.office"].labels.add("heating_upstairs")
        await self.run_controller()
        self.assertEqual(self.controller.snapshot["rooms"][0]["low_runtime_hours"], before)
        self.clock += 60
        await self.run_controller()
        self.assertAlmostEqual(self.controller.snapshot["rooms"][0]["low_runtime_hours"], before + 1 / 60)

    async def test_speed_is_set_before_powering_on(self):
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "low"), ("set_hvac_mode", "fan_only")])
        self.assertTrue(self.controller.snapshot["rooms"][0]["confirmed"])

    async def test_timer_runs_in_the_event_loop(self):
        self.controller.async_start()
        await self.run_controller()
        self.hass.interval_action(None)
        await self.run_controller()
        await self.controller.async_stop(turn_off=False)

    async def test_shutdown_removes_started_and_stop_listeners_once(self):
        self.controller.async_start()
        self.hass.bus.fire("started", {})
        await self.run_controller()
        await self.hass.bus.fire("stop", {})
        self.assertEqual(self.hass.bus.listeners, {})

    async def test_reconciliation_is_idempotent(self):
        await self.run_controller()
        await self.run_controller()
        self.assertEqual(len(self.hass.services.calls), 2)

    async def test_room_cutoff_during_speed_command_prevents_start(self):
        async def heat_room(service, data):
            self.hass.states.get("climate.office").attributes["current_temperature"] = 24
        self.hass.services.hook = heat_room
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "low")])
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_off_during_speed_command_prevents_start(self):
        async def switch_off(service, data):
            self.controller.settings.mode = "Off"
        self.hass.services.hook = switch_off
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "low")])

    async def test_target_change_during_command_rechecks_speed(self):
        self.controller.settings.mode = "Adaptive"
        self.controller.gates["upstairs"] = True
        async def change_target(service, data):
            self.controller.settings.target = 20
        self.hass.services.hook = change_target
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "medium"), ("set_fan_mode", "low"), ("set_hvac_mode", "fan_only")])

    async def test_off_is_reasserted_without_touching_native_setpoint(self):
        self.hass.states.room("climate.office", mode="heat")
        self.controller.settings.mode = "Off"
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_hvac_mode", "off")])

    async def test_failure_in_one_room_does_not_block_another(self):
        self.hass.entity_registry.add("climate.bedroom", ["heating_upstairs"])
        self.hass.states.room("climate.bedroom")
        self.hass.services.fail = "climate.office"
        with self.assertLogs(controller_module._LOGGER, level="WARNING"):
            await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.bedroom").state, "fan_only")
        office = next(room for room in self.controller.snapshot["rooms"] if room["entity_id"] == "climate.office")
        self.assertFalse(office["confirmed"])
        self.assertEqual(office["error"], "Connection lost")

    async def test_removing_label_requests_off(self):
        await self.run_controller()
        self.hass.entity_registry.entities["climate.office"].labels.clear()
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.assertEqual(self.controller.snapshot["rooms"], [])

    async def test_two_labels_stop_the_room(self):
        await self.run_controller()
        self.hass.entity_registry.entities["climate.office"].labels.add("heating_downstairs")
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.assertEqual(self.controller.snapshot["rooms"][0]["reason"], "Conflicting zone labels")

    async def test_new_rooms_get_controls_and_inherit_current_default(self):
        seen = []
        self.controller.async_add_room_listener(lambda rooms: seen.extend(rooms))
        self.controller.settings.target = 23
        self.hass.entity_registry.add("climate.bedroom", ["heating_upstairs"])
        self.hass.states.room("climate.bedroom")
        await self.run_controller()
        await self.run_controller()
        self.assertEqual([room.entity_id for room in seen], ["climate.bedroom"])
        memory = self.controller.memories["climate.bedroom"]
        self.assertFalse(memory.override)
        self.assertEqual(memory.target, 23)

        self.assertFalse(memory.limits_override)
        self.assertIsNone(memory.room_maximum)
        self.assertIsNone(memory.room_hysteresis)
        await self.controller.async_set_setting("room_maximum", 26)
        bedroom = next(room for room in self.controller.snapshot["rooms"] if room["entity_id"] == "climate.bedroom")
        self.assertEqual((bedroom["fan_on_temperature"], bedroom["fan_off_temperature"]), (25.5, 26))
        self.assertFalse(bedroom["limits_override"])

    async def test_saved_latches_survive_startup_and_deadbands(self):
        self.controller.settings.mode = "Adaptive"
        self.controller.memories["climate.office"] = logic.RoomMemory(blocked=True, override=True, target=23)
        self.controller.gates["upstairs"] = True
        await self.controller._store.async_save(self.controller._serialize())
        self.hass.states.room("climate.office", temperature=23.8)
        self.hass.states.water("sensor.upstairs", 31)
        restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await restored.async_load()
        self.assertTrue(restored.gates["upstairs"])
        self.assertTrue(restored.memories["climate.office"].blocked)
        self.assertTrue(restored.memories["climate.office"].override)
        restored._started = True
        await restored.async_reconcile_now()
        self.assertTrue(restored.gates["upstairs"])
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_invalid_setting_does_not_mutate_current_configuration(self):
        with self.assertRaises(ServiceValidationError):
            await self.controller.async_set_setting("water_on", 29)
        self.assertEqual(self.controller.settings.water_on, 33)

    async def test_disabled_override_retains_its_value(self):
        await self.controller.async_set_room("climate.office", "target", 23)
        await self.controller.async_set_room("climate.office", "override", True)
        await self.controller.async_set_setting("target", 20)
        self.assertEqual(self.controller.snapshot["rooms"][0]["effective_target"], 23)
        await self.controller.async_set_room("climate.office", "override", False)
        self.assertEqual(self.controller.snapshot["rooms"][0]["effective_target"], 20)
        self.assertEqual(self.controller.memories["climate.office"].target, 23)

    async def test_room_limits_capture_pair_and_reset_to_live_global_defaults(self):
        key = "climate.office"
        await self.controller.async_set_room(key, "room_maximum", 23)
        memory = self.controller.memories[key]
        self.assertTrue(memory.limits_override)
        self.assertEqual((memory.room_maximum, memory.room_hysteresis), (23, 0.5))
        await self.controller.async_set_setting("room_maximum", 26)
        await self.controller.async_set_setting("room_hysteresis", 1)
        room = self.controller.snapshot["rooms"][0]
        self.assertEqual((room["room_maximum"], room["room_hysteresis"]), (23, 0.5))
        self.assertEqual((room["fan_on_temperature"], room["fan_off_temperature"]), (22.5, 23))
        await self.controller.async_set_room(key, "limits_override", False)
        self.assertIsNone(memory.room_maximum)
        self.assertIsNone(memory.room_hysteresis)
        self.assertFalse(memory.limits_override)
        room = self.controller.snapshot["rooms"][0]
        self.assertEqual((room["fan_on_temperature"], room["fan_off_temperature"]), (25, 26))
        await self.controller.async_set_setting("room_hysteresis", 2)
        self.assertEqual(self.controller.snapshot["rooms"][0]["fan_on_temperature"], 24)
        await self.controller.async_set_room(key, "limits_override", True)
        self.assertEqual((memory.room_maximum, memory.room_hysteresis), (26, 2))
        await self.controller.async_set_room(key, "room_hysteresis", 0.75)
        self.assertEqual((memory.room_maximum, memory.room_hysteresis), (26, 0.75))

    async def test_hysteresis_edit_also_captures_current_maximum(self):
        await self.controller.async_set_setting("room_maximum", 25)
        await self.controller.async_set_room("climate.office", "room_hysteresis", 1.5)
        await self.controller.async_set_setting("room_maximum", 27)
        memory = self.controller.memories["climate.office"]
        self.assertEqual(memory.effective_room_limits(self.controller.settings), (25, 1.5))

    async def test_room_limit_change_during_fan_command_prevents_start(self):
        async def lower_limit(service, data):
            self.controller.memories["climate.office"].room_maximum = 20
        self.hass.services.hook = lower_limit
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "low")])
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_room_limits_drive_each_device_at_its_own_boundaries(self):
        self.hass.entity_registry.add("climate.bedroom", ["heating_upstairs"])
        self.hass.states.room("climate.bedroom", temperature=22)
        await self.run_controller()
        await self.controller.async_set_room("climate.office", "room_maximum", 23)
        await self.controller.async_set_room("climate.office", "room_hysteresis", 1)
        for mode in ("Manual", "Adaptive"):
            self.controller.settings.mode = mode
            self.controller.gates["upstairs"] = True
            for value, office_mode, bedroom_mode in [
                (22, "fan_only", "fan_only"),
                (23, "off", "fan_only"),
                (22.5, "off", "fan_only"),
                (22, "fan_only", "fan_only"),
                (24, "off", "off"),
                (23.6, "off", "off"),
                (23.5, "off", "fan_only"),
            ]:
                with self.subTest(mode=mode, value=value):
                    for key in ("climate.office", "climate.bedroom"):
                        self.hass.states.get(key).attributes["current_temperature"] = value
                    self.clock += 11
                    await self.run_controller()
                    self.assertEqual(self.hass.states.get("climate.office").state, office_mode)
                    self.assertEqual(self.hass.states.get("climate.bedroom").state, bedroom_mode)

    async def test_room_limits_persist_independently_of_target_expiry_and_cancel(self):
        key = "climate.office"
        await self.controller.async_set_room(key, "room_maximum", 25)
        await self.controller.async_set_room(key, "room_hysteresis", 1)
        await self.controller.async_apply_override(key, 23, "30 minutes")
        self.clock += 1800
        await self.run_controller()
        memory = self.controller.memories[key]
        self.assertFalse(memory.override)
        self.assertEqual(memory.effective_room_limits(self.controller.settings), (25, 1))
        await self.controller.async_apply_override(key, 23, "1 hour")
        await self.controller.async_set_room(key, "override", False)
        restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await restored.async_load()
        memory = restored.memories[key]
        self.assertTrue(memory.limits_override)
        self.assertFalse(memory.override)
        self.assertEqual(memory.effective_room_limits(restored.settings), (25, 1))
        self.assertTrue(restored.snapshot["rooms"][0]["limits_override"])

    async def test_resetting_room_limits_preserves_active_target_and_deadline(self):
        key = "climate.office"
        await self.controller.async_set_room(key, "room_maximum", 25)
        await self.controller.async_apply_override(key, 23, "1 hour")
        deadline = self.controller.memories[key].override_expires_at
        await self.controller.async_set_room(key, "limits_override", False)
        memory = self.controller.memories[key]
        self.assertTrue(memory.override)
        self.assertEqual(memory.target, 23)
        self.assertEqual(memory.override_expires_at, deadline)

    async def test_invalid_room_limits_are_rejected_without_mutation_or_commands(self):
        key = "climate.office"
        await self.controller.async_set_room(key, "room_maximum", 25)
        before = deepcopy(self.controller._serialize())
        snapshot = deepcopy(self.controller.snapshot)
        storage = deepcopy(self.hass.storage)
        calls = deepcopy(self.hass.services.calls)
        invalid = [("room_maximum", value) for value in (None, True, False, "NaN", float("inf"), 4.9, 35.1)]
        invalid += [("room_hysteresis", value) for value in (None, True, False, float("nan"), -1, 0, 0.09, 5.1)]
        invalid += [("limits_override", value) for value in (None, 0, 1, "true")]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ServiceValidationError):
                    await self.controller.async_set_room(key, field, value)
                self.assertEqual(self.controller._serialize(), before)
                self.assertEqual(self.controller.snapshot, snapshot)
                self.assertEqual(self.hass.storage, storage)
                self.assertEqual(self.hass.services.calls, calls)
        with self.assertRaises(ServiceValidationError):
            await self.controller.async_set_room("climate.missing", "room_maximum", 25)
        self.assertEqual(self.controller._serialize(), before)

    async def test_room_limit_bounds_are_accepted(self):
        for field, values in (("room_maximum", (5, 35)), ("room_hysteresis", (0.1, 5))):
            for value in values:
                await self.controller.async_set_room("climate.office", field, value)
                self.assertEqual(getattr(self.controller.memories["climate.office"], field), value)

    async def test_legacy_and_invalid_stored_room_limits_inherit_globals(self):
        for extra in ({}, {"room_maximum": True, "room_hysteresis": False},
                      {"room_maximum": "NaN", "room_hysteresis": float("inf")},
                      {"room_maximum": 40, "room_hysteresis": 0}):
            with self.subTest(extra=extra):
                self.hass.storage["central_heating"] = {
                    "settings": {"room_maximum": 26, "room_hysteresis": 1},
                    "rooms": {"climate.office": {"override": True, "target": 23, **extra}},
                    "seeded": True,
                }
                restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
                await restored.async_load()
                memory = restored.memories["climate.office"]
                self.assertEqual(memory.effective_room_limits(restored.settings), (26, 1))
                self.assertFalse(memory.limits_override)
                self.assertTrue(memory.override)
                self.assertEqual(memory.target, 23)

    async def test_partial_saved_limits_inherit_missing_value_until_override_is_enabled(self):
        for stored, expected in [({"room_maximum": 25, "room_hysteresis": -1}, (25, 1)),
                                 ({"room_hysteresis": 2}, (26, 2))]:
            with self.subTest(stored=stored):
                self.hass.storage["central_heating"] = {
                    "settings": {"room_maximum": 26, "room_hysteresis": 1},
                    "rooms": {"climate.office": stored}, "seeded": True,
                }
                restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
                await restored.async_load()
                memory = restored.memories["climate.office"]
                self.assertTrue(memory.limits_override)
                self.assertEqual(memory.effective_room_limits(restored.settings), expected)
                await restored.async_set_room("climate.office", "limits_override", True)
                self.assertEqual((memory.room_maximum, memory.room_hysteresis), expected)
                await restored.async_set_setting("room_maximum", 27)
                await restored.async_set_setting("room_hysteresis", 3)
                self.assertEqual(memory.effective_room_limits(restored.settings), expected)

    async def test_timed_override_expires_to_changed_central_default(self):
        await self.controller.async_apply_override("climate.office", 23, "30 minutes")
        deadline = self.controller.memories["climate.office"].override_expires_at
        await self.controller.async_set_setting("target", 21.5)
        self.clock += 1799
        await self.run_controller()
        self.assertTrue(self.controller.memories["climate.office"].override)
        self.clock += 1
        await self.run_controller()
        room = self.controller.snapshot["rooms"][0]
        self.assertEqual(deadline, 1800002800)
        self.assertFalse(room["override"])
        self.assertEqual(room["effective_target"], 21.5)
        self.assertEqual(room["custom_target"], 23)

    async def test_deadline_survives_restart_and_overdue_expiry_is_applied_at_load(self):
        await self.controller.async_apply_override("climate.office", 23, "1 hour")
        deadline = self.controller.memories["climate.office"].override_expires_at
        restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await restored.async_load()
        self.assertEqual(restored.memories["climate.office"].override_expires_at, deadline)
        self.clock += 3600
        overdue = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await overdue.async_load()
        self.assertFalse(overdue.memories["climate.office"].override)

    async def test_cancel_clears_deadline_and_permanent_override_has_none(self):
        await self.controller.async_apply_override("climate.office", 23, "2 hours")
        await self.controller.async_set_room("climate.office", "override", False)
        self.assertIsNone(self.controller.memories["climate.office"].override_expires_at)
        await self.controller.async_apply_override("climate.office", 23, "Until cancelled")
        self.clock += 100000
        await self.run_controller()
        self.assertTrue(self.controller.memories["climate.office"].override)

    async def test_legacy_permanent_overrides_are_preserved(self):
        self.hass.storage["central_heating"] = {"rooms": {"climate.office": {"override": True, "target": 23}}, "seeded": True}
        restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await restored.async_load()
        memory = restored.memories["climate.office"]
        self.assertTrue(memory.override)
        self.assertEqual(memory.override_duration, "Until cancelled")
        self.assertIsNone(memory.override_expires_at)

    async def test_invalid_atomic_override_does_not_change_target_or_deadline(self):
        await self.controller.async_apply_override("climate.office", 23, "1 hour")
        before = self.controller.memories["climate.office"].serialize()
        for entity, target, duration in [("climate.missing", 23, "1 hour"), ("climate.office", 99, "1 hour"), ("climate.office", 23, "3 hours")]:
            with self.assertRaises(ServiceValidationError):
                await self.controller.async_apply_override(entity, target, duration)
        self.assertEqual(self.controller.memories["climate.office"].serialize(), before)

    async def test_expiry_during_fan_command_rechecks_effective_target(self):
        memory = self.controller.memories["climate.office"]
        self.controller.settings.mode = "Adaptive"
        self.controller.settings.target = 20
        self.controller.gates["upstairs"] = True
        memory.override = True
        memory.target = 23
        memory.override_expires_at = 1800001001
        async def expire(service, data):
            self.clock += 1
        self.hass.services.hook = expire
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "medium"), ("set_fan_mode", "low"), ("set_hvac_mode", "fan_only")])

    async def test_cached_water_reading_is_not_counted_as_new_samples(self):
        await self.run_controller()
        for _ in range(5):
            await self.run_controller()
        self.assertEqual(list(self.controller.water_filters["upstairs"].samples), [34])

    async def test_smoothing_ticks_publish_temperature_without_inventing_sensor_reports(self):
        for _ in range(3):
            self.hass.states.water("sensor.upstairs", 22)
            await self.run_controller()
            self.clock += 5
        self.hass.states.water("sensor.upstairs", 21)
        await self.run_controller()
        before = deepcopy(self.zone("upstairs"))
        self.assertEqual(before["water_temperature"], 22)
        self.clock += 120
        await self.run_controller()
        after = self.zone("upstairs")
        self.assertGreater(after["water_temperature"], 21)
        self.assertLess(after["water_temperature"], 21.5)
        self.assertEqual(after["raw_reported_at"], before["raw_reported_at"])
        self.assertEqual(after["accepted_reported_at"], before["accepted_reported_at"])
        self.assertEqual(list(self.controller.water_filters["upstairs"].samples), [22, 22, 21])

    async def test_stable_exact_on_report_starts_after_smoothing_then_raw_cold_stops(self):
        self.controller.settings.mode = "Adaptive"
        self.hass.states.water("sensor.upstairs", 32)
        await self.run_controller()
        self.clock += 5
        self.hass.states.water("sensor.upstairs", 33)
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.clock += 360
        await self.run_controller()
        self.assertEqual(self.zone("upstairs")["water_temperature"], 33)
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.clock += 30
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "fan_only")
        self.hass.states.water("sensor.upstairs", 30)
        await self.run_controller()
        self.assertGreater(self.zone("upstairs")["water_temperature"], 30)
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_warm_water_requires_thirty_seconds_and_85_resets_confirmation(self):
        self.controller.settings.mode = "Adaptive"
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [])
        self.clock += 29
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [])
        self.hass.states.water("sensor.upstairs", 85)
        await self.run_controller()
        self.clock += 60
        await self.run_controller()
        self.assertFalse(self.controller.gates["upstairs"])
        self.hass.states.water("sensor.upstairs", 34)
        await self.run_controller()
        self.clock += 30
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").attributes["fan_mode"], "medium")

    async def test_fresh_identical_water_reports_are_observed(self):
        self.controller.async_start()
        await self.run_controller()
        for _ in range(2):
            self.hass.states.water("sensor.upstairs", 34)
            self.hass.bus.fire("state_reported", {"entity_id": "sensor.upstairs", "new_state": self.hass.states.get("sensor.upstairs")})
        await self.run_controller()
        self.assertEqual(list(self.controller.water_filters["upstairs"].samples), [34, 34, 34])
        await self.controller.async_stop(turn_off=False)

    async def test_sensor_fault_uses_healthy_backup_and_manual_bypasses_it(self):
        self.hass.entity_registry.add("climate.lounge", ["heating_downstairs"])
        self.hass.states.room("climate.lounge")
        self.hass.states.water("sensor.downstairs", 34)
        self.controller.settings.mode = "Adaptive"
        self.controller.gates = {"upstairs": True, "downstairs": True}
        await self.run_controller()
        self.hass.states.water("sensor.upstairs", 85)
        await self.run_controller()
        self.clock += 60
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "fan_only")
        upstairs = next(zone for zone in self.controller.snapshot["zones"] if zone["id"] == "upstairs")
        self.assertTrue(upstairs["backup_active"])
        self.assertEqual(upstairs["water_source_id"], "downstairs")
        self.assertEqual(self.hass.states.get("climate.lounge").state, "fan_only")
        await self.controller.async_set_setting("mode", "Manual")
        self.assertEqual(self.hass.states.get("climate.office").attributes["fan_mode"], "low")
        self.assertFalse(self.controller.snapshot["backup_active"])
        self.assertEqual(self.hass.notifications, {})

    async def prepare_adaptive_water(self, backup_temperature=34):
        self.controller.settings.mode = "Adaptive"
        self.hass.states.water("sensor.downstairs", backup_temperature)
        await self.run_controller()
        self.clock += 30
        await self.run_controller()

    def zone(self, key):
        return next(zone for zone in self.controller.snapshot["zones"] if zone["id"] == key)

    async def test_unavailable_primary_immediately_uses_backup_without_notification_spam(self):
        await self.prepare_adaptive_water()
        self.hass.states.water("sensor.upstairs", "unavailable")
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "fan_only")
        self.assertTrue(self.zone("upstairs")["backup_active"])
        self.assertEqual(self.zone("upstairs")["water_source_id"], "downstairs")
        self.assertEqual(self.zone("upstairs")["operating_water_temperature"], 34)
        self.assertIn("Downstairs", self.hass.notifications["central_heating_water_backup"]["message"])
        events = deepcopy(self.hass.notification_events)
        for _ in range(3):
            await self.run_controller()
        self.assertEqual(events, self.hass.notification_events)

    async def test_backup_works_in_both_directions(self):
        self.hass.entity_registry.add("climate.lounge", ["heating_downstairs"])
        self.hass.states.room("climate.lounge")
        await self.prepare_adaptive_water()
        for failed, backup in (("upstairs", "downstairs"), ("downstairs", "upstairs")):
            self.hass.states.water(f"sensor.{failed}", "unknown")
            await self.run_controller()
            self.assertEqual(self.zone(failed)["water_source_id"], backup)
            self.assertTrue(self.zone(failed)["backup_active"])
            self.assertEqual(self.hass.states.get("climate.office").state, "fan_only")
            self.assertEqual(self.hass.states.get("climate.lounge").state, "fan_only")
            self.hass.states.water(f"sensor.{failed}", 34)
            await self.run_controller()
            self.assertFalse(self.controller.snapshot["backup_active"])

    async def test_startup_with_missing_primary_confirms_backup_before_enabling(self):
        self.controller.settings.mode = "Adaptive"
        self.hass.states.values.pop("sensor.upstairs")
        self.hass.states.water("sensor.downstairs", 34)
        await self.run_controller()
        self.assertTrue(self.zone("upstairs")["backup_active"])
        self.assertEqual(self.zone("upstairs")["sensor_status"], "Confirming warm water")
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.clock += 29
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.clock += 1
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "fan_only")

    async def test_backup_cold_water_stops_fan_immediately(self):
        await self.prepare_adaptive_water()
        self.hass.states.water("sensor.upstairs", "unavailable")
        await self.run_controller()
        self.hass.states.water("sensor.downstairs", 30)
        await self.run_controller()
        self.assertTrue(self.zone("upstairs")["backup_active"])
        self.assertFalse(self.zone("upstairs")["allowed"])
        self.assertGreater(self.zone("upstairs")["operating_water_temperature"], 30)
        self.assertEqual(self.zone("downstairs")["raw_temperature"], 30)
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_backup_uses_its_own_hysteresis_not_failed_primary_latch(self):
        await self.prepare_adaptive_water(31)
        self.assertTrue(self.controller.gates["upstairs"])
        self.assertFalse(self.controller.gates["downstairs"])
        self.hass.states.water("sensor.upstairs", "unavailable")
        await self.run_controller()
        self.assertTrue(self.zone("upstairs")["backup_active"])
        self.assertFalse(self.zone("upstairs")["allowed"])
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_primary_recovery_uses_its_cold_reading_and_clears_warning(self):
        await self.prepare_adaptive_water()
        self.hass.states.water("sensor.upstairs", "unavailable")
        await self.run_controller()
        self.hass.states.water("sensor.upstairs", 29)
        await self.run_controller()
        self.assertFalse(self.controller.snapshot["backup_active"])
        self.assertEqual(self.zone("upstairs")["water_source_id"], "upstairs")
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.assertEqual(self.hass.notifications, {})

    async def test_both_unavailable_stop_adaptive_immediately_without_circular_backup(self):
        await self.prepare_adaptive_water()
        for key in ("upstairs", "downstairs"):
            self.hass.states.water(f"sensor.{key}", "unavailable")
        await self.run_controller()
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        self.assertFalse(self.controller.snapshot["backup_active"])
        for zone in self.controller.snapshot["zones"]:
            self.assertIsNone(zone["water_source_id"])
            self.assertFalse(zone["allowed"])
            self.assertEqual(zone["sensor_status"], "No usable water sensor")

    async def test_rejected_backup_reading_is_not_reused_for_failed_primary(self):
        await self.prepare_adaptive_water()
        self.hass.states.water("sensor.upstairs", "unavailable")
        self.hass.states.water("sensor.downstairs", 85)
        await self.run_controller()
        self.assertIsNone(self.zone("upstairs")["water_source_id"])
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_backup_loss_during_device_command_prevents_start(self):
        await self.prepare_adaptive_water()
        self.clock += 11
        self.hass.states.room("climate.office")
        self.hass.services.calls.clear()
        self.hass.states.water("sensor.upstairs", "unavailable")
        async def lose_backup(service, data):
            self.hass.states.water("sensor.downstairs", "unavailable")
        self.hass.services.hook = lose_backup
        await self.run_controller()
        self.assertEqual(self.fan_calls(), [("set_fan_mode", "medium")])
        self.assertEqual(self.hass.states.get("climate.office").state, "off")

    async def test_room_cutoff_and_off_mode_win_over_backup(self):
        await self.prepare_adaptive_water()
        self.hass.states.water("sensor.upstairs", "unavailable")
        self.hass.states.get("climate.office").attributes["current_temperature"] = 24
        await self.run_controller()
        self.assertTrue(self.zone("upstairs")["backup_active"])
        self.assertEqual(self.hass.states.get("climate.office").state, "off")
        await self.controller.async_set_setting("mode", "Off")
        self.assertFalse(self.controller.snapshot["backup_active"])
        self.assertEqual(self.hass.notifications, {})

    async def test_backup_is_recomputed_after_restart_and_notification_is_polish(self):
        await self.prepare_adaptive_water()
        self.hass.states.water("sensor.upstairs", "unavailable")
        await self.run_controller()
        await self.controller._store.async_save(self.controller._serialize())
        self.hass.config.language = "pl"
        restored = controller_module.HeatingController(self.hass, self.entry, self.zones)
        await restored.async_load()
        restored._started = True
        await restored.async_reconcile_now()
        self.assertTrue(restored.snapshot["backup_active"])
        self.assertEqual(restored.snapshot["zones"][0]["water_source_id"], "downstairs")
        self.assertIn("czujnik zapasowy", self.hass.notifications["central_heating_water_backup"]["title"])
        self.assertIn("czujnika Downstairs", self.hass.notifications["central_heating_water_backup"]["message"])

    async def test_configuration_links_resolve_each_actual_device(self):
        self.hass.entity_registry.entities["climate.office"].device_id = "thermostat-id"
        self.hass.entity_registry.add("sensor.upstairs", [])
        self.hass.entity_registry.entities["sensor.upstairs"].device_id = "shelly-id"
        await self.run_controller()
        self.assertEqual(self.controller.snapshot["rooms"][0]["configuration_url"], "/config/devices/device/thermostat-id")
        self.assertEqual(self.controller.snapshot["zones"][0]["configuration_url"], "/config/devices/device/shelly-id")


if __name__ == "__main__":
    unittest.main()
