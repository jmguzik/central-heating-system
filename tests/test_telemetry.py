"""Reported fan metrics never infer commands, missing periods, or restart starts."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

PATH = Path(__file__).parents[1] / "custom_components/central_heating/telemetry.py"
SPEC = importlib.util.spec_from_file_location("fan_telemetry", PATH)
telemetry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telemetry)


def state(mode, speed=None):
    return SimpleNamespace(state=mode, attributes={"fan_mode": speed})


class FanTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.metrics = telemetry.FanTelemetry()

    def observe(self, mode, at, speed=None, room="bedroom"):
        self.metrics.observe(room, state(mode, speed) if mode is not None else None, at)
        return self.metrics.snapshot(room)

    def test_reported_transitions_and_per_speed_runtime(self):
        self.observe("off", 0)
        self.assertEqual(self.observe("fan_only", 10, "low")["fan_starts"], 1)
        self.observe("fan_only", 1810, "medium")
        totals = self.observe("off", 2710)
        self.assertEqual(totals, {"fan_starts": 1, "low_runtime_hours": 0.5, "medium_runtime_hours": 0.25})
        self.assertEqual(self.observe("fan_only", 2800, "medium")["fan_starts"], 2)
        self.assertEqual(self.metrics.snapshot("other")["fan_starts"], 0)

    def test_initial_active_state_does_not_count_as_start(self):
        self.observe("fan_only", 100, "low")
        totals = self.observe("fan_only", 1900, "low")
        self.assertEqual(totals["fan_starts"], 0)
        self.assertEqual(totals["low_runtime_hours"], 0.5)

    def test_missing_unknown_and_unavailable_break_runtime_and_start_chain(self):
        for missing in (None, "unknown", "unavailable"):
            with self.subTest(missing=missing):
                self.metrics = telemetry.FanTelemetry()
                self.observe("fan_only", 0, "low")
                self.observe("fan_only", 60, "low")
                self.observe(missing, 120)
                self.observe("fan_only", 3600, "medium")
                self.observe("off", 3660)
                totals = self.metrics.snapshot("bedroom")
                self.assertEqual(totals["fan_starts"], 0)
                self.assertAlmostEqual(totals["low_runtime_hours"], 1 / 60)
                self.assertAlmostEqual(totals["medium_runtime_hours"], 1 / 60)
                self.observe(missing, 3700)
                self.assertEqual(self.observe("fan_only", 3800, "low")["fan_starts"], 0)

    def test_unknown_and_high_speed_do_not_pollute_low_medium_runtime(self):
        self.observe("off", 0)
        self.observe("fan_only", 60, "high")
        self.observe("fan_only", 3600, "automatic")
        totals = self.observe("off", 7200)
        self.assertEqual(totals, {"fan_starts": 1, "low_runtime_hours": 0, "medium_runtime_hours": 0})

    def test_repeated_observation_is_idempotent_and_snapshot_is_copy(self):
        self.observe("off", 0)
        before = self.observe("fan_only", 60, "low")
        self.assertEqual(self.observe("fan_only", 60, "low"), before)
        before["fan_starts"] = 99
        self.assertEqual(self.metrics.snapshot("bedroom")["fan_starts"], 1)

    def test_persistence_checkpoints_runtime_and_preserves_starts_immediately(self):
        self.observe("off", 0)
        self.observe("fan_only", 1, "low")
        checkpoint = self.metrics.serialize()
        self.assertEqual(checkpoint["bedroom"]["fan_starts"], 1)
        self.observe("fan_only", 60, "low")
        self.assertEqual(self.metrics.serialize(), checkpoint)
        self.observe("fan_only", 61, "low")
        self.assertEqual(self.metrics.serialize()["bedroom"]["low_runtime_hours"], 1 / 60)
        self.observe("fan_only", 90, "low")
        restored = telemetry.FanTelemetry()
        restored.load(self.metrics.serialize())
        restored.observe("bedroom", state("fan_only", "low"), 100000)
        self.assertEqual(restored.snapshot("bedroom"), {"fan_starts": 1, "low_runtime_hours": 1 / 60, "medium_runtime_hours": 0})
        restored.observe("bedroom", state("off"), 100060)
        self.assertAlmostEqual(restored.snapshot("bedroom")["low_runtime_hours"], 2 / 60)

    def test_restore_rejects_invalid_saved_totals_and_resets_baselines(self):
        for bad in (float("nan"), float("inf"), -1, True, "12", None):
            with self.subTest(bad=bad):
                self.metrics.load({"bedroom": {"fan_starts": bad, "low_runtime_hours": bad, "medium_runtime_hours": bad}, "broken": []})
                self.assertEqual(self.metrics.snapshot("bedroom"), {"fan_starts": 0, "low_runtime_hours": 0, "medium_runtime_hours": 0})
        self.metrics.load(None)
        self.assertEqual(self.metrics.serialize(), {})

    def test_reversed_clock_does_not_decrease_totals_or_count_start(self):
        self.observe("off", 100)
        self.assertEqual(self.observe("fan_only", 90, "low")["fan_starts"], 0)
        self.assertAlmostEqual(self.observe("off", 150)["low_runtime_hours"], 1 / 60)


if __name__ == "__main__":
    unittest.main()
