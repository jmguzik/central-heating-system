"""Integration constants."""

DOMAIN = "central_heating"
VERSION = "0.2.0"
PLATFORMS = ["select", "number", "switch", "sensor"]
MODES = ["Off", "Manual", "Adaptive"]
CARD_URL = f"/central_heating/central-heating-card.js?v={VERSION}"
RECONCILE_SECONDS = 5
OVERRIDE_DURATIONS = {"30 minutes": 1800, "1 hour": 3600, "2 hours": 7200, "Until cancelled": None}
