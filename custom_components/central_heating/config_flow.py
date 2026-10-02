"""Import or select the two heating-water sensors."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers import selector

from .const import DOMAIN


class CentralHeatingConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_import(self, user_input):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Central Heating", data=user_input)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            if user_input["poddasze_sensor"] == user_input["piwnica_sensor"]:
                errors = {"base": "same_sensor"}
            else:
                return await self.async_step_import({"zones": [
                    {"id": "poddasze", "name": "Poddasze / Składzik", "sensor": user_input["poddasze_sensor"], "label": "heating_poddasze", "initial_members": []},
                    {"id": "piwnica", "name": "Piwnica / Kotłownia", "sensor": user_input["piwnica_sensor"], "label": "heating_piwnica", "initial_members": []},
                ]})
        else:
            errors = {}
        sensor = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature"))
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required("poddasze_sensor"): sensor,
            vol.Required("piwnica_sensor"): sensor,
        }), errors=errors)

