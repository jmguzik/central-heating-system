"""Central mode selector."""

from homeassistant.components.select import SelectEntity

from .const import MODES
from .entity import HeatingEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HeatingMode(entry.runtime_data)])


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

