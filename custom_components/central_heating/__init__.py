"""Central controls for locally integrated fan-coil thermostats."""

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, OVERRIDE_DURATIONS, PLATFORMS
from .controller import HeatingController
from .frontend import async_register_card
from .history import HistoryRetention

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

    async def set_room_override(call):
        await controller.async_apply_override(call.data["entity_id"], call.data["target"], call.data["duration"])

    hass.services.async_register(DOMAIN, "set_room_override", set_room_override, schema=vol.Schema({
        vol.Required("entity_id"): cv.entity_domain("climate"),
        vol.Required("target"): vol.All(vol.Coerce(float), vol.Range(min=5, max=35)),
        vol.Required("duration", default="2 hours"): vol.In(OVERRIDE_DURATIONS),
    }))
    async def set_room_quiet_hours(call):
        await controller.async_set_quiet_hours(
            call.data["entity_id"], call.data["enabled"], call.data["start"], call.data["end"], call.data["days"]
        )

    hass.services.async_register(DOMAIN, "set_room_quiet_hours", set_room_quiet_hours, schema=vol.Schema({
        vol.Required("entity_id"): cv.entity_domain("climate"),
        vol.Required("enabled"): bool,
        vol.Required("start"): str,
        vol.Required("end"): str,
        vol.Required("days"): list,
    }))
    async def comfort_settings(call):
        await controller.comfort.configure(call.data["settings"])

    async def weekly_schedule(call):
        await controller.comfort.set_schedule(call.data["entries"])

    async def global_override(call):
        await controller.comfort.set_manual(call.data["target"], call.data["minutes"])

    hass.services.async_register(DOMAIN, "configure_comfort", comfort_settings,
                                 schema=vol.Schema({vol.Required("settings"): dict}))
    hass.services.async_register(DOMAIN, "set_schedule", weekly_schedule,
                                 schema=vol.Schema({vol.Required("entries"): list}))
    hass.services.async_register(DOMAIN, "set_global_override", global_override,
                                 schema=vol.Schema({vol.Required("target"): vol.Coerce(float), vol.Required("minutes", default=60): vol.Coerce(int)}))
    for name, action in {
        "cancel_global_override": controller.comfort.cancel_manual,
        "prepare_native_schedule": controller.comfort.prepare_schedule,
        "retry_native_schedule": controller.comfort.retry_schedule,
        "discover_comfort": controller.comfort.discover,
        "enable_correction": controller.comfort.enable,
        "relinquish": controller.comfort.relinquish,
    }.items():
        async def invoke(call, action=action):
            try:
                await action()
            except (ValueError, TimeoutError) as err:
                from homeassistant.exceptions import ServiceValidationError
                raise ServiceValidationError(str(err)) from err
        hass.services.async_register(DOMAIN, name, invoke, schema=vol.Schema({}))
    await async_register_card(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    HistoryRetention(controller).async_start()
    controller.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """An unloaded controller leaves its fan-coils off."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop(turn_off=True)
        hass.services.async_remove(DOMAIN, "set_room_override")
        hass.services.async_remove(DOMAIN, "set_room_quiet_hours")
        for name in ("configure_comfort", "set_schedule", "set_global_override", "cancel_global_override", "prepare_native_schedule", "retry_native_schedule", "discover_comfort", "enable_correction", "relinquish"):
            hass.services.async_remove(DOMAIN, name)
        return True
    return False
