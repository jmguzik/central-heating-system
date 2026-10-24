"""History entities expose finite measurements and reported fan activity."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def module(name, **attributes):
    value = ModuleType(name)
    value.__dict__.update(attributes)
    return value


class HeatingEntity:
    def __init__(self, controller, key, name, platform, room=None):
        self.controller, self.key, self.room = controller, key, room

    @property
    def room_data(self):
        return next((item for item in self.controller.snapshot["rooms"] if item["key"] == self.room.key), {})

    def _changed(self):
        self.controller.publications.append((self.key, self.force_update))


def add_room_entities(controller, entry, add, factories):
    add([factory(controller, room) for room in controller.rooms.values() for factory in factories])


PATH = Path(__file__).parents[1] / "custom_components/central_heating/sensor.py"
TELEMETRY_SPEC = importlib.util.spec_from_file_location("sensor_test_package.telemetry", PATH.with_name("telemetry.py"))
telemetry = importlib.util.module_from_spec(TELEMETRY_SPEC)
TELEMETRY_SPEC.loader.exec_module(telemetry)
SPEC = importlib.util.spec_from_file_location("sensor_test_package.sensor", PATH)
sensors = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {
    "homeassistant.components.sensor": module("homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        SensorDeviceClass=SimpleNamespace(TEMPERATURE="temperature", DURATION="duration"),
        SensorStateClass=SimpleNamespace(MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing")),
    "homeassistant.const": module("homeassistant.const", UnitOfTemperature=SimpleNamespace(CELSIUS="°C"), UnitOfTime=SimpleNamespace(HOURS="h")),
    "homeassistant.core": module("homeassistant.core", callback=lambda fn: fn),
    "sensor_test_package": module("sensor_test_package"),
    "sensor_test_package.entity": module("sensor_test_package.entity", HeatingEntity=HeatingEntity, add_room_entities=add_room_entities),
    "sensor_test_package.telemetry": telemetry,
}):
    SPEC.loader.exec_module(sensors)


class HistorySensorTests(unittest.TestCase):
    def setUp(self):
        self.room = SimpleNamespace(key="bedroom")
        self.data = {"key": "bedroom", "effective_target": 21, "reported_mode": "fan_only", "reported_fan": "low", "fan_starts": 12, "low_runtime_hours": 1.25, "medium_runtime_hours": 0.5}
        self.comfort = {"weighted_temperature": 20.5, "effective_target": 22, "native_target": 23, "reference_temperature": 21.5}
        self.controller = SimpleNamespace(settings=SimpleNamespace(target=20), snapshot={"comfort": self.comfort, "running_fans": 1, "rooms": [self.data]}, rooms={"bedroom": self.room}, zones={})

    def central(self, key):
        return sensors.HeatingMetric(self.controller, key, key)

    def test_central_values_and_target_fallback(self):
        expected = {"weighted_temperature": 20.5, "home_target": 22, "native_target": 23, "native_correction": 1.5, "running_fans": 1}
        for key, value in expected.items():
            self.assertEqual(self.central(key).native_value, value)
        self.comfort.clear()
        self.assertEqual(self.central("home_target").native_value, 20)
        self.assertIsNone(self.central("native_target").native_value)
        self.assertIsNone(self.central("native_correction").native_value)

    def test_cloud_outage_and_stale_reference_leave_gap_in_correction(self):
        self.comfort["connection_retrying"] = True
        self.assertIsNone(self.central("native_correction").native_value)
        self.assertIsNone(self.central("native_target").native_value)
        self.comfort["connection_retrying"] = False
        self.comfort["reference_temperature"] = None
        self.assertIsNone(self.central("native_correction").native_value)
        for invalid in (float("nan"), float("inf"), True, "23"):
            self.comfort["weighted_temperature"] = invalid
            self.assertIsNone(self.central("weighted_temperature").native_value)

    def test_delta_metadata_does_not_apply_absolute_temperature_conversion(self):
        correction = self.central("native_correction")
        self.assertEqual(correction._attr_native_unit_of_measurement, "°C")
        self.assertIsNone(getattr(correction, "_attr_state_class", None))
        self.assertIsNone(getattr(correction, "_attr_device_class", None))
        self.assertEqual(self.central("native_target")._attr_device_class, "temperature")

    def measured_entities(self):
        self.data["temperature"] = 20.53
        self.comfort["weighted_temperature"] = 20.53
        self.comfort.update(native_target=23.03, reference_temperature=21.5)
        self.water = SimpleNamespace(accepted=40.53, usable=lambda now: True)
        self.controller.water_filters = {"downstairs": self.water}
        return [
            self.central("weighted_temperature"),
            self.central("native_correction"),
            sensors.RoomTemperature(self.controller, self.room),
            sensors.WaterTemperature(self.controller, "downstairs", {"name": "Downstairs"}),
        ]

    def test_measured_history_is_rounded_and_sampled_every_minute(self):
        entities = self.measured_entities()
        with patch.object(sensors, "monotonic", return_value=100) as clock:
            self.assertEqual([entity.native_value for entity in entities], [20.5, 1.5, 20.5, 40.5])
            self.data["temperature"] = 20.67
            self.comfort.update(weighted_temperature=20.67, native_target=23.17)
            self.water.accepted = 40.67
            for instant in (100, 105, 130, 159.999):
                clock.return_value = instant
                # Repeated property reads cannot move the sampling boundary.
                for _ in range(2):
                    self.assertEqual([entity.native_value for entity in entities], [20.5, 1.5, 20.5, 40.5])
            clock.return_value = 160
            self.assertEqual([entity.native_value for entity in entities], [20.7, 1.7, 20.7, 40.7])
        self.assertEqual(self.controller.snapshot["rooms"][0]["temperature"], 20.67)
        self.assertEqual(self.controller.snapshot["comfort"]["weighted_temperature"], 20.67)
        self.assertEqual(self.water.accepted, 40.67)

    def test_measured_faults_and_recovery_bypass_minute_sampling(self):
        entities = self.measured_entities()
        with patch.object(sensors, "monotonic", return_value=100) as clock:
            self.assertTrue(all(entity.native_value is not None for entity in entities))
            clock.return_value = 105
            self.data["temperature"] = float("nan")
            self.comfort["weighted_temperature"] = float("inf")
            self.comfort["connection_retrying"] = True
            self.water.usable = lambda now: False
            self.assertEqual([entity.native_value for entity in entities], [None] * 4)
            clock.return_value = 106
            self.data["temperature"] = 21.23
            self.comfort.update(weighted_temperature=21.23, native_target=23.73, connection_retrying=False)
            self.water.usable = lambda now: True
            self.water.accepted = 41.23
            self.assertEqual([entity.native_value for entity in entities], [21.2, 2.2, 21.2, 41.2])

    def test_missing_and_non_numeric_measurements_leave_history_gaps(self):
        entity = sensors.RoomTemperature(self.controller, self.room)
        with patch.object(sensors, "monotonic", return_value=100):
            for invalid in (None, True, "21", float("nan"), float("inf")):
                self.data["temperature"] = 21
                self.assertEqual(entity.native_value, 21)
                self.data["temperature"] = invalid
                self.assertIsNone(entity.native_value)
            self.data.pop("temperature")
            self.assertIsNone(entity.native_value)

    def test_measurements_have_independent_entity_caches(self):
        first = self.central("weighted_temperature")
        second = self.central("weighted_temperature")
        with patch.object(sensors, "monotonic", return_value=100) as clock:
            self.assertEqual(first.native_value, 20.5)
            self.comfort["weighted_temperature"] = 21.24
            clock.return_value = 105
            self.assertEqual(second.native_value, 21.2)
            self.assertEqual(first.native_value, 20.5)

    def test_targets_and_fan_activity_change_immediately(self):
        home = self.central("home_target")
        native = self.central("native_target")
        room = sensors.RoomTargetTemperature(self.controller, self.room)
        speed = sensors.RoomFanSpeed(self.controller, self.room)
        starts = sensors.RoomFanStarts(self.controller, self.room)
        with patch.object(sensors, "monotonic", return_value=100):
            self.assertEqual([entity.native_value for entity in (home, native, room, speed, starts)], [22, 23, 21, "low", 12])
            self.comfort.update(effective_target=22.24, native_target=23.24)
            self.data.update(effective_target=21.24, reported_fan="medium", fan_starts=13)
            self.assertEqual([entity.native_value for entity in (home, native, room, speed, starts)], [22.2, 23.2, 21.2, "medium", 13])

    def test_room_metrics_and_accumulator_metadata(self):
        expected = ((sensors.RoomTargetTemperature, "target_temperature", 21), (sensors.RoomFanStarts, "fan_starts", 12), (sensors.RoomLowRuntime, "low_runtime", 1.25), (sensors.RoomMediumRuntime, "medium_runtime", 0.5))
        for factory, key, value in expected:
            entity = factory(self.controller, self.room)
            self.assertEqual(entity.key, key)
            self.assertEqual(entity.native_value, value)
        runtime = sensors.RoomLowRuntime(self.controller, self.room)
        self.assertEqual(runtime._attr_native_unit_of_measurement, "h")
        self.assertEqual(runtime._attr_device_class, "duration")
        self.assertIsNone(getattr(runtime, "_attr_state_class", None))
        self.assertEqual(sensors.RoomFanStarts(self.controller, self.room)._attr_native_unit_of_measurement, "starts")
        self.assertEqual(self.central("running_fans")._attr_native_unit_of_measurement, "fans")
        self.assertEqual(sensors.HomeFanMetric(self.controller, "fan_starts", "Starts")._attr_native_unit_of_measurement, "starts")

    def test_speed_timeline_uses_reported_not_desired(self):
        speed = sensors.RoomFanSpeed(self.controller, self.room)
        self.data.update(desired_mode="off", desired_fan="medium")
        self.assertEqual(speed.native_value, "low")
        self.data.update(reported_mode="off")
        self.assertEqual(speed.native_value, "off")
        for mode, fan in (("unavailable", "low"), ("unknown", "low"), ("fan_only", None), ("heat", "medium")):
            self.data.update(reported_mode=mode, reported_fan=fan)
            self.assertIsNone(speed.native_value)

    def test_runtime_recorder_totals_do_not_decrease_after_restore(self):
        for speed, factory in (("low", sensors.RoomLowRuntime), ("medium", sensors.RoomMediumRuntime)):
            with self.subTest(speed=speed):
                metrics = telemetry.FanTelemetry()
                state = SimpleNamespace(state="fan_only", attributes={"fan_mode": speed})
                metrics.observe("bedroom", state, 0)
                metrics.observe("bedroom", state, 90)
                self.data.update(metrics.snapshot("bedroom"))
                sensor = factory(self.controller, self.room)
                before_restart = sensor.native_value
                self.assertEqual(before_restart, 1 / 60)
                self.assertIsNone(getattr(sensor, "_attr_state_class", None))
                restored = telemetry.FanTelemetry()
                restored.load(metrics.serialize())
                restored.observe("bedroom", state, 10000)
                self.data.update(restored.snapshot("bedroom"))
                self.assertEqual(sensor.native_value, before_restart)
                restored.observe("bedroom", state, 10060)
                self.data.update(restored.snapshot("bedroom"))
                # The raw history increment remains one minute after restart.
                self.assertAlmostEqual(sensor.native_value - before_restart, 1 / 60)

    def test_setup_registers_all_history_sensors_without_room_control_key_collisions(self):
        entities = []
        asyncio.run(sensors.async_setup_entry(None, SimpleNamespace(runtime_data=self.controller), entities.extend))
        room_keys = {entity.key for entity in entities if entity.room is not None}
        self.assertEqual(room_keys, {"status", "temperature", "target_temperature", "fan_speed", "fan_starts", "low_runtime", "medium_runtime"})
        self.assertNotIn("target", room_keys)
        self.assertEqual({entity.key for entity in entities if entity.room is None}, {"status", "weighted_temperature", "home_target", "native_target", "native_correction", "running_fans", "average_temperature", "fan_starts", "low_runtime", "medium_runtime"})

    def test_whole_home_sums_only_current_managed_rooms(self):
        self.data["temperature"] = 20
        self.controller.snapshot["rooms"].extend([
            {"key": "office", "temperature": 22, "fan_starts": 3, "low_runtime_hours": 0.5, "medium_runtime_hours": 1},
            {"key": "missing", "temperature": None, "fan_starts": 0, "low_runtime_hours": 0, "medium_runtime_hours": 0},
        ])
        self.assertEqual(self.central("average_temperature").native_value, 21)
        for key, expected in (("fan_starts", 15), ("low_runtime", 1.75), ("medium_runtime", 1.5)):
            sensor = sensors.HomeFanMetric(self.controller, key, key)
            self.assertEqual(sensor.native_value, expected)
            self.assertIsNone(getattr(sensor, "_attr_state_class", None))
        self.controller.snapshot["rooms"] = []
        self.assertIsNone(self.central("average_temperature").native_value)
        self.assertEqual(sensors.HomeFanMetric(self.controller, "fan_starts", "Starts").native_value, 0)

    def test_missing_home_counter_leaves_gap_instead_of_false_zero(self):
        self.data["low_runtime_hours"] = None
        self.assertIsNone(sensors.HomeFanMetric(self.controller, "low_runtime", "Low runtime").native_value)

    def test_minute_baseline_keeps_stable_values_visible_without_five_second_rows(self):
        sensor = self.central("home_target")
        self.controller.publications = []
        self.assertFalse(sensor.force_update)
        with patch.object(sensors, "time", return_value=120) as clock:
            sensor._changed()
            self.assertTrue(sensor.force_update)
            clock.return_value = 125
            sensor._changed()
            self.assertFalse(sensor.force_update)
            clock.return_value = 175
            sensor._changed()
            clock.return_value = 180
            sensor._changed()
            self.assertTrue(sensor.force_update)
            clock.return_value = 185
            sensor._changed()
            self.assertFalse(sensor.force_update)
            self.assertEqual(sensor.native_value, 22)
        self.assertEqual([forced for _, forced in self.controller.publications], [True, False, False, True, False])

    def test_no_history_sensor_produces_indefinite_statistics(self):
        self.controller.water_filters = {"upstairs": SimpleNamespace(accepted=40, usable=lambda now: True)}
        self.controller.zones = {"upstairs": {"name": "Upstairs"}}
        entities = []
        asyncio.run(sensors.async_setup_entry(None, SimpleNamespace(runtime_data=self.controller), entities.extend))
        for sensor in entities:
            self.assertIsNone(getattr(sensor, "_attr_state_class", None), sensor.key)
        self.assertEqual(sensors.HeatingStatus._unrecorded_attributes, frozenset({"*"}))
        self.assertEqual(sensors.RoomStatus._unrecorded_attributes, frozenset({"*"}))


if __name__ == "__main__":
    unittest.main()
