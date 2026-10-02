"""Heating policy, independent of Home Assistant and device commands."""

from dataclasses import asdict, dataclass
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

    def effective_target(self, settings: Settings) -> float:
        value = temperature(self.target)
        if self.override and value is not None and 5 <= value <= 35:
            return value
        return settings.target

    def serialize(self) -> dict:
        return asdict(self)


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


def room_blocked(room: float | None, previous: bool, settings: Settings) -> bool:
    """Room cutoff has its own memory, separate from water availability."""
    if room is None or not -40 <= room <= 80:
        return True
    if room >= settings.room_maximum:
        return True
    if room <= settings.room_maximum - settings.room_hysteresis:
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
    if room < memory.effective_target(settings) - 1:
        return Decision("fan_only", "medium", "Cold room · medium")
    return Decision("fan_only", "low", "Near target · low")

