"""Heating policy, independent of Home Assistant and device commands."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
import re
from math import isfinite
from typing import Any


def temperature(value: Any, unit: str = "°C") -> float | None:
    """Read a finite Celsius value; invalid values are never treated as zero."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(result):
        return None
    if unit == "°F":
        result = (result - 32) * 5 / 9
    elif unit != "°C":
        return None
    return result


@dataclass
class Settings:
    mode: str = "Off"
    water_on: float = 33.0
    water_off: float = 30.0
    target: float = 22.0
    room_maximum: float = 24.0
    room_hysteresis: float = 0.5

    def validate(self) -> None:
        """Validate values before applying or restoring them."""
        if self.mode not in ("Off", "Manual", "Adaptive"):
            raise ValueError("Mode must be Off, Manual, or Adaptive")
        bounds = {
            "water_on": (0, 100),
            "water_off": (0, 100),
            "target": (5, 35),
            "room_maximum": (5, 35),
            "room_hysteresis": (0.1, 5),
        }
        for key, (lower, upper) in bounds.items():
            value = temperature(getattr(self, key))
            if value is None or not lower <= value <= upper:
                raise ValueError(f"{key} must be between {lower} and {upper} °C")
        if self.water_on <= self.water_off:
            raise ValueError("Water ON must be greater than water OFF")

    def serialize(self) -> dict:
        return asdict(self)


@dataclass
class RoomMemory:
    blocked: bool = True
    override: bool = False
    target: float = 22.0
    adaptive_fan: str = "low"
    override_duration: str = "2 hours"
    override_expires_at: float | None = None
    room_maximum: float | None = None
    room_hysteresis: float | None = None

    quiet_enabled: bool = False
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    quiet_days: list[int] = field(default_factory=lambda: list(range(7)))

    @staticmethod
    def validate_quiet(enabled, start, end, days) -> None:
        """Validate the complete schedule before any values are applied."""
        if not isinstance(enabled, bool):
            raise ValueError("Quiet hours enabled must be a boolean")
        if any(not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) for value in (start, end)):
            raise ValueError("Quiet hours must use HH:MM times")
        if start == end:
            raise ValueError("Quiet hours start and end must differ")
        if not isinstance(days, list) or not days or any(type(day) is not int or day not in range(7) for day in days) or len(set(days)) != len(days):
            raise ValueError("Choose distinct quiet start days from Monday (0) to Sunday (6)")

    def quiet_active(self, local_now: datetime) -> bool:
        """Overnight schedules belong to their start day, including after midnight."""
        if not self.quiet_enabled:
            return False
        clock = local_now.strftime("%H:%M")
        day = local_now.weekday()
        if self.quiet_start < self.quiet_end:
            return day in self.quiet_days and self.quiet_start <= clock < self.quiet_end
        if clock >= self.quiet_start:
            return day in self.quiet_days
        return clock < self.quiet_end and (day - 1) % 7 in self.quiet_days

    @property
    def limits_override(self) -> bool:
        return self.room_maximum is not None or self.room_hysteresis is not None

    def effective_room_limits(self, settings: Settings) -> tuple[float, float]:
        """Use central limits for unset or invalid room values."""
        maximum = temperature(self.room_maximum)
        gap = temperature(self.room_hysteresis)
        return (
            maximum if maximum is not None and 5 <= maximum <= 35 else settings.room_maximum,
            gap if gap is not None and 0.1 <= gap <= 5 else settings.room_hysteresis,
        )

    def effective_target(self, settings: Settings) -> float:
        value = temperature(self.target)
        if self.override and value is not None and 5 <= value <= 35:
            return value
        return settings.target

    def serialize(self) -> dict:
        return asdict(self)

    def expire_override(self, now: float) -> bool:
        if self.override and self.override_expires_at is not None and now >= self.override_expires_at:
            self.override = False
            self.override_expires_at = None
            return True
        return False


@dataclass(frozen=True)
class Decision:
    hvac_mode: str
    fan_mode: str | None
    reason: str


def zone_permission(water: float | None, previous: bool, settings: Settings) -> bool:
    """Schmitt trigger for heating-water availability, independently per zone."""
    if water is None or not 0 <= water <= 120:
        return False
    if water >= settings.water_on:
        return True
    if water <= settings.water_off:
        return False
    return previous


def room_blocked(room: float | None, previous: bool, settings: Settings, memory: RoomMemory | None = None) -> bool:
    """Room cutoff has its own memory, separate from water availability."""
    if room is None or not -40 <= room <= 80:
        return True
    maximum, gap = memory.effective_room_limits(settings) if memory is not None else (settings.room_maximum, settings.room_hysteresis)
    if room >= maximum:
        return True
    if room <= maximum - gap:
        return False
    return previous


def decide(
    settings: Settings,
    memory: RoomMemory,
    room: float | None,
    water_allowed: bool,
    *,
    water_valid: bool = True,
    membership_valid: bool = True,
    available: bool = True,
    compatible: bool = True,
    quiet_active: bool = False,
) -> Decision:
    """Choose fan operation; overrides never bypass Off or room protection."""
    if not membership_valid:
        return Decision("off", None, "Conflicting zone labels")
    if not available:
        return Decision("off", None, "Thermostat unavailable")
    if settings.mode == "Off":
        return Decision("off", None, "System off")
    if room is None or not -40 <= room <= 80:
        return Decision("off", None, "Room temperature unavailable")
    if memory.blocked:
        return Decision("off", None, "Room limit reached")
    if not compatible:
        return Decision("off", None, "Unsupported thermostat fan modes")
    if settings.mode == "Manual":
        return Decision("fan_only", "low", "Manual · low")
    if not water_valid:
        return Decision("off", None, "Water temperature unavailable")
    if not water_allowed:
        return Decision("off", None, "Water too cold")
    target = memory.effective_target(settings)
    if room <= target - 1.5:
        memory.adaptive_fan = "medium"
    elif room >= target - 0.5:
        memory.adaptive_fan = "low"
    if quiet_active:
        return Decision("fan_only", "low", "Quiet hours · low")
    if memory.adaptive_fan == "medium":
        return Decision("fan_only", "medium", "Cold room · medium")
    return Decision("fan_only", "low", "Near target · low")
