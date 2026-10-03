"""Water smoothing, isolated spike rejection, and delayed zone enablement."""

from collections import deque
from math import expm1
from statistics import median_low

CONFIRM_SECONDS = 30
FAULT_SECONDS = 60
SMOOTH_SECONDS = 60
MEDIAN_HOLD_SECONDS = 60
SETTLE_C = 0.01
UNAVAILABLE = "Water sensor unavailable"


def select_water_sources(filters, previous, now):
    """Resolve primaries and healthy backups without chaining failed readers."""
    sources = {}
    for key, primary in filters.items():
        if primary.usable(now) and not primary.issue:
            sources[key] = key
        elif (
            primary.usable(now) and primary.issue != UNAVAILABLE
            and previous.get(key, key) == key
        ):
            # Brief rejected spikes retain only this sensor's existing grace.
            # Once on backup, stay there until the primary reports valid data.
            sources[key] = key
        else:
            sources[key] = next((
                other for other, backup in filters.items()
                if other != key and backup.usable(now) and not backup.issue
            ), None)
    return sources


class WaterFilter:
    """Observe real reports only; tick advances deadlines without adding samples."""

    def __init__(self):
        self.raw = None
        self.accepted = None
        self.samples = deque(maxlen=3)
        self.issue = None
        self.issue_since = None
        self.warm_since = None
        self.observed_at = None
        self.accepted_at = None
        self.rejected_readings = 0
        self._baseline = None
        self._median = None
        self._smooth_at = None
        self._reset_on_recovery = False

    def observe(self, value, now, settings):
        self.tick(now)
        self.raw = value
        self.observed_at = now
        # 85°C is the DS18B20 reset value. A gradual approach to 85°C is
        # legitimate; an abrupt jump or an unverified startup value is not.
        if value is None or not 0 <= value <= 120:
            self._reject(UNAVAILABLE, now)
            return
        if value == 85 and (self._baseline is None or abs(value - self._baseline) > 5):
            self._reject("Suspect 85°C sensor reset", now)
            return
        reset = self.accepted is None or self._reset_on_recovery or not self.usable(now)
        if reset:
            self.samples.clear()
        self.samples.append(value)
        filtered = median_low(self.samples)
        # Bootstrap with one valid observation. Two samples use the lower
        # value, so an isolated high second reading cannot enable a zone.
        # A valid cold report must still stop fans even during a large cooldown.
        if value > settings.water_off and abs(value - filtered) > 10:
            self._reject("Isolated temperature spike", now)
            return
        if reset:
            self.accepted = filtered
        self._baseline = value
        self._median = filtered
        self._smooth_at = now
        self._reset_on_recovery = False
        self.accepted_at = now
        self.issue = None
        self.issue_since = None
        if value <= settings.water_off:
            self.warm_since = None

    def tick(self, now):
        """Advance toward trusted data; never manufacture reports or extend grace."""
        if self._smooth_at is None or now <= self._smooth_at:
            return
        if not self.issue:
            # Shellys report changes, so a stable value may have no fresh reports.
            # Let the median guard a brief spike, then follow the last valid raw
            # value. Split at the deadline so results do not depend on tick rate.
            boundary = max(self._smooth_at, min(now, self.accepted_at + MEDIAN_HOLD_SECONDS))
            self._smooth(self._median, boundary - self._smooth_at)
            self._smooth(self._baseline, now - boundary)
        self._smooth_at = now

    def _smooth(self, target, elapsed):
        if elapsed <= 0:
            return
        self.accepted += -expm1(-elapsed / SMOOTH_SECONDS) * (target - self.accepted)
        # Settle within the raw sensor's displayed resolution so a steady value
        # exactly at the ON threshold can finish confirmation in finite time.
        if abs(target - self.accepted) < SETTLE_C:
            self.accepted = target

    def _reject(self, reason, now):
        if reason == UNAVAILABLE:
            self._reset_on_recovery = True
        self.issue = reason
        if self.issue_since is None:
            self.issue_since = now
        self.rejected_readings += 1
        self.warm_since = None

    def usable(self, now):
        return self.accepted is not None and (
            self.issue_since is None or now - self.issue_since < FAULT_SECONDS
        )

    def permission(self, previous, now, settings):
        self.tick(now)
        if not self.usable(now):
            self.warm_since = None
            return False
        if self.accepted <= settings.water_off or (not self.issue and self.raw <= settings.water_off):
            self.warm_since = None
            return False
        if self.issue:
            self.warm_since = None
            return previous
        if self.accepted < settings.water_on or self.raw < settings.water_on:
            self.warm_since = None
            return previous
        if previous:
            self.warm_since = None
            return True
        if self.warm_since is None:
            self.warm_since = now
        return now - self.warm_since >= CONFIRM_SECONDS

    def status(self, allowed, now, settings):
        if self.issue:
            return "Sensor fault" if not self.usable(now) else "Reading rejected · last good retained"
        if allowed:
            return "Water ready"
        if self.warm_since is not None:
            return "Confirming warm water"
        return "Waiting for warm water"
