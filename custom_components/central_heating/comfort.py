"""Whole-home comfort policy. Never switches a burner or changes fan gates."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import exp, floor

from .logic import temperature


@dataclass
class ComfortSettings:
    room_sensors: dict = field(default_factory=dict)
    migo_entity: str = ""
    home_id: str = ""
    room_id: str = ""
    schedule_id: str = ""
    minimum: float = 18.0
    maximum: float = 24.0
    manual_minutes: int = 60
    poll_seconds: int = 60
    correction_seconds: int = 600
    freshness_seconds: int = 900
    coverage: float = 1.0
    smoothing_seconds: float = 60.0
    hysteresis: float = 0.05

    def validate(self, water_entities=()):
        bounds = {
            "minimum": (7, 30), "maximum": (7, 30), "manual_minutes": (5, 720),
            "poll_seconds": (60, 3600), "correction_seconds": (60, 7200),
            "freshness_seconds": (60, 86400), "coverage": (0.1, 1),
            "smoothing_seconds": (0, 1800), "hysteresis": (0, 0.2),
        }
        for key, (low, high) in bounds.items():
            value = temperature(getattr(self, key))
            if not isinstance(getattr(self, key), (int, float)) or isinstance(getattr(self, key), bool) or value is None or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        if self.minimum > self.maximum or self.minimum * 2 % 1 or self.maximum * 2 % 1:
            raise ValueError("Output bounds must be ordered 0.5°C steps")
        if self.poll_seconds * 3 > self.manual_minutes * 60:
            raise ValueError("Reference polling must be at most one third of the shared native duration")
        for key in ("manual_minutes", "poll_seconds", "correction_seconds", "freshness_seconds"):
            if int(getattr(self, key)) != getattr(self, key):
                raise ValueError(f"{key} must be an integer")
        if not isinstance(self.room_sensors, dict) or len(self.room_sensors) > 6:
            raise ValueError("Choose up to six room ambient sensors")
        for entity, weight in self.room_sensors.items():
            value = temperature(weight)
            if not isinstance(entity, str) or not entity.startswith(("sensor.", "climate.")):
                raise ValueError("Select a room ambient sensor or climate entity")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool) or value is None or not 0 <= value <= 100:
                raise ValueError("Room weights must be finite, between 0 and 100")
            if entity in water_entities or entity == self.migo_entity:
                raise ValueError("Water and miGo reference cannot contribute to the average")
        for key in ("migo_entity", "home_id", "room_id", "schedule_id"):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"{key} must be text")
        if self.migo_entity and not self.migo_entity.startswith("climate."):
            raise ValueError("miGo reference must be its climate entity")

    def serialize(self):
        return asdict(self)


def validate_schedule(entries):
    """Canonical weekly transitions, Monday 00:00 mandatory, no implicit gaps."""
    if not isinstance(entries, list) or not 1 <= len(entries) <= 336:
        raise ValueError("Schedule needs 1–336 transitions")
    result = []
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {"day", "time", "target"}:
            raise ValueError("Each transition needs day, time and target")
        day = raw["day"]
        clock = raw["time"]
        target = temperature(raw["target"])
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ValueError("Day must be 0 (Monday) to 6 (Sunday)")
        if not isinstance(clock, str) or len(clock) != 5 or clock[2] != ":":
            raise ValueError("Time must use HH:MM")
        try:
            hour, minute = map(int, clock.split(":"))
        except ValueError as err:
            raise ValueError("Time must use HH:MM") from err
        if not 0 <= hour <= 23 or not 0 <= minute <= 59 or clock != f"{hour:02}:{minute:02}":
            raise ValueError("Invalid schedule time")
        if target is None or not 7 <= target <= 30 or target * 2 % 1:
            raise ValueError("Schedule temperatures must be 7–30°C in 0.5°C steps")
        result.append({"day": day, "time": clock, "target": target})
    result.sort(key=lambda row: (row["day"], row["time"]))
    if result[0]["day"] != 0 or result[0]["time"] != "00:00":
        raise ValueError("Include Monday 00:00 as the weekly baseline")
    if len({(row["day"], row["time"]) for row in result}) != len(result):
        raise ValueError("Duplicate schedule transition")
    return result


def schedule_target(entries, local_now: datetime):
    offset = local_now.weekday() * 1440 + local_now.hour * 60 + local_now.minute
    selected = entries[-1]
    for row in entries:
        hour, minute = map(int, row["time"].split(":"))
        if row["day"] * 1440 + hour * 60 + minute > offset:
            break
        selected = row
    return selected["target"]


def ambient_value(state, now, freshness, default_unit="°C"):
    """Use last_reported, not last_changed: steady real reports stay fresh."""
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    stamp = getattr(state, "last_reported", None)
    if stamp is None or not 0 <= now - stamp.timestamp() <= freshness:
        return None
    attrs = state.attributes
    climate = "current_temperature" in attrs
    value = temperature(attrs.get("current_temperature") if climate else state.state,
                        attrs.get("temperature_unit", default_unit) if climate else attrs.get("unit_of_measurement", ""))
    return value if value is not None and -20 <= value <= 50 else None


def weighted_average(settings, states, now, unit="°C"):
    total = sum(float(weight) for weight in settings.room_sensors.values())
    valid_weight = 0.0
    weighted = 0.0
    diagnostics = []
    for entity, weight in settings.room_sensors.items():
        value = ambient_value(states.get(entity), now, settings.freshness_seconds, unit)
        diagnostics.append({"entity_id": entity, "weight": weight, "temperature": value})
        if weight > 0 and value is not None:
            weighted += value * weight
            valid_weight += weight
    coverage = valid_weight / total if total else 0
    value = weighted / valid_weight if valid_weight and coverage >= settings.coverage else None
    return value, coverage, diagnostics


@dataclass
class ComfortMemory:
    owner: str = "miGo"
    manual_target: float | None = None
    manual_expires_at: float | None = None
    last_command_at: float | None = None
    last_command: float | None = None
    native_expires_at: float | None = None
    schedule_version: int = 0
    confirmed_version: int = 0
    confirmed_schedule: list = field(default_factory=list)
    native_schedule: dict = field(default_factory=dict)
    managed_schedule_id: str = ""
    managed_name: str = ""
    duration_confirmed: bool = False
    pending_sync: dict = field(default_factory=dict)
    native_timezone: str = ""
    pending_create: dict = field(default_factory=dict)
    managed_dhw_id: str = ""
    native_dhw_schedule: dict = field(default_factory=dict)
    pending_dhw_create: dict = field(default_factory=dict)
    pending_command: bool = False

    def serialize(self):
        return asdict(self)

    def target(self, schedule, now, local_now):
        if self.manual_expires_at is not None and now >= self.manual_expires_at:
            self.manual_target = self.manual_expires_at = None
        if self.manual_target is not None and self.manual_expires_at is not None:
            return self.manual_target, "Manual"
        return schedule_target(schedule, local_now), "Schedule"


class Correction:
    """Elapsed-time smoothing; one 0.5°C actuator step per configured interval."""
    def __init__(self):
        self.filtered = None
        self.updated_at = None
        self.hold_since = None

    def reset(self):
        self.__init__()

    def evaluate(self, settings, memory, weighted, reference, target, now, actual):
        raw = reference + target - weighted
        if self.filtered is None:
            self.filtered = raw
        else:
            elapsed = max(0, now - self.updated_at)
            alpha = 1 - exp(-elapsed / settings.smoothing_seconds) if settings.smoothing_seconds else 1
            self.filtered += alpha * (raw - self.filtered)
        self.updated_at = now
        bounded = min(settings.maximum, max(settings.minimum, self.filtered))
        desired = floor(bounded * 2 + 0.5) / 2
        # Slew from fresh native reality (expiry can change it), not a saved command.
        previous = actual
        if previous is not None and desired != previous and abs(bounded - previous) < 0.25 + settings.hysteresis:
            self.hold_since = self.hold_since if self.hold_since is not None else now
            # A steady bias must eventually move, especially downward at target.
            if now - self.hold_since < settings.correction_seconds * 2:
                desired = previous
        else:
            self.hold_since = None
        due = memory.last_command_at is None or now - memory.last_command_at >= settings.correction_seconds
        if previous is not None:
            desired = min(previous + 0.5, max(previous - 0.5, desired))
        # A command outside the configured bounds is rejected by the adapter;
        # changing bounds around an active output explicitly releases native control.
        return {"raw_output": raw, "smoothed_output": self.filtered, "requested_output": desired,
                "saturated": raw < settings.minimum or raw > settings.maximum,
                "due": due, "next_correction_at": (memory.last_command_at + settings.correction_seconds) if memory.last_command_at is not None else now}
