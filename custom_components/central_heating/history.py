"""Heating-only rolling history cleanup through Home Assistant's Recorder queue.

Recorder's public purge action accepts whole days. Isolate its internal task API
here so incompatible upgrades report a history fault without stopping fan safety.
"""

from datetime import UTC, datetime, timedelta
import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
HISTORY_HOURS = 12
CLEANUP_INTERVAL = timedelta(minutes=5)
# Leave room for the next timer tick and normal Recorder queue latency.
RETAIN = timedelta(hours=HISTORY_HOURS) - timedelta(minutes=10)


class HistoryRetention:
    def __init__(self, controller):
        self.controller = controller
        self.hass = controller.hass
        self.entry = controller.entry
        self._statistics_cleared = set()
        self._last_error = None

    @callback
    def async_start(self):
        """Run after platform setup; defer statistics cleanup until HA is ready."""
        if self.hass.is_running:
            self._cleanup()
        else:
            self.entry.async_on_unload(self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._cleanup
            ))
        self.entry.async_on_unload(async_track_time_interval(
            self.hass, self._cleanup, CLEANUP_INTERVAL
        ))

    @callback
    def _cleanup(self, _event=None):
        if not self.hass.is_running:
            return
        try:
            from homeassistant.components.recorder import get_instance, is_entity_recorded
            from homeassistant.components.recorder.tasks import PurgeEntitiesTask

            registry = er.async_get(self.hass)
            entries = er.async_entries_for_config_entry(registry, self.entry.entry_id)
            owned = [entry for entry in entries if entry.platform == DOMAIN]
            ids = frozenset(entry.entity_id for entry in owned)
            if not ids:
                return
            instance = get_instance(self.hass)
            # Include disabled/renamed entries: data from previous versions must
            # not survive indefinitely after a room or sensor is retired.
            sensors = {entry.entity_id for entry in owned if entry.domain == "sensor"}
            new_statistics = sensors - self._statistics_cleared
            if new_statistics:
                instance.async_clear_statistics(sorted(new_statistics))
                self._statistics_cleared.update(new_statistics)
            instance.queue_task(PurgeEntitiesTask(ids.__contains__, datetime.now(UTC) - RETAIN))
            recorded_statuses = [entry.entity_id for entry in owned
                if entry.unique_id.endswith("_status") and is_entity_recorded(self.hass, entry.entity_id)]
            error = (
                "Exclude heating status sensors from Recorder to reduce writes: " + ", ".join(recorded_statuses)
                if recorded_statuses else None
            )
        except (ImportError, AttributeError, KeyError, RuntimeError, TypeError) as err:
            error = f"Heating history cleanup unavailable: {err}"
        if error != self._last_error:
            if error:
                _LOGGER.error("%s", error)
            self._last_error = error
        self.controller.history_error = error
        self.controller._publish()
