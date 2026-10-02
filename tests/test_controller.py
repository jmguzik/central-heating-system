"""Exercise asynchronous device commands with an in-memory HA adapter."""

import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest

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
module("homeassistant.const", EVENT_HOMEASSISTANT_STARTED="started", EVENT_HOMEASSISTANT_STOP="stop", EVENT_STATE_CHANGED="state_changed")
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
for name in ("const", "logic", "controller"):
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
            id=entity_id, entity_id=entity_id, domain="climate", labels=set(labels),
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

    def get(self, entity_id):
        return self.values.get(entity_id)

    def water(self, entity_id, value):
        self.values[entity_id] = SimpleNamespace(state=str(value), attributes={"unit_of_measurement": "°C"}, name=entity_id)

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


class Hass:
    def __init__(self):
        self.storage = {}
        self.states = States()
        self.services = Services(self)
        self.entity_registry = Registry()
        self.label_registry = Labels()
        self.device_registry = SimpleNamespace(async_get=lambda _: None)
        self.area_registry = SimpleNamespace(async_get_area=lambda _: None)
        self.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))
        self.is_running = True
        self.bus = SimpleNamespace(async_listen=lambda *args: lambda: None, async_listen_once=lambda *args: lambda: None)

    def async_create_task(self, coroutine, name):
        return asyncio.create_task(coroutine, name=name)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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


if __name__ == "__main__":
    unittest.main()
