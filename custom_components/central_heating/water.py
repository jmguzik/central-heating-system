"""Water readings, isolated spike rejection, and delayed zone enablement."""

from collections import deque
from statistics import median_low

CONFIRM_SECONDS = 30
FAULT_SECONDS = 60
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

    def observe(self, value, now, settings):
        self.raw = value
        self.observed_at = now
        # 85°C is the DS18B20 reset value. A gradual approach to 85°C is
        # legitimate; an abrupt jump or an unverified startup value is not.
        if value is None or not 0 <= value <= 120:
            self._reject(UNAVAILABLE, now)
            return
        if value == 85 and (self.accepted is None or abs(value - self.accepted) > 5):
            self._reject("Suspect 85°C sensor reset", now)
            return
        self.samples.append(value)
        filtered = median_low(self.samples)
        if value <= settings.water_off:
            # Cold water wins immediately, including a rapid real cooldown.
            filtered = value
        # Bootstrap with one valid observation. Two samples use the lower
        # value, so an isolated high second reading cannot enable a zone.
        if abs(value - filtered) > 10:
            self._reject("Isolated temperature spike", now)
            return
        self.accepted = filtered
        self.accepted_at = now
        self.issue = None
        self.issue_since = None
        if value <= settings.water_off:
            # A valid cold-water report stops fans without median delay.
            self.accepted = value
            self.warm_since = None

    def _reject(self, reason, now):
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
        if not self.usable(now):
            self.warm_since = None
            return False
        if self.accepted <= settings.water_off:
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
