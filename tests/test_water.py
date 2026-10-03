"""Replay noisy and genuine heating-water sequences against the filter."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

PATH = Path(__file__).parents[1] / "custom_components/central_heating/water.py"
SPEC = importlib.util.spec_from_file_location("water_policy", PATH)
water = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(water)


class WaterFilterTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(water_on=33, water_off=30)
        self.filter = water.WaterFilter()

    def report(self, value, now, previous=False):
        self.filter.observe(value, now, self.settings)
        return self.filter.permission(previous, now, self.settings)

    def test_startup_85_never_enables_even_if_reported_repeatedly(self):
        for now in (0, 10, 30, 60, 120):
            self.assertFalse(self.report(85, now))
        self.assertIsNone(self.filter.accepted)
        self.assertEqual(self.filter.issue, "Suspect 85°C sensor reset")

    def test_recorded_two_second_85_spike_cannot_enable_cold_zone(self):
        self.report(21.94, 0)
        self.report(22.31, 5)
        self.assertFalse(self.report(85, 10))
        self.assertEqual(self.filter.accepted, 21.94)
        self.assertFalse(self.report(21.94, 12.3))
        self.assertIsNone(self.filter.issue)
        self.assertEqual(self.filter.rejected_readings, 1)

    def test_high_second_sample_does_not_average_into_warm_permission(self):
        self.report(22, 0)
        self.report(90, 5)
        self.assertEqual(self.filter.accepted, 22)
        self.assertFalse(self.filter.permission(False, 40, self.settings))

    def test_median_removes_an_isolated_non_sentinel_spike(self):
        for now in (0, 5, 10):
            self.report(22, now)
        self.assertFalse(self.report(80, 15))
        self.assertEqual(self.filter.accepted, 22)
        self.assertFalse(self.report(22, 20))
        self.assertIsNone(self.filter.issue)

    def test_genuine_rapid_warmup_is_accepted_after_confirming_reports(self):
        for now in (0, 5, 10):
            self.report(22, now)
        self.report(55, 15)
        self.assertFalse(self.report(55, 20))
        self.assertEqual(self.filter.issue, None)
        self.assertLess(self.filter.accepted, 33)
        self.assertFalse(self.filter.permission(False, 80, self.settings))
        self.assertGreater(self.filter.accepted, 33)
        self.assertFalse(self.filter.permission(False, 109.9, self.settings))
        self.assertTrue(self.filter.permission(False, 110, self.settings))

    def test_slow_approach_to_real_85_and_higher_is_not_banned(self):
        for now, value in enumerate((80, 82, 84, 85, 85, 87)):
            self.report(value, now * 5)
            self.assertIsNone(self.filter.issue, value)
        self.filter.tick(1000)
        self.assertEqual(self.filter.accepted, 87)

    def test_warmup_is_reset_by_a_reading_below_on_threshold(self):
        self.report(34, 0)
        self.report(34, 5)
        self.report(32, 20)
        self.assertFalse(self.filter.permission(False, 31, self.settings))
        self.assertIsNone(self.filter.warm_since)

    def test_cold_water_wins_without_median_delay(self):
        for now in (0, 5, 10):
            self.report(90, now)
        self.assertFalse(self.report(25, 15, previous=True))
        self.assertGreater(self.filter.accepted, 30)
        self.assertFalse(self.filter.permission(False, 45, self.settings))
        self.assertIsNone(self.filter.issue)
        self.filter.tick(300)
        self.assertLess(self.filter.accepted, 30)

    def test_recorded_five_second_cold_dip_is_removed_from_smoothed_reading(self):
        for now in (0, 5, 10):
            self.report(22, now)
        self.report(21.06, 75)
        self.report(22, 80)
        self.filter.tick(200)
        self.assertEqual(self.filter.accepted, 22)
        self.assertIsNone(self.filter.issue)

    def test_single_sustained_change_settles_without_fresh_reports(self):
        for now in (0, 5, 10):
            self.report(22, now)
        self.report(21, 15)
        self.filter.tick(75)
        self.assertEqual(self.filter.accepted, 22)
        self.filter.tick(300)
        self.assertLess(self.filter.accepted, 21.05)
        self.assertGreaterEqual(self.filter.accepted, 21)
        self.assertEqual(self.filter.accepted_at, 15)
        self.assertEqual(self.filter.observed_at, 15)
        self.assertEqual(list(self.filter.samples), [22, 22, 21])

    def test_irregular_ticks_do_not_change_elapsed_time_smoothing(self):
        for now, value in ((0, 22), (5, 22), (10, 22), (15, 21)):
            self.report(value, now)
        other = water.WaterFilter()
        for now, value in ((0, 22), (5, 22), (10, 22), (15, 21)):
            other.observe(value, now, self.settings)
        for now in (31, 74, 81, 112, 139):
            self.filter.tick(now)
        other.tick(139)
        self.assertAlmostEqual(self.filter.accepted, other.accepted)
        self.assertEqual(self.filter.accepted_at, other.accepted_at)

    def test_rejected_reading_freezes_smoothing_without_extending_grace(self):
        self.report(40, 0)
        self.report(42, 5)
        self.report(42, 10)
        self.filter.tick(20)
        self.report(85, 20, previous=True)
        retained = self.filter.accepted
        self.filter.tick(50)
        self.assertEqual(self.filter.accepted, retained)
        self.assertEqual(self.filter.accepted_at, 10)
        self.filter.tick(80)
        self.assertFalse(self.filter.usable(80))

    def test_missing_sensor_recovery_discards_old_hot_samples(self):
        for now in (0, 5, 10):
            self.report(40, now)
        self.report(None, 15, previous=True)
        self.report(85, 16, previous=True)
        self.assertFalse(self.report(29, 20, previous=True))
        self.assertEqual(self.filter.accepted, 29)
        self.assertEqual(list(self.filter.samples), [29])

    def test_extended_spike_fault_recovery_reseeds_smoothing(self):
        self.report(22, 0)
        self.report(85, 5)
        self.assertFalse(self.report(40, 70))
        self.assertEqual(self.filter.accepted, 40)
        self.assertTrue(self.filter.permission(False, 100, self.settings))

    def test_exact_on_threshold_eventually_confirms_with_no_further_reports(self):
        self.report(32, 0)
        self.report(33, 5)
        self.assertFalse(self.filter.permission(False, 65, self.settings))
        self.assertFalse(self.filter.permission(False, 365, self.settings))
        self.assertEqual(self.filter.accepted, 33)
        self.assertTrue(self.filter.permission(False, 395, self.settings))

    def test_real_85_is_checked_against_validated_raw_not_lagging_smoothing(self):
        self.report(75, 0)
        for now, value in ((5, 78), (10, 81), (15, 84), (20, 85)):
            self.report(value, now)
            self.assertIsNone(self.filter.issue)
        self.assertLess(self.filter.accepted, 80)

    def test_small_updates_still_smooth_when_water_is_above_off_threshold(self):
        self.report(40, 0)
        self.report(41, 5)
        self.report(41, 10)
        self.filter.tick(40)
        self.assertGreater(self.filter.accepted, 40)
        self.assertLess(self.filter.accepted, 41)

    def test_raw_below_on_threshold_resets_confirmation_even_if_smoothed_is_hot(self):
        self.report(40, 0)
        self.report(32, 20)
        self.assertGreater(self.filter.accepted, 33)
        self.assertFalse(self.filter.permission(False, 100, self.settings))
        self.assertIsNone(self.filter.warm_since)

    def test_invalid_readings_hold_only_previously_enabled_zone_for_sixty_seconds(self):
        self.report(40, 0)
        self.assertTrue(self.report(85, 10, previous=True))
        self.assertTrue(self.filter.permission(True, 69.9, self.settings))
        self.assertFalse(self.filter.permission(True, 70, self.settings))
        self.assertFalse(self.filter.usable(70))
        self.assertEqual(self.filter.status(False, 70, self.settings), "Sensor fault")

    def test_repeated_invalid_reports_do_not_extend_fault_grace(self):
        self.report(40, 0)
        self.report(None, 5, previous=True)
        self.report(None, 40, previous=True)
        self.assertFalse(self.filter.permission(True, 65, self.settings))

    def test_recovery_after_fault_requires_new_confirmation(self):
        self.report(40, 0)
        self.report(None, 5, previous=True)
        self.filter.permission(True, 65, self.settings)
        self.assertFalse(self.report(40, 70))
        self.assertTrue(self.filter.permission(False, 100, self.settings))

    def test_deadband_remembers_zone_permission(self):
        self.report(31, 0)
        self.assertTrue(self.filter.permission(True, 100, self.settings))
        self.assertFalse(self.filter.permission(False, 100, self.settings))

    def test_out_of_range_and_missing_initial_readings_are_unusable(self):
        for value in (None, -1, 121):
            fresh = water.WaterFilter()
            fresh.observe(value, 0, self.settings)
            self.assertFalse(fresh.permission(True, 0, self.settings))


class WaterSourceTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(water_on=33, water_off=30)
        self.filters = {key: water.WaterFilter() for key in ("upstairs", "downstairs")}
        self.previous = {key: key for key in self.filters}
        for key, value in (("upstairs", 34), ("downstairs", 40)):
            self.filters[key].observe(value, 0, self.settings)

    def sources(self, now=0):
        self.previous = water.select_water_sources(self.filters, self.previous, now)
        return self.previous

    def test_healthy_readers_remain_independent(self):
        self.assertEqual(self.sources(), {"upstairs": "upstairs", "downstairs": "downstairs"})

    def test_isolated_spike_keeps_grace_then_switches_to_healthy_backup(self):
        self.filters["upstairs"].observe(85, 10, self.settings)
        self.assertEqual(self.sources(69.9)["upstairs"], "upstairs")
        self.assertEqual(self.sources(70)["upstairs"], "downstairs")

    def test_invalid_primary_after_unavailable_stays_on_backup_until_valid(self):
        primary = self.filters["upstairs"]
        primary.observe(None, 10, self.settings)
        self.assertEqual(self.sources(10)["upstairs"], "downstairs")
        primary.observe(85, 11, self.settings)
        self.assertEqual(self.sources(11)["upstairs"], "downstairs")
        primary.observe(34, 12, self.settings)
        self.assertEqual(self.sources(12)["upstairs"], "upstairs")

    def test_a_reader_retaining_invalid_grace_cannot_be_backup(self):
        self.filters["upstairs"].observe(None, 10, self.settings)
        self.filters["downstairs"].observe(85, 10, self.settings)
        self.assertIsNone(self.sources(10)["upstairs"])
        self.assertEqual(self.previous["downstairs"], "downstairs")
        self.assertEqual(self.sources(70), {"upstairs": None, "downstairs": None})

    def test_single_reader_failure_has_no_backup(self):
        only = self.filters["upstairs"]
        only.observe(None, 10, self.settings)
        self.assertEqual(water.select_water_sources({"upstairs": only}, {"upstairs": "upstairs"}, 10), {"upstairs": None})


if __name__ == "__main__":
    unittest.main()
