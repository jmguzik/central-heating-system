"""Choose central or custom room targets and fan limits independently."""

from homeassistant.components.switch import SwitchEntity

from .entity import HeatingEntity, add_room_entities


async def async_setup_entry(hass, entry, async_add_entities):
    add_room_entities(entry.runtime_data, entry, async_add_entities, [TargetOverride, LimitsOverride, QuietHours])


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


class LimitsOverride(HeatingEntity, SwitchEntity):
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, controller, room):
        super().__init__(controller, "limits_override", "Use custom fan limits", "switch", room)

    @property
    def is_on(self):
        return self.controller.memories[self.room_key].limits_override

    async def async_turn_on(self, **kwargs):
        await self.controller.async_set_room(self.room_key, "limits_override", True)

    async def async_turn_off(self, **kwargs):
        await self.controller.async_set_room(self.room_key, "limits_override", False)


class QuietHours(HeatingEntity, SwitchEntity):
    _attr_icon = "mdi:weather-night"

    def __init__(self, controller, room):
        super().__init__(controller, "quiet_enabled", "Quiet hours", "switch", room)

    @property
    def is_on(self):
        return self.controller.memories[self.room_key].quiet_enabled

    async def async_turn_on(self, **kwargs):
        await self.controller.async_set_room(self.room_key, "quiet_enabled", True)

    async def async_turn_off(self, **kwargs):
        await self.controller.async_set_room(self.room_key, "quiet_enabled", False)
