"""Central mode selector."""

from homeassistant.components.select import SelectEntity

from .const import MODES, OVERRIDE_DURATIONS
from .entity import HeatingEntity, add_room_entities


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HeatingMode(entry.runtime_data)])
    add_room_entities(entry.runtime_data, entry, async_add_entities, [OverrideDuration])


class HeatingMode(HeatingEntity, SelectEntity):
    _attr_options = MODES
    _attr_icon = "mdi:radiator"

    def __init__(self, controller):
        super().__init__(controller, "mode", "Mode", "select")

    @property
    def current_option(self):
        return self.controller.settings.mode

    async def async_select_option(self, option):
        await self.controller.async_set_setting("mode", option)


class OverrideDuration(HeatingEntity, SelectEntity):
    _attr_options = list(OVERRIDE_DURATIONS)
    _attr_icon = "mdi:timer-outline"

    def __init__(self, controller, room):
        super().__init__(controller, "override_duration", "Override duration", "select", room)

    @property
    def current_option(self):
        return self.controller.memories[self.room_key].override_duration

    async def async_select_option(self, option):
        await self.controller.async_set_room(self.room_key, "override_duration", option)
