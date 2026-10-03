"""Load the card as a versioned Lovelace module, including on mobile cold starts."""

from pathlib import Path
from urllib.parse import urlsplit

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA

from .const import CARD_URL, DOMAIN

CARD_PATH = "/central_heating/central-heating-card.js"


def is_our_resource(item):
    """Match our path only; preserve unrelated and external resources."""
    parsed = urlsplit(item.get("url", ""))
    return not parsed.scheme and not parsed.netloc and parsed.path == CARD_PATH


async def async_register_card(hass):
    """Register once, replacing old versions and duplicate entries on upgrade."""
    registered_key = f"{DOMAIN}_frontend_registered"
    if not hass.data.get(registered_key):
        await hass.http.async_register_static_paths([
            StaticPathConfig(CARD_PATH, str(Path(__file__).parent / "www" / "central-heating-card.js"), False)
        ])
        hass.data[registered_key] = True

    resources = hass.data[LOVELACE_DATA].resources
    await resources.async_get_info()  # Storage resources load lazily in HA.
    existing = [item for item in resources.async_items() if is_our_resource(item)]
    if hasattr(resources, "async_create_item"):
        if existing:
            await resources.async_update_item(existing[0]["id"], {"url": CARD_URL, "res_type": "module"})
            for duplicate in existing[1:]:
                await resources.async_delete_item(duplicate["id"])
        else:
            await resources.async_create_item({"url": CARD_URL, "res_type": "module"})
    else:
        # YAML resources are read-only to the UI. Add our resource to the loaded
        # collection without changing the user's YAML or switching resource mode.
        resources.data[:] = [item for item in resources.async_items() if not is_our_resource(item)]
        resources.data.append({"url": CARD_URL, "type": "module"})
