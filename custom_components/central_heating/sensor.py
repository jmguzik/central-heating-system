"""Controller status, per-room reasons, and temperature history."""

from time import monotonic, time
from math import isfinite

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import callback

from .entity import HeatingEntity, add_room_entities
from .telemetry import runtime_checkpoint


async def async_setup_entry(hass, entry, async_add_entities):
    controller = entry.runtime_data
    async_add_entities([HeatingStatus(controller)])
    async_add_entities([
        HeatingMetric(controller, key, name) for key, name in (
            ("weighted_temperature", "Weighted room temperature"),
            ("home_target", "Home target temperature"),
            ("native_target", "miGo target temperature"),
            ("native_correction", "miGo temperature correction"),
            ("running_fans", "Running fans"),
            ("average_temperature", "Average managed room temperature"),
        )
    ])
    async_add_entities([HomeFanMetric(controller, key, name) for key, name in (
        ("fan_starts", "Whole home fan starts"),
        ("low_runtime", "Whole home low speed runtime"),
        ("medium_runtime", "Whole home medium speed runtime"),
    )])
    async_add_entities([WaterTemperature(controller, key, zone) for key, zone in controller.zones.items()])
    add_room_entities(controller, entry, async_add_entities, [
        RoomStatus, RoomTemperature, RoomTargetTemperature, RoomFanSpeed,
        RoomFanStarts, RoomLowRuntime, RoomMediumRuntime,
    ])


