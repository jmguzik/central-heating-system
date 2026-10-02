"""Central controls for locally integrated fan-coil thermostats."""

from pathlib import Path

import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import CARD_URL, DOMAIN, PLATFORMS
from .controller import HeatingController

ZONE_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.Match(r"^[a-z][a-z0-9_]*$"),
        vol.Required("name"): cv.string,
        vol.Required("sensor"): cv.entity_domain("sensor"),
        vol.Required("label"): vol.Match(r"^[a-z][a-z0-9_]*$"),
        vol.Optional("initial_members", default=[]): vol.All(
            cv.ensure_list, [cv.entity_domain("climate")]
        ),
    }
)


def validate_zones(config):
    """Reject ambiguous sensor and label mappings before controlling devices."""
    zones = config["zones"]
    for key in ("id", "sensor", "label"):
        if len({zone[key] for zone in zones}) != len(zones):
            raise vol.Invalid(f"Each zone must have a distinct {key}")
    return config


HEATING_SCHEMA = vol.All(
    vol.Schema({vol.Required("zones"): vol.All([ZONE_SCHEMA], vol.Length(min=1))}),
    validate_zones,
)
CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): HEATING_SCHEMA}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Import the site's YAML once; existing entries receive YAML updates."""
    if DOMAIN in config:
        data = config[DOMAIN]
        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            if entries[0].data != data:
                hass.config_entries.async_update_entry(entries[0], data=data)
        else:
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT}, data=data
                )
            )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create native controls and start event-driven reconciliation."""
    data = HEATING_SCHEMA(dict(entry.data))
    controller = HeatingController(hass, entry, data["zones"])
    await controller.async_load()
    entry.runtime_data = controller
    if not hass.data.get(f"{DOMAIN}_frontend_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(
                "/central_heating/central-heating-card.js",
                str(Path(__file__).parent / "www" / "central-heating-card.js"),
                False,
            )]
        )
        frontend.add_extra_js_url(hass, CARD_URL)
        hass.data[f"{DOMAIN}_frontend_registered"] = True
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    controller.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """An unloaded controller leaves its fan-coils off."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop(turn_off=True)
        return True
    return False

