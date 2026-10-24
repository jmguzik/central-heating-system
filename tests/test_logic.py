"""Boundary and sequence tests for the heating policy."""

from datetime import datetime
from zoneinfo import ZoneInfo
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
        self.memory.blocked = logic.room_blocked(room, self.memory.blocked, self.settings, self.memory)
        return logic.decide(self.settings, self.memory, room, allowed, **kwargs)

    def test_quiet_schedule_boundaries_and_start_days(self):
        self.memory.quiet_enabled = True
        self.memory.quiet_days = [0]
        zone = ZoneInfo("Europe/Warsaw")
        for timestamp, active in [
            ("2026-10-19T21:59", False), ("2026-10-19T22:00", True),
            ("2026-10-20T00:00", True), ("2026-10-20T06:59", True),
            ("2026-10-20T07:00", False), ("2026-10-20T22:00", False),
            ("2026-10-19T06:00", False),
        ]:
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.memory.quiet_active(datetime.fromisoformat(timestamp).replace(tzinfo=zone)), active)
        self.memory.quiet_days = [6]
        self.assertTrue(self.memory.quiet_active(datetime(2026, 10, 19, 6, tzinfo=zone)))
        self.memory.quiet_days = [0]
        self.memory.quiet_start, self.memory.quiet_end = "09:00", "17:00"
        for hour, active in [(8, False), (9, True), (16, True), (17, False)]:
            self.assertEqual(self.memory.quiet_active(datetime(2026, 10, 19, hour, tzinfo=zone)), active)

    def test_quiet_schedule_dst_uses_local_wall_clock(self):
        self.memory.quiet_enabled = True
        self.memory.quiet_days = [5]
        zone = ZoneInfo("Europe/Warsaw")
        # Both occurrences of the repeated autumn hour belong to Saturday night.
        for fold in (0, 1):
            self.assertTrue(self.memory.quiet_active(datetime(2026, 10, 25, 2, 30, fold=fold, tzinfo=zone)))
        self.assertTrue(self.memory.quiet_active(datetime(2026, 3, 29, 3, 0, tzinfo=zone)))
        self.assertFalse(self.memory.quiet_active(datetime(2026, 3, 29, 7, 0, tzinfo=zone)))

    def test_quiet_caps_speed_without_corrupting_adaptive_memory(self):
        quiet = self.decision(20, quiet_active=True)
        self.assertEqual((quiet.fan_mode, quiet.reason), ("low", "Quiet hours · low"))
        self.assertEqual(self.memory.adaptive_fan, "medium")
        self.assertEqual(self.decision(21, quiet_active=False).fan_mode, "medium")
        self.decision(22, quiet_active=True)
        self.assertEqual(self.memory.adaptive_fan, "low")
        self.assertEqual(self.decision(21, quiet_active=False).fan_mode, "low")

    def test_quiet_never_bypasses_stop_conditions(self):
        for kwargs in ({"water_valid": False}, {"available": False}, {"membership_valid": False}, {"compatible": False}):
            self.assertEqual(self.decision(20, quiet_active=True, **kwargs).hvac_mode, "off")
        self.assertEqual(self.decision(20, allowed=False, quiet_active=True).hvac_mode, "off")
        self.assertEqual(self.decision(None, quiet_active=True).hvac_mode, "off")
        self.assertEqual(self.decision(24, quiet_active=True).hvac_mode, "off")
        self.settings.mode = "Off"
        self.assertEqual(self.decision(20, quiet_active=True).hvac_mode, "off")
        self.settings.mode = "Manual"
        self.assertEqual(self.decision(20, quiet_active=True).reason, "Manual · low")

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

    def test_half_degree_readings_keep_speed_in_the_deadband(self):
        for room, expected in [(21, "low"), (20.5, "medium"), (21, "medium"), (20.5, "medium"), (21.5, "low"), (21, "low")]:
            self.assertEqual(self.decision(room).fan_mode, expected, room)

    def test_room_specific_limits_have_independent_cutoff_and_restart_latches(self):
        for mode in ("Manual", "Adaptive"):
            with self.subTest(mode=mode):
                settings = logic.Settings(mode=mode)
                rooms = [
                    logic.RoomMemory(blocked=False, room_maximum=23, room_hysteresis=1),
                    logic.RoomMemory(blocked=False, room_maximum=25, room_hysteresis=0.5),
                ]
                for value, expected in [(22, (False, False)), (23, (True, False)),
                                        (22.5, (True, False)), (22, (False, False)),
                                        (25, (True, True)), (24.6, (True, True)),
                                        (24.5, (True, False))]:
                    for memory, blocked in zip(rooms, expected):
                        memory.blocked = logic.room_blocked(value, memory.blocked, settings, memory)
                        self.assertEqual(memory.blocked, blocked, (mode, value))
                        decision = logic.decide(settings, memory, value, True)
                        self.assertEqual(decision.hvac_mode, "off" if blocked else "fan_only")

    def test_room_limits_inherit_globals_and_valid_overrides_survive_roundtrip(self):
        memory = logic.RoomMemory()
        self.assertFalse(memory.limits_override)
        self.assertEqual(memory.effective_room_limits(self.settings), (24, 0.5))
        self.settings.room_maximum = 26
        self.settings.room_hysteresis = 1
        self.assertEqual(memory.effective_room_limits(self.settings), (26, 1))
        memory.room_maximum = 23
        memory.room_hysteresis = 0.75
        restored = logic.RoomMemory(**memory.serialize())
        self.assertTrue(restored.limits_override)
        self.assertEqual(restored.effective_room_limits(self.settings), (23, 0.75))
        self.assertFalse(logic.room_blocked(24.5, False, self.settings))
        self.assertTrue(logic.room_blocked(24.5, False, self.settings, restored))

    def test_invalid_memory_limits_use_globals(self):
        for maximum, hysteresis in [(float("nan"), float("inf")), (True, False), (4, 6)]:
            with self.subTest(maximum=maximum, hysteresis=hysteresis):
                memory = logic.RoomMemory(room_maximum=maximum, room_hysteresis=hysteresis)
                self.assertEqual(memory.effective_room_limits(self.settings), (24, 0.5))

    def test_override_changes_only_its_room(self):
        self.memory.target = 23
        self.memory.override = True
        self.assertEqual(self.decision(21.5).fan_mode, "medium")
        other = logic.RoomMemory(blocked=False)
        self.assertEqual(logic.decide(self.settings, other, 21.5, True).fan_mode, "low")
        self.assertEqual(self.decision(22).fan_mode, "medium")
        self.assertEqual(self.decision(22.5).fan_mode, "low")

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

    def test_speed_memory_survives_restart(self):
        self.decision(20.5)
        restored = logic.RoomMemory(**self.memory.serialize())
        self.assertEqual(logic.decide(self.settings, restored, 21, True).fan_mode, "medium")

    def test_override_expiry_retains_saved_target_and_returns_to_central(self):
        self.memory.override = True
        self.memory.target = 23
        self.memory.override_expires_at = 100
        self.assertFalse(self.memory.expire_override(99))
        self.settings.target = 21.5
        self.assertTrue(self.memory.expire_override(100))
        self.assertEqual(self.memory.effective_target(self.settings), 21.5)
        self.assertEqual(self.memory.target, 23)
        self.assertIsNone(self.memory.override_expires_at)


if __name__ == "__main__":
    unittest.main()
