"""Reported fan activity counters, independent of requested device commands.

Only consecutive available observations accrue runtime. Persisted runtime is
checkpointed in whole minutes to avoid a storage write on every controller tick;
an unclean restart can lose less than one minute per speed. Starts persist at
once. Neither process downtime nor a missing device report implies a start.
"""

from math import floor, isfinite


def _counter(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value if isfinite(value) and value >= 0 else 0


def runtime_checkpoint(hours):
    """Use identical minute checkpoints for persistence and recorder sensors.

    Publishing fractions that are not persisted would make TOTAL_INCREASING
    sensors decrease after a normal restore and appear to reset in statistics.
    """
    return floor(hours * 60 + 1e-9) / 60


class FanTelemetry:
    """Accumulate observed off-to-fan transitions and low/medium runtime."""

    def __init__(self):
        self._totals = {}
        self._previous = {}

    def load(self, data):
        self._totals = {}
        self._previous = {}
        if not isinstance(data, dict):
            return
        for key, saved in data.items():
            if not isinstance(saved, dict):
                continue
            self._totals[key] = {
                "fan_starts": int(_counter(saved.get("fan_starts"))),
                "low_runtime_hours": _counter(saved.get("low_runtime_hours")),
                "medium_runtime_hours": _counter(saved.get("medium_runtime_hours")),
            }

    def snapshot(self, room_key):
        return dict(self._totals.setdefault(room_key, {
            "fan_starts": 0, "low_runtime_hours": 0.0,
            "medium_runtime_hours": 0.0,
        }))

    def observe(self, room_key, state, now_monotonic):
        self.snapshot(room_key)
        mode = getattr(state, "state", None)
        if mode in (None, "unavailable", "unknown"):
            self._previous.pop(room_key, None)
            return
        attributes = getattr(state, "attributes", {}) or {}
        speed = attributes.get("fan_mode") if mode == "fan_only" else None
        previous = self._previous.get(room_key)
        totals = self._totals[room_key]
        if previous is not None:
            previous_mode, previous_speed, previous_at = previous
            elapsed = now_monotonic - previous_at
            if elapsed < 0:
                # Reset the observation baseline if the clock is replaced.
                self._previous[room_key] = (mode, speed, now_monotonic)
                return
            if previous_mode == "fan_only" and previous_speed in ("low", "medium"):
                totals[f"{previous_speed}_runtime_hours"] += elapsed / 3600
            if previous_mode == "off" and mode == "fan_only":
                totals["fan_starts"] += 1
        self._previous[room_key] = (mode, speed, now_monotonic)

    def serialize(self):
        return {
            key: {"fan_starts": totals["fan_starts"], **{
                f"{speed}_runtime_hours": runtime_checkpoint(totals[f"{speed}_runtime_hours"])
                for speed in ("low", "medium")
            }} for key, totals in self._totals.items()
        }
