"""Integration constants."""

DOMAIN = "central_heating"
VERSION = "0.1.0"
PLATFORMS = ["select", "number", "switch", "sensor"]
MODES = ["Off", "Manual", "Adaptive"]
CARD_URL = f"/central_heating/central-heating-card.js?v={VERSION}"
RECONCILE_SECONDS = 30

