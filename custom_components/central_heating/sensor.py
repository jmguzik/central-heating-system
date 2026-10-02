"""Controller status, per-room reasons, and room-temperature history."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTemperature

from .entity import HeatingEntity, add_room_entities


async def async_setup_entry(hass, entry, async_add_entities):
    controller = entry.runtime_data
    async_add_entities([HeatingStatus(controller)])
    add_room_entities(controller, entry, async_add_entities, [RoomStatus, RoomTemperature])


class HeatingStatus(HeatingEntity, SensorEntity):
    _attr_icon = "mdi:radiator"

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

    def __init__(self, controller, room):
        super().__init__(controller, "status", "Status", "sensor", room)

    @property
    def native_value(self):
        return self.room_data.get("reason", "Initializing")

    @property
    def extra_state_attributes(self):
        return self.room_data


class RoomTemperature(HeatingEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, controller, room):
        super().__init__(controller, "temperature", "Room temperature", "sensor", room)

    @property
    def native_value(self):
        return self.room_data.get("temperature")

