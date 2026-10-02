"""Central thresholds and optional per-room targets."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature

from .entity import HeatingEntity, add_room_entities

CENTRAL_NUMBERS = {
    "target": ("Central target", 5, 35, 0.5),
    "water_on": ("Water ON temperature", 0, 100, 0.5),
    "water_off": ("Water OFF temperature", 0, 100, 0.5),
    "room_maximum": ("Room maximum", 5, 35, 0.5),
    "room_hysteresis": ("Room restart gap", 0.1, 5, 0.1),
}


async def async_setup_entry(hass, entry, async_add_entities):
    controller = entry.runtime_data
    async_add_entities([HeatingNumber(controller, key, *values) for key, values in CENTRAL_NUMBERS.items()])
    add_room_entities(controller, entry, async_add_entities, [RoomTarget])


class HeatingNumber(HeatingEntity, NumberEntity):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer"

    def __init__(self, controller, key, name, minimum, maximum, step, room=None):
        super().__init__(controller, key, name, "number", room)
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step

    @property
    def native_value(self):
        return getattr(self.controller.settings, self.key)

    async def async_set_native_value(self, value):
        await self.controller.async_set_setting(self.key, value)


class RoomTarget(HeatingNumber):
    def __init__(self, controller, room):
        super().__init__(controller, "target", "Custom target", 5, 35, 0.5, room)

    @property
    def native_value(self):
        return self.controller.memories[self.room_key].target

    async def async_set_native_value(self, value):
        await self.controller.async_set_room(self.room_key, "target", value)

