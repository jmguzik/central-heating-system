"""Shared native-entity lifecycle and dynamic room discovery."""

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, VERSION


class HeatingEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, controller, key, name, platform, room=None):
        self.controller = controller
        self.key = key
        self.room_key = room.key if room else None
        suffix = room.entity_id.split(".", 1)[1] if room else ""
        object_id = f"central_heating_{suffix + '_' if suffix else ''}{key}"
        self.entity_id = f"{platform}.{object_id}"
        self._attr_translation_key = (
            "water_temperature" if key.endswith("_water_temperature") else
            "room_target" if room and key == "target" else
            "room_status" if room and key == "status" else key
        )
        self._attr_unique_id = f"{controller.entry.entry_id}_{self.room_key or 'central'}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.room_key or "central")},
            "name": f"Heating · {room.name}" if room else "Central Heating",
            "manufacturer": "Central Heating System",
            "model": "Room controller" if room else "Fan-coil controller",
            "sw_version": VERSION,
        }

    @property
    def available(self):
        return self.room_key is None or self.room_key in self.controller.rooms

    @property
    def room_data(self):
        return next((room for room in self.controller.snapshot.get("rooms", []) if room["key"] == self.room_key), {})

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.controller.async_add_listener(self._changed))
        self.controller.async_register_control(self.key, self.entity_id, self.room_key)

    @callback
    def _changed(self):
        self.async_write_ha_state()


def add_room_entities(controller, entry, async_add_entities, factories):
    """Add controls once per registry ID, including rooms discovered at runtime."""
    known = set()

    @callback
    def added(rooms):
        entities = []
        for room in rooms:
            if room.key in known:
                continue
            known.add(room.key)
            entities.extend(factory(controller, room) for factory in factories)
        if entities:
            async_add_entities(entities)

    added(list(controller.rooms.values()))
    entry.async_on_unload(controller.async_add_room_listener(added))
