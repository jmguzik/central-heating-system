"""Check resource migration and preservation with HA's collection interface."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[1] / "custom_components/central_heating"


class Resources:
    def __init__(self, items):
        self.data = deepcopy(items)

    async def async_get_info(self):
        return {"resources": len(self.data)}

    def async_items(self):
        return self.data


class StorageResources(Resources):
    async def async_create_item(self, item):
        self.data.append({"id": "new", "url": item["url"], "type": item["res_type"]})

    async def async_update_item(self, key, updates):
        item = next(item for item in self.data if item["id"] == key)
        item.update(url=updates["url"], type=updates["res_type"])

    async def async_delete_item(self, key):
        self.data[:] = [item for item in self.data if item["id"] != key]


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        package = ModuleType("frontend_under_test")
        package.__path__ = [str(ROOT)]
        http = ModuleType("homeassistant.components.http")
        http.StaticPathConfig = lambda *args: args
        lovelace_const = ModuleType("homeassistant.components.lovelace.const")
        lovelace_const.LOVELACE_DATA = "lovelace"
        self.modules = patch.dict(sys.modules, {
            "frontend_under_test": package, "homeassistant.components.http": http,
            "homeassistant.components.lovelace.const": lovelace_const,
        })
        self.modules.start()
        spec = importlib.util.spec_from_file_location("frontend_under_test.frontend", ROOT / "frontend.py")
        self.frontend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.frontend)
        self.addCleanup(self.modules.stop)

    async def register(self, resources):
        hass = SimpleNamespace(data={"lovelace": SimpleNamespace(resources=resources)},
                               http=SimpleNamespace(async_register_static_paths=AsyncMock()))
        await self.frontend.async_register_card(hass)
        await self.frontend.async_register_card(hass)
        hass.http.async_register_static_paths.assert_awaited_once()
        own = [item for item in resources.data if self.frontend.is_our_resource(item)]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["url"], self.frontend.CARD_URL)
        self.assertEqual(own[0]["type"], "module")

    async def test_first_install_registers_a_module(self):
        await self.register(StorageResources([]))

    async def test_upgrade_removes_duplicates_and_preserves_other_resources(self):
        other = {"id": "other", "url": "/hacsfiles/another-card.js", "type": "module"}
        resources = StorageResources([
            other, {"id": "old", "url": self.frontend.CARD_PATH + "?v=0.1.0", "type": "js"},
            {"id": "duplicate", "url": self.frontend.CARD_PATH + "?v=0.2.0", "type": "module"},
        ])
        await self.register(resources)
        self.assertIn(other, resources.data)

    async def test_yaml_collection_is_preserved_without_changing_mode(self):
        other = {"url": "/local/other.js", "type": "module"}
        resources = Resources([other, {"url": self.frontend.CARD_PATH, "type": "js"}])
        await self.register(resources)
        self.assertIn(other, resources.data)

    def test_external_urls_are_not_owned(self):
        self.assertFalse(self.frontend.is_our_resource({"url": "https://example.org" + self.frontend.CARD_PATH}))
