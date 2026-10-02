"""Use the central target or a room's saved custom target."""

from homeassistant.components.switch import SwitchEntity

from .entity import HeatingEntity, add_room_entities


async def async_setup_entry(hass, entry, async_add_entities):
    add_room_entities(entry.runtime_data, entry, async_add_entities, [TargetOverride])


class TargetOverride(HeatingEntity, SwitchEntity):
    _attr_icon = "mdi:thermometer-auto"

    def __init__(self, controller, room):
        super().__init__(controller, "override", "Use custom target", "switch", room)

    @property
    def is_on(self):
        return self.controller.memories[self.room_key].override

    async def async_turn_on(self, **kwargs):
        await self.controller.async_set_room(self.room_key, "override", True)

    async def async_turn_off(self, **kwargs):
        await self.controller.async_set_room(self.room_key, "override", False)

