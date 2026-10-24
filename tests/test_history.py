"""Keep heating retention scoped to registry ownership and Recorder's queue."""

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1] / "custom_components/central_heating"


def module(name, **attributes):
    value = ModuleType(name)
    value.__dict__.update(attributes)
    return value


def registry_entry(entity_id, *, platform="central_heating", config_entry_id="heating", unique_id=None, disabled_by=None):
    return SimpleNamespace(entity_id=entity_id, domain=entity_id.split(".")[0], platform=platform,
                           config_entry_id=config_entry_id, unique_id=unique_id or entity_id,
                           disabled_by=disabled_by)


class PurgeEntitiesTask:
    def __init__(self, entity_filter, purge_before):
        self.entity_filter = entity_filter
        self.purge_before = purge_before


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.entries = []
        self.unload = []
        self.instance = SimpleNamespace(queue_task=Mock(), async_clear_statistics=Mock())
        self.recorded = set()
        self.timer_cancel, self.start_cancel = Mock(), Mock()
        self.track = Mock(return_value=self.timer_cancel)
        self.bus = SimpleNamespace(async_listen_once=Mock(return_value=self.start_cancel))
        self.hass = SimpleNamespace(is_running=True, bus=self.bus)
        self.entry = SimpleNamespace(entry_id="heating", async_on_unload=self.unload.append)
        self.controller = SimpleNamespace(hass=self.hass, entry=self.entry, history_error=None, _publish=Mock())
        self.recorder = module("homeassistant.components.recorder", get_instance=Mock(return_value=self.instance),
                               is_entity_recorded=lambda hass, entity_id: entity_id in self.recorded)
        registry = SimpleNamespace(async_get=lambda hass: self.entries,
            async_entries_for_config_entry=lambda entries, key: [entry for entry in entries if entry.config_entry_id == key])
        package = module("history_under_test")
        package.__path__ = [str(ROOT)]
        self.modules = patch.dict(sys.modules, {
            "history_under_test": package,
            "homeassistant": module("homeassistant"),
            "homeassistant.components": module("homeassistant.components"),
            "homeassistant.components.recorder": self.recorder,
            "homeassistant.components.recorder.tasks": module("homeassistant.components.recorder.tasks", PurgeEntitiesTask=PurgeEntitiesTask),
            "homeassistant.const": module("homeassistant.const", EVENT_HOMEASSISTANT_STARTED="started"),
            "homeassistant.core": module("homeassistant.core", callback=lambda fn: fn),
            "homeassistant.helpers": module("homeassistant.helpers", entity_registry=registry),
            "homeassistant.helpers.event": module("homeassistant.helpers.event", async_track_time_interval=self.track),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("history_under_test.history", ROOT / "history.py")
        self.history = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.history)
        self.retention = self.history.HistoryRetention(self.controller)

    def test_purge_uses_exact_owned_ids_including_renamed_and_disabled_entries(self):
        own = [registry_entry("sensor.renamed_room"), registry_entry("sensor.disabled_room", disabled_by="user"),
               registry_entry("number.renamed_target")]
        others = [registry_entry("sensor.central_heating_unrelated", config_entry_id="other"),
                  registry_entry("sensor.central_heating_foreign", platform="other")]
        self.entries.extend(own + others)
        self.retention._cleanup()
        task = self.instance.queue_task.call_args.args[0]
        for entry in own:
            self.assertTrue(task.entity_filter(entry.entity_id))
        for entity_id in [entry.entity_id for entry in others] + ["sensor.renamed_room_extra", "sensor.outdoor", ""]:
            self.assertFalse(task.entity_filter(entity_id))
        # A queued task owns a snapshot, independent of later registry changes.
        self.entries.clear()
        self.assertTrue(task.entity_filter("sensor.renamed_room"))
        self.instance.async_clear_statistics.assert_called_once_with(["sensor.disabled_room", "sensor.renamed_room"])

    def test_empty_or_foreign_only_registry_never_queues_global_purge(self):
        for entries in ([], [registry_entry("sensor.foreign", platform="other")],
                        [registry_entry("sensor.foreign", config_entry_id="other")]):
            self.entries[:] = entries
            self.retention._cleanup()
        self.instance.queue_task.assert_not_called()
        self.instance.async_clear_statistics.assert_not_called()
        self.recorder.get_instance.assert_not_called()

    def test_cutoff_is_utc_eleven_hours_fifty_minutes_and_timer_is_five_minutes(self):
        self.entries.append(registry_entry("sensor.room"))
        instant = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
        with patch.object(self.history, "datetime") as clock:
            clock.now.return_value = instant
            self.retention.async_start()
            clock.now.assert_called_once_with(UTC)
        task = self.instance.queue_task.call_args.args[0]
        self.assertEqual(task.purge_before, instant - timedelta(hours=11, minutes=50))
        self.track.assert_called_once_with(self.hass, self.retention._cleanup, timedelta(minutes=5))
        self.bus.async_listen_once.assert_not_called()
        self.assertEqual(self.unload, [self.timer_cancel])

    def test_statistics_clear_once_per_sensor_and_new_registry_id_is_cleared(self):
        self.entries.extend([registry_entry("sensor.room"), registry_entry("switch.heat")])
        self.retention._cleanup()
        self.retention._cleanup()
        self.instance.async_clear_statistics.assert_called_once_with(["sensor.room"])
        self.entries[0].entity_id = "sensor.room_renamed"
        self.entries.append(registry_entry("sensor.new_room", disabled_by="integration"))
        self.retention._cleanup()
        self.assertEqual(self.instance.async_clear_statistics.call_args.args, (["sensor.new_room", "sensor.room_renamed"],))
        self.assertEqual(self.instance.async_clear_statistics.call_count, 2)
        self.assertEqual(self.instance.queue_task.call_count, 3)

    def test_statistics_failure_retries_and_recovers_with_visible_fault(self):
        self.entries.append(registry_entry("sensor.room"))
        self.instance.async_clear_statistics.side_effect = RuntimeError("Recorder starting")
        with self.assertLogs(self.history.__name__, level="ERROR") as logs:
            self.retention._cleanup()
        self.assertIn("Recorder starting", self.controller.history_error)
        self.assertIn("Recorder starting", logs.output[0])
        self.controller._publish.assert_called_once()
        self.instance.queue_task.assert_not_called()
        self.instance.async_clear_statistics.side_effect = None
        self.retention._cleanup()
        self.assertIsNone(self.controller.history_error)
        self.assertEqual(self.instance.async_clear_statistics.call_count, 2)
        self.instance.queue_task.assert_called_once()

    def test_unavailable_recorder_does_not_escape_and_error_recovers(self):
        self.entries.append(registry_entry("sensor.room"))
        self.recorder.get_instance.side_effect = KeyError("recorder")
        with self.assertLogs(self.history.__name__, level="ERROR"):
            self.retention._cleanup()
        self.assertIn("Heating history cleanup unavailable", self.controller.history_error)
        self.recorder.get_instance.side_effect = None
        self.retention._cleanup()
        self.assertIsNone(self.controller.history_error)
        self.instance.queue_task.assert_called_once()

    def test_missing_internal_recorder_api_reports_fault_and_retries(self):
        self.entries.append(registry_entry("sensor.room"))
        tasks = sys.modules["homeassistant.components.recorder.tasks"]
        del tasks.PurgeEntitiesTask
        with self.assertLogs(self.history.__name__, level="ERROR"):
            self.retention._cleanup()
        self.assertIn("Heating history cleanup unavailable", self.controller.history_error)
        self.instance.queue_task.assert_not_called()
        tasks.PurgeEntitiesTask = PurgeEntitiesTask
        self.retention._cleanup()
        self.assertIsNone(self.controller.history_error)
        self.instance.queue_task.assert_called_once()

    def test_queue_failure_remains_visible_without_reclearing_statistics(self):
        self.entries.append(registry_entry("sensor.room"))
        self.instance.queue_task.side_effect = RuntimeError("queue stopped")
        with self.assertLogs(self.history.__name__, level="ERROR") as logs:
            self.retention._cleanup()
            self.retention._cleanup()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("queue stopped", self.controller.history_error)
        self.instance.async_clear_statistics.assert_called_once_with(["sensor.room"])
        self.instance.queue_task.side_effect = None
        self.retention._cleanup()
        self.assertIsNone(self.controller.history_error)
        self.instance.async_clear_statistics.assert_called_once()

    def test_renamed_status_warning_checks_unique_id_and_clears_after_exclusion(self):
        self.entries.extend([registry_entry("sensor.custom_name", unique_id="central_heating_room_status"),
                             registry_entry("sensor.foreign_status", platform="other", unique_id="other_status"),
                             registry_entry("sensor.temperature")])
        self.recorded.update(entry.entity_id for entry in self.entries)
        with self.assertLogs(self.history.__name__, level="ERROR"):
            self.retention._cleanup()
        self.assertIn("sensor.custom_name", self.controller.history_error)
        self.assertNotIn("sensor.foreign_status", self.controller.history_error)
        self.assertNotIn("sensor.temperature", self.controller.history_error)
        self.recorded.remove("sensor.custom_name")
        self.retention._cleanup()
        self.assertIsNone(self.controller.history_error)
        self.assertEqual(self.instance.queue_task.call_count, 2)

    def test_waits_for_startup_and_registers_both_unload_cancellations(self):
        self.entries.append(registry_entry("sensor.room"))
        self.hass.is_running = False
        self.retention.async_start()
        self.instance.queue_task.assert_not_called()
        self.bus.async_listen_once.assert_called_once_with("started", self.retention._cleanup)
        self.track.call_args.args[1](None)
        self.instance.queue_task.assert_not_called()
        self.hass.is_running = True
        self.bus.async_listen_once.call_args.args[1](None)
        self.instance.queue_task.assert_called_once()
        self.assertEqual(self.unload, [self.start_cancel, self.timer_cancel])
        for cancel in self.unload:
            cancel()
        self.start_cancel.assert_called_once()
        self.timer_cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