def _finite(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _temperature(value):
    value = _finite(value)
    return round(value, 1) if value is not None else None


class _HistorySample:
    """Limit measured history to minute samples without delaying sensor faults.

    This cache belongs to the history entity only. Control and the live panel
    continue to read the controller's full precision snapshot.
    """

    def __init__(self):
        self.value = None
        self.sampled_at = None

    def read(self, value, now=None):
        value = _temperature(value)
        if value is None:
            self.value = None
            self.sampled_at = None
            return None
        now = monotonic() if now is None else now
        if self.sampled_at is None or now - self.sampled_at >= 60:
            self.value = value
            self.sampled_at = now
        return self.value


class HistorySensor(HeatingEntity, SensorEntity):
    """Keep a minute baseline without creating indefinite HA statistics."""

    _history_minute = None
    _force_history_update = False

    @property
    def force_update(self):
        # Entity's default force_update is cached. This flag must be read anew
        # for every publication so normal five-second ticks stay unforced.
        return self._force_history_update

    @callback
    def _changed(self):
        minute = int(time() // 60)
        self._force_history_update = minute != self._history_minute
        self._history_minute = minute
        # A forced minute publication refreshes last_changed, so native history
        # graphs retain steady values after old significant rows are purged.
        super()._changed()


class HeatingMetric(HistorySensor):
    """Small recorder-friendly measurements for native HA history graphs."""

    _attr_suggested_display_precision = 1

    def __init__(self, controller, key, name):
        super().__init__(controller, key, name, "sensor")
        self._history_sample = _HistorySample()
        if key != "running_fans":
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            if key != "native_correction":
                self._attr_device_class = SensorDeviceClass.TEMPERATURE
        else:
            self._attr_icon = "mdi:fan"
            self._attr_native_unit_of_measurement = "fans"
            self._attr_suggested_display_precision = 0

    @property
    def native_value(self):
        comfort = self.controller.snapshot.get("comfort", {})
        if self.key == "running_fans":
            return _finite(self.controller.snapshot.get("running_fans"))
        if self.key == "average_temperature":
            values = [_finite(room.get("temperature")) for room in self.controller.snapshot.get("rooms", [])]
            values = [value for value in values if value is not None]
            return self._history_sample.read(sum(values) / len(values) if values else None)
        if self.key == "home_target":
            value = comfort.get("effective_target")
            return _temperature(self.controller.settings.target if value is None else value)
        if self.key in ("native_target", "native_correction") and comfort.get("connection_retrying"):
            return self._history_sample.read(None)
        if self.key == "native_correction":
            # The comfort adapter only exposes a reference after validating its
            # physical freshness; never derive a correction from a saved command.
            target = _finite(comfort.get("native_target"))
            reference = _finite(comfort.get("reference_temperature"))
            return self._history_sample.read(target - reference if target is not None and reference is not None else None)
        if self.key == "native_target":
            return _temperature(comfort.get(self.key))
        return self._history_sample.read(comfort.get(self.key))


class HomeFanMetric(HistorySensor):
    """Sum currently managed rooms; retired rooms do not inflate home totals."""

    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "starts"

    def __init__(self, controller, key, name):
        super().__init__(controller, key, name, "sensor")
        if key.endswith("runtime"):
            self._attr_native_unit_of_measurement = UnitOfTime.HOURS
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_suggested_display_precision = 2
            self._attr_icon = "mdi:timer-outline"

    @property
    def native_value(self):
        runtime = self.key.endswith("runtime")
        key = f"{self.key}_hours" if runtime else self.key
        values = [_finite(room.get(key)) for room in self.controller.snapshot.get("rooms", [])]
        if any(value is None for value in values):
            return None
        return sum(runtime_checkpoint(value) if runtime else value for value in values)


class HeatingStatus(HeatingEntity, SensorEntity):
    _attr_icon = "mdi:radiator"
    _unrecorded_attributes = frozenset({"*"})

    def __init__(self, controller):
        super().__init__(controller, "status", "Status", "sensor")

    @property
    def native_value(self):
        return self.controller.settings.mode

    @property
    def extra_state_attributes(self):
        return self.controller.snapshot


class RoomStatus(HeatingEntity, SensorEntity):
    _attr_icon = "mdi:fan"
    _unrecorded_attributes = frozenset({"*"})

    def __init__(self, controller, room):
        super().__init__(controller, "status", "Status", "sensor", room)

    @property
    def native_value(self):
        return self.room_data.get("reason", "Initializing")

    @property
    def extra_state_attributes(self):
        return self.room_data


class RoomTemperature(HistorySensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, controller, room):
        super().__init__(controller, "temperature", "Room temperature", "sensor", room)
        self._history_sample = _HistorySample()

    @property
    def native_value(self):
        return self._history_sample.read(self.room_data.get("temperature"))


class RoomTargetTemperature(RoomTemperature):
    def __init__(self, controller, room):
        HeatingEntity.__init__(self, controller, "target_temperature", "Effective target temperature", "sensor", room)

    @property
    def native_value(self):
        return _temperature(self.room_data.get("effective_target"))


class RoomFanSpeed(HistorySensor):
    _attr_icon = "mdi:fan"

    def __init__(self, controller, room):
        super().__init__(controller, "fan_speed", "Reported fan speed", "sensor", room)

    @property
    def native_value(self):
        data = self.room_data
        if data.get("reported_mode") == "off":
            return "off"
        speed = data.get("reported_fan")
        if data.get("reported_mode") == "fan_only" and speed in ("low", "medium", "high"):
            return speed
        return None


class RoomFanStarts(HistorySensor):
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "starts"
    _attr_suggested_display_precision = 0
    metric = "fan_starts"
    label = "Fan starts"

    def __init__(self, controller, room):
        super().__init__(controller, self.metric, self.label, "sensor", room)

    @property
    def native_value(self):
        return _finite(self.room_data.get(self.metric))


class RoomLowRuntime(RoomFanStarts):
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_suggested_display_precision = 2
    metric = "low_runtime"
    label = "Low speed runtime"

    @property
    def native_value(self):
        value = _finite(self.room_data.get(f"{self.metric}_hours"))
        return runtime_checkpoint(value) if value is not None else None


class RoomMediumRuntime(RoomLowRuntime):
    metric = "medium_runtime"
    label = "Medium speed runtime"


class WaterTemperature(HistorySensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, controller, key, zone):
        super().__init__(controller, f"{key}_water_temperature", f"{zone['name']} smoothed water temperature", "sensor")
        self.zone_key = key
        self._history_sample = _HistorySample()
        self._attr_translation_placeholders = {"zone": zone["name"]}

    @property
    def native_value(self):
        water = self.controller.water_filters[self.zone_key]
        now = monotonic()
        return self._history_sample.read(water.accepted if water.usable(now) else None, now)
