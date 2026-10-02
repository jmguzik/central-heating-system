"""Boundary and sequence tests for the heating policy."""

import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = Path(__file__).parents[1] / "custom_components/central_heating/logic.py"
SPEC = importlib.util.spec_from_file_location("heating_logic", MODULE_PATH)
logic = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logic
SPEC.loader.exec_module(logic)


class HeatingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = logic.Settings(mode="Adaptive")
        self.memory = logic.RoomMemory(blocked=False)

    def decision(self, room=20, allowed=True, **kwargs):
        self.memory.blocked = logic.room_blocked(room, self.memory.blocked, self.settings)
        return logic.decide(self.settings, self.memory, room, allowed, **kwargs)

    def test_water_heats_and_cools_through_deadband(self):
        allowed = False
        for water, expected in [(29, False), (32.9, False), (33, True), (32, True), (30.1, True), (30, False), (32, False)]:
            allowed = logic.zone_permission(water, allowed, self.settings)
            self.assertEqual(allowed, expected, water)

    def test_two_zones_are_independent(self):
        first = logic.zone_permission(34, False, self.settings)
        second = logic.zone_permission(29, True, self.settings)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.decision(20, first).fan_mode, "medium")
        self.assertEqual(self.decision(20, second).hvac_mode, "off")

    def test_room_guard_heats_and_cools_through_deadband(self):
        blocked = False
        for room, expected in [(23.9, False), (24, True), (23.9, True), (23.6, True), (23.5, False)]:
            blocked = logic.room_blocked(room, blocked, self.settings)
            self.assertEqual(blocked, expected, room)

    def test_cutoff_overrides_manual_and_adaptive(self):
        for mode in ("Manual", "Adaptive"):
            self.settings.mode = mode
            self.assertEqual(self.decision(24).hvac_mode, "off")
            self.assertEqual(self.decision(23.8).hvac_mode, "off")
            self.assertEqual(self.decision(23.5).fan_mode, "low")

    def test_room_release_does_not_bypass_cold_water(self):
        self.decision(24)
        result = self.decision(23.5, False)
        self.assertFalse(self.memory.blocked)
        self.assertEqual(result.hvac_mode, "off")

    def test_room_guard_remembers_warm_room_while_water_is_off(self):
        self.decision(24, False)
        self.assertEqual(self.decision(23.8, True).hvac_mode, "off")

    def test_medium_boundary_is_strict(self):
        self.assertEqual(self.decision(20.99).fan_mode, "medium")
        self.assertEqual(self.decision(21).fan_mode, "low")
        self.assertEqual(self.decision(22).fan_mode, "low")
        self.assertEqual(self.decision(23).fan_mode, "low")

    def test_override_changes_only_its_room(self):
        self.memory.target = 23
        self.memory.override = True
        self.assertEqual(self.decision(21.5).fan_mode, "medium")
        other = logic.RoomMemory(blocked=False)
        self.assertEqual(logic.decide(self.settings, other, 21.5, True).fan_mode, "low")
        self.assertEqual(self.decision(22).fan_mode, "low")

    def test_central_changes_do_not_replace_override(self):
        self.memory.target = 23
        self.memory.override = True
        self.settings.target = 20
        self.assertEqual(self.memory.effective_target(self.settings), 23)
        self.memory.override = False
        self.assertEqual(self.memory.effective_target(self.settings), 20)
        self.assertEqual(self.memory.target, 23)

    def test_manual_ignores_target_and_missing_water(self):
        self.settings.mode = "Manual"
        self.memory.override = True
        self.memory.target = 30
        self.assertEqual(self.decision(15, False, water_valid=False).fan_mode, "low")

    def test_off_cannot_be_overridden(self):
        self.settings.mode = "Off"
        self.memory.override = True
        self.memory.target = 30
        self.assertEqual(self.decision(15).hvac_mode, "off")

    def test_invalid_room_temperature_blocks_only_that_room(self):
        self.assertEqual(self.decision(None).hvac_mode, "off")
        other = logic.RoomMemory(blocked=False)
        self.assertEqual(logic.decide(self.settings, other, 20, True).fan_mode, "medium")

    def test_invalid_water_clears_zone_permission(self):
        self.assertFalse(logic.zone_permission(None, True, self.settings))
        self.assertFalse(logic.zone_permission(200, True, self.settings))
        self.assertFalse(logic.zone_permission(31, False, self.settings))

    def test_start_without_memory_is_conservative_in_deadbands(self):
        self.assertFalse(logic.zone_permission(31.5, False, self.settings))
        self.assertTrue(logic.room_blocked(23.8, True, self.settings))

    def test_conflicting_labels_block_room(self):
        result = self.decision(20, membership_valid=False)
        self.assertEqual(result.hvac_mode, "off")
        self.assertIn("Conflicting", result.reason)

    def test_unreachable_or_unsupported_thermostat_is_not_started(self):
        self.assertEqual(self.decision(20, available=False).hvac_mode, "off")
        self.assertEqual(self.decision(20, compatible=False).hvac_mode, "off")

    def test_temperature_rejects_unknown_nonfinite_and_boolean_values(self):
        for value in (None, "unknown", "unavailable", "NaN", "inf", True):
            self.assertIsNone(logic.temperature(value), value)
        self.assertEqual(logic.temperature("33"), 33)
        self.assertAlmostEqual(logic.temperature(71.6, "°F"), 22)

    def test_invalid_settings_are_rejected(self):
        for changes in ({"water_on": 30}, {"water_off": 33}, {"target": float("nan")}, {"room_hysteresis": 0}, {"mode": "heat"}):
            settings = logic.Settings(**changes)
            with self.assertRaises(ValueError):
                settings.validate()

    def test_invalid_custom_target_falls_back_to_central(self):
        self.memory.override = True
        self.memory.target = float("nan")
        self.assertEqual(self.memory.effective_target(self.settings), 22)

    def test_memories_can_roundtrip_without_using_thermostat_state(self):
        original = logic.RoomMemory(blocked=True, override=True, target=23)
        restored = logic.RoomMemory(**original.serialize())
        self.assertTrue(restored.blocked)
        self.assertEqual(restored.effective_target(self.settings), 23)
        settings = logic.Settings(**self.settings.serialize())
        settings.validate()
        self.assertEqual(settings, self.settings)


if __name__ == "__main__":
    unittest.main()

