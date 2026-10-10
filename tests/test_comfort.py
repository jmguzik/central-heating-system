"""Comfort policy and mocked native transport. No live actuator writes."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_controller import Hass, controller_module
from adapter_under_test.comfort import ComfortMemory, ComfortSettings, Correction, ambient_value, schedule_target, validate_schedule, weighted_average
from adapter_under_test.migo import MigoBridge
from adapter_under_test.schedule import build_native, canonical, checked, native_entries
import adapter_under_test.migo as migo


def native_schedule(id="original", name="User schedule", selected=True):
    return {"id": id, "name": name, "type": "therm", "selected": selected, "default": True,
            "hg_temp": 7, "away_temp": 16,
            "zones": [{"id": id, "type": kind, "name": label, "rooms": [{"id": "room", "therm_setpoint_temperature": target}]}
                      for id, kind, label, target in ((0, 0, "Comfort", 22), (1, 1, "Night", 20), (4, 5, "Eco", 18))],
            "timetable": [{"zone_id": 1, "m_offset": 0}, {"zone_id": 0, "m_offset": 420}]}


class Api:
    def __init__(self, clock):
        self.clock = clock
        self.home = {"id": "home", "timezone": "Europe/Warsaw", "therm_mode": "schedule", "temperature_control_mode": "heating", "therm_setpoint_default_duration": 720,
                     "rooms": [{"id": "room"}], "schedules": [native_schedule(), {"id": "dhw", "name": "DHW", "type": "event", "zones": [{"id": 0, "modules": [{"id": "boiler", "dhw_enabled": True}]}], "timetable": [{"zone_id": 0, "m_offset": 0}]}]}
        self.room = {"id": "room", "reachable": True, "therm_measured_temperature": 21, "therm_setpoint_temperature": 20, "therm_setpoint_mode": "home"}
        self.calls = []
        self.fail_read = False
        self.fail_sync_read = False
        self.renew = True
        self.fail_release = False
        self.wait = None
        self.http_error = False
        self.mutate_selection = False
        self.wrong_expiry = False
        self.modules = []
        self.status_delay = 0
        self.advance_clock = None
        self.command_times = []

    async def get_homes_data(self):
        if self.fail_read:
            raise ValueError("Offline")
        return {"status": "ok", "body": {"homes": [deepcopy(self.home)]}}

    async def get_home_status(self, home):
        if self.advance_clock:
            self.advance_clock(self.status_delay)
        if self.wait:
            await self.wait.wait()
        if self.fail_read:
            raise ValueError("Offline")
        return {"status": "ok", "body": {"home": {"id": "home", "rooms": [deepcopy(self.room)], "modules": deepcopy(self.modules)}}}

    async def set_manual_setpoint_duration(self, home, minutes):
        self.calls.append(("duration", home, minutes))
        self.home["therm_setpoint_default_duration"] = minutes
        return {"status": "ok"}

    async def set_room_state(self, home, room, **kwargs):
        self.calls.append(("room", home, room, deepcopy(kwargs)))
        if kwargs["mode"] == "home":
            if self.fail_release:
                raise ValueError("Cancel failed")
            self.room["therm_setpoint_mode"] = "home"
            self.room.pop("therm_setpoint_end_time", None)
        else:
            self.command_times.append(self.clock())
            previous = self.room.get("therm_setpoint_temperature")
            self.room.update(therm_setpoint_mode="manual", therm_setpoint_temperature=kwargs["temp"])
            if self.renew or previous != kwargs["temp"]:
                self.room["therm_setpoint_end_time"] = self.clock() + self.home["therm_setpoint_default_duration"] * 60
                if self.wrong_expiry:
                    self.room["therm_setpoint_end_time"] += 3600
        return {"status": "ok"}

    async def switch_home_schedule(self, home, id):
        self.calls.append(("switch", id))
        for item in self.home["schedules"]:
            if item["type"] == "therm":
                item["selected"] = item["id"] == id
        return {"status": "ok"}

    async def _api_request(self, url):
        if urlsplit(url).scheme != "https" or urlsplit(url).netloc != "api.netatmo.com":
            raise AssertionError("Schedule calls must use the documented public HTTPS server")
        params = parse_qs(urlsplit(url).query)
        values = {key: json.loads(value[0]) if key in ("zones", "timetable") else value[0] for key, value in params.items()}
        self.calls.append((urlsplit(url).path, deepcopy(values)))
        if self.http_error:
            return {"status": "error", "error": {"code": 21}}
        if urlsplit(url).path.endswith("createnewhomeschedule"):
            self.home["schedules"].append({"id": "managed", "type": "therm", "selected": False, **values})
        else:
            item = next(item for item in self.home["schedules"] if item["id"] == values["schedule_id"])
            item.update(values)
            if self.mutate_selection:
                item["selected"] = True
                self.home["schedules"][0]["selected"] = False
        for item in self.home["schedules"]:
            if item["type"] == "therm":
                item["hg_temp"] = float(item["hg_temp"])
                item["away_temp"] = float(item["away_temp"])
        if self.fail_sync_read:
            self.fail_read = True
        return {"status": "ok"}


class PolicyTests(unittest.TestCase):
    def test_settings_validation(self):
        for data in ({"minimum": 31}, {"manual_minutes": 4}, {"minimum": "18"}, {"room_sensors": {"sensor.a": float("nan")}}, {"room_sensors": {"sensor.a": "1"}}, {"coverage": 0}, {"correction_seconds": 599}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ComfortSettings(**data).validate()
        ComfortSettings(manual_minutes=5).validate()
        for entity in ("sensor.water", "climate.migo"):
            with self.assertRaises(ValueError):
                ComfortSettings(migo_entity="climate.migo", room_sensors={entity: 1}).validate({"sensor.water"})

    def test_average_units_weight_coverage_and_freshness(self):
        states = {"sensor.a": SimpleNamespace(state="20", last_reported=datetime.fromtimestamp(1000, UTC), attributes={"unit_of_measurement": "°C"}),
                  "sensor.b": SimpleNamespace(state="75.2", last_reported=datetime.fromtimestamp(1000, UTC), attributes={"unit_of_measurement": "°F"})}
        settings = ComfortSettings(room_sensors={"sensor.a": 1, "sensor.b": 3})
        value, coverage, _ = weighted_average(settings, states, 1001)
        self.assertAlmostEqual(value, 23)
        self.assertEqual(coverage, 1)
        self.assertIsNone(weighted_average(settings, states, 2000)[0])
        states["sensor.b"].state = "unavailable"
        self.assertIsNone(weighted_average(settings, states, 1001)[0])
        settings.coverage = 0.25
        self.assertEqual(weighted_average(settings, states, 1001)[0], 20)
        states["sensor.a"].attributes["unit_of_measurement"] = "K"
        self.assertIsNone(ambient_value(states["sensor.a"], 1001, 900))

    def test_schedule_validation_and_week_wrap(self):
        entries = validate_schedule([{"day": 0, "time": "00:00", "target": 20}, {"day": 4, "time": "22:00", "target": 18}])
        self.assertEqual(schedule_target(entries, datetime(2026, 10, 11, 12)), 18)
        self.assertEqual(schedule_target(entries, datetime(2026, 10, 12, 12)), 20)
        for entries in ([], [{"day": 0, "time": "24:00", "target": 20}], [{"day": 1, "time": "00:00", "target": 20}], [{"day": 0, "time": "00:00", "target": 20.1}]):
            with self.assertRaises(ValueError):
                validate_schedule(entries)

    def test_limiter_slew_and_eventual_downward_hysteresis(self):
        settings = ComfortSettings(smoothing_seconds=0)
        memory = ComfortMemory(last_command=23, last_command_at=1000)
        correction = Correction()
        value = correction.evaluate(settings, memory, 21, 21, 19, 1001, 23)
        self.assertFalse(value["due"])
        self.assertEqual(value["requested_output"], 22.5)
        value = correction.evaluate(settings, memory, 21, 21, 19, 1600, 23)
        self.assertTrue(value["due"])
        # A tiny offset beyond quantization boundary eventually escapes hold.
        correction.reset()
        memory.last_command = 22.5
        self.assertEqual(correction.evaluate(settings, memory, 22, 22.24, 22, 1600, 22.5)["requested_output"], 22.5)
        self.assertEqual(correction.evaluate(settings, memory, 22, 22.24, 22, 2800, 22.5)["requested_output"], 22)

    def test_elapsed_smoothing_and_saturation(self):
        correction = Correction()
        memory = ComfortMemory()
        settings = ComfortSettings()
        correction.evaluate(settings, memory, 20, 21, 22, 0, 20)
        result = correction.evaluate(settings, memory, 30, 21, 22, 60, 20)
        self.assertAlmostEqual(result["smoothed_output"], 23 + (13 - 23) * (1 - __import__("math").exp(-1)))
        self.assertTrue(result["saturated"])

    def test_native_roundtrip_preserves_mandatory_away_frost(self):
        source = native_schedule()
        entries = [{"day": 0, "time": "00:00", "target": 21.5}, {"day": 6, "time": "22:00", "target": 18}]
        payload = build_native(entries, source, {"rooms": [{"id": "room"}]}, "room")
        self.assertEqual(payload["zones"][:3], source["zones"])
        self.assertEqual((payload["hg_temp"], payload["away_temp"]), (7, 16))
        self.assertEqual(native_entries(payload, "room"), entries)
        with self.assertRaises(ValueError):
            build_native(entries, source, {"rooms": [{"id": "room"}, {"id": "other"}]}, "room")
        with self.assertRaises(ValueError):
            checked({"status": "ok", "error": {"code": 21}})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1800000000
        self.patcher = patch.object(migo, "time", lambda: self.now)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.hass = Hass()
        self.hass.states.water("sensor.ambient", 20)
        self.fresh()
        self.controller = controller_module.HeatingController(self.hass, SimpleNamespace(entry_id="test"), [{"id": "zone", "sensor": "sensor.water", "label": "heating", "name": "Zone"}])
        self.bridge = self.controller.comfort
        self.bridge.settings = ComfortSettings(migo_entity="climate.migo", home_id="home", room_id="room", schedule_id="original", room_sensors={"sensor.ambient": 1}, smoothing_seconds=0)
        self.api = Api(lambda: self.now)
        self.bridge._api = lambda: self.api

    def fresh(self):
        self.hass.states.get("sensor.ambient").last_reported = datetime.fromtimestamp(self.now, UTC)

    async def ready(self):
        await self.bridge.prepare_schedule()
        await self.bridge.enable()

    async def reconcile(self):
        self.fresh()
        self.bridge.last_poll = self.bridge.last_config_poll = None
        await self.bridge.reconcile()

    def commands(self):
        return [call for call in self.api.calls if call[0] == "room" and call[-1]["mode"] == "manual"]

    async def test_passive_startup_does_not_change_duration_or_target(self):
        await self.reconcile()
        self.assertEqual(self.api.calls, [])
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        self.assertEqual(restored.memory.owner, "miGo")

    async def test_dedicated_copy_and_nonselected_update_preserve_other_schedules(self):
        before = deepcopy(self.api.home["schedules"])
        await self.bridge.prepare_schedule()
        self.assertEqual(self.api.home["schedules"][:2], before)
        entries = [{"day": 0, "time": "00:00", "target": 21.5}]
        await self.bridge.set_schedule(entries)
        self.assertEqual(self.api.home["schedules"][:2], before)
        payload = next(call[1] for call in self.api.calls if call[0] == "/api/synchomeschedule")
        self.assertEqual(payload["schedule_id"], "managed")
        self.assertEqual(native_entries(self.api.home["schedules"][-1], "room"), entries)
        self.assertEqual(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)

    async def test_changed_target_resets_shared_duration_without_command_ttl(self):
        await self.ready()
        await self.reconcile()
        command = self.commands()[-1]
        self.assertEqual(command[-1], {"mode": "manual", "temp": 20.5})
        self.assertEqual(self.bridge.memory.native_expires_at, self.now + 3600)
        self.assertEqual(self.api.home["therm_setpoint_default_duration"], 60)

    async def test_user_target_change_does_not_bypass_limiter(self):
        await self.ready()
        await self.reconcile()
        count = len(self.commands())
        await self.bridge.set_manual(25, 120)
        self.now += 30
        await self.reconcile()
        self.assertEqual(len(self.commands()), count)
        self.now += 570
        await self.reconcile()
        self.assertEqual(self.commands()[-1][-1]["temp"], 21)

    async def test_same_value_renewal_checks_real_expiry(self):
        await self.ready()
        self.hass.states.get("sensor.ambient").state = "23"
        await self.reconcile()
        initial = self.bridge.memory.native_expires_at
        self.now = initial - 100
        await self.reconcile()
        self.assertGreater(self.bridge.memory.native_expires_at, initial)
        self.assertEqual(self.commands()[-1][-1]["temp"], 20)

    async def test_failed_same_value_renewal_not_claimed_success(self):
        await self.ready()
        self.hass.states.get("sensor.ambient").state = "23"
        await self.reconcile()
        self.api.renew = False
        self.now = self.bridge.memory.native_expires_at - 100
        await self.reconcile()
        self.assertIn("did not extend", self.bridge.fault)
        self.assertEqual(self.bridge.memory.owner, "miGo")

    async def test_fault_cancels_owned_bias_and_no_room_fan_calls(self):
        await self.ready()
        await self.reconcile()
        self.hass.states.get("sensor.ambient").state = "unavailable"
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.api.room["therm_setpoint_mode"], "home")
        self.assertEqual(self.hass.services.calls, [])

    async def test_external_override_and_early_cancel_are_respected(self):
        for mode in ("manual", "home", "away", "off"):
            with self.subTest(mode=mode):
                self.api.room = {"id": "room", "therm_measured_temperature": 21, "therm_setpoint_temperature": 20, "therm_setpoint_mode": "home"}
                await self.ready()
                await self.reconcile()
                count = len(self.commands())
                self.api.room["therm_setpoint_mode"] = mode
                self.api.room["therm_setpoint_temperature"] = 25
                self.now += 600
                await self.reconcile()
                self.assertEqual(self.bridge.memory.owner, "miGo")
                self.assertEqual(len(self.commands()), count)
                self.now += 100

    async def test_release_failure_persists_pending_over_restart(self):
        await self.ready()
        await self.reconcile()
        self.api.fail_release = True
        with self.assertRaises(Exception):
            await self.bridge.relinquish()
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        self.assertEqual(restored.memory.owner, "Release pending")
        self.api.fail_release = False
        self.bridge = restored
        self.bridge._api = lambda: self.api
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")

    async def test_mapping_change_invalidates_previous_confirmation(self):
        await self.bridge.prepare_schedule()
        await self.bridge.configure({"home_id": "another"})
        self.assertFalse(self.bridge.memory.confirmed_schedule)
        with self.assertRaises(Exception):
            await self.bridge.enable()

    async def test_native_sync_success_read_failure_recovers_after_restart(self):
        await self.bridge.prepare_schedule()
        self.api.fail_sync_read = True
        entries = [{"day": 0, "time": "00:00", "target": 23}]
        await self.bridge.set_schedule(entries)
        self.assertLess(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        self.api.fail_sync_read = self.api.fail_read = False
        restored._api = lambda: self.api
        await restored.retry_schedule()
        self.assertEqual(restored.memory.confirmed_schedule, entries)
        self.assertEqual(restored.memory.confirmed_version, restored.memory.schedule_version)
        self.assertEqual(len([call for call in self.api.calls if call[0] == "/api/synchomeschedule"]), 1)

    async def test_http200_error_keeps_schedule_pending(self):
        await self.bridge.prepare_schedule()
        self.api.http_error = True
        await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 23}])
        self.assertLess(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)
        self.assertIn("acknowledge", self.bridge.fault)

    async def test_external_schedule_is_never_clobbered(self):
        await self.bridge.prepare_schedule()
        self.api.home["schedules"][-1]["timetable"][0]["m_offset"] = 60
        count = len(self.api.calls)
        await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 23}])
        self.assertEqual(len(self.api.calls), count)
        self.assertIn("externally", self.bridge.fault)

    async def test_restart_preserves_manual_deadline_and_command_limiter(self):
        await self.ready()
        await self.reconcile()
        await self.bridge.set_manual(24, 60)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        self.assertEqual(restored.memory.last_command_at, self.bridge.memory.last_command_at)
        self.assertEqual(restored.memory.manual_expires_at, self.now + 3600)
        self.now += 3601
        value, source = restored.memory.target(restored.schedule, self.now, datetime(2026, 10, 12))
        self.assertEqual(source, "Schedule")
        self.assertEqual(value, restored.schedule[0]["target"])

    async def test_native_target_below_bounds_requires_explicit_operator_action(self):
        await self.bridge.prepare_schedule()
        self.api.room["therm_setpoint_temperature"] = 17
        with self.assertRaisesRegex(Exception, "within output bounds"):
            await self.bridge.enable()
        self.assertEqual(self.commands(), [])

    async def test_stalled_cloud_does_not_block_fan_off(self):
        await self.ready()
        self.hass.entity_registry.add("climate.fan", ["heating"])
        self.hass.states.room("climate.fan", 20)
        await self.controller._refresh_rooms()
        self.controller._started = True
        self.controller.settings.mode = "Manual"
        self.api.wait = asyncio.Event()
        self.bridge.last_poll = None
        await self.controller.async_reconcile_now()
        await asyncio.sleep(0)
        self.assertFalse(self.controller._comfort_task.done())
        self.controller.settings.mode = "Off"
        await asyncio.wait_for(self.controller.async_reconcile_now(), 0.2)
        self.assertEqual(self.hass.states.get("climate.fan").state, "off")
        self.api.wait.set()
        await self.controller._comfort_task
        await self.controller.async_stop(turn_off=False)

    async def test_legacy_target_preserved_without_comfort_configuration(self):
        bridge = MigoBridge(self.controller)
        self.controller.settings.target = 24
        await bridge.reconcile()
        self.assertEqual(self.controller.settings.target, 24)

    async def test_cloud_success_stale_or_unreachable_thermostat_rf_pauses(self):
        await self.ready()
        await self.reconcile()
        self.api.home["rooms"][0]["module_ids"] = ["rf"]
        self.api.home["modules"] = [{"id": "rf", "type": "NAThermVaillant", "room_id": "room"}]
        self.api.modules = [{"id": "rf", "type": "NAThermVaillant", "reachable": True, "last_seen": self.now - 1000}, {"id": "gateway", "type": "NAVaillant", "reachable": True, "last_seen": self.now}]
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertIn("stale", self.bridge.snapshot["reference_freshness"])
        self.assertIsNone(self.bridge.snapshot["reference_temperature"])

    async def test_native_timezone_survives_network_failure_restart_and_dst(self):
        self.api.home["timezone"] = "America/New_York"
        self.hass.config.time_zone = "Europe/Warsaw"
        await self.bridge.prepare_schedule()
        self.bridge.schedule = [{"day": 0, "time": "00:00", "target": 18}, {"day": 0, "time": "06:00", "target": 22}]
        self.now = datetime(2026, 10, 26, 8, tzinfo=UTC).timestamp()  # 04:00 NY, 09:00 Warsaw
        saved = self.bridge.serialize()
        restored = MigoBridge(self.controller)
        restored.load(saved)
        restored._api = lambda: self.api
        self.api.fail_read = True
        await restored.reconcile()
        self.assertEqual(self.controller.settings.target, 18)
        self.assertEqual(restored.memory.native_timezone, "America/New_York")

    async def test_unexpected_native_selection_during_sync_not_confirmed(self):
        await self.bridge.prepare_schedule()
        self.api.mutate_selection = True
        await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 23}])
        self.assertLess(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)
        self.assertIn("Unrelated", self.bridge.fault)

    async def test_wrong_native_duration_is_not_confirmed(self):
        await self.ready()
        self.api.wrong_expiry = True
        await self.reconcile()
        self.assertIn("shared duration", self.bridge.fault)
        self.assertNotEqual(self.bridge.memory.owner, "HA")
        self.assertEqual(self.api.room["therm_setpoint_mode"], "home")
        self.assertNotIn("therm_setpoint_end_time", self.api.room)

    async def test_wrong_ttl_cancellation_failure_survives_restart_and_preserves_replacement(self):
        await self.ready()
        self.api.wrong_expiry = self.api.fail_release = True
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "Release pending")
        self.assertEqual(self.bridge.memory.native_expires_at, self.api.room["therm_setpoint_end_time"])
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        self.api.fail_release = False
        self.api.room.update(therm_setpoint_temperature=25, therm_setpoint_end_time=self.now + 7200)
        count = len(self.api.calls)
        await restored.reconcile()
        self.assertEqual(restored.memory.owner, "miGo")
        self.assertEqual(len(self.api.calls), count)
        self.assertEqual(self.api.room["therm_setpoint_temperature"], 25)

    async def test_relinquish_never_cancels_external_manual_or_protection(self):
        for mode in ("manual", "off", "hg", "away", "home"):
            self.bridge.memory.owner = "miGo"
            self.bridge.memory.last_command = None
            self.bridge.memory.native_expires_at = None
            self.api.room.update(therm_setpoint_mode=mode, therm_setpoint_temperature=25)
            self.api.room["therm_setpoint_end_time"] = self.now + 7200
            count = len(self.api.calls)
            await self.bridge.relinquish()
            self.assertEqual(len(self.api.calls), count)
            self.assertEqual(self.api.room["therm_setpoint_mode"], mode)
            self.assertEqual(self.bridge.memory.owner, "miGo")

    async def test_expired_native_value_is_actual_slew_baseline(self):
        await self.ready()
        await self.reconcile()
        self.now = self.bridge.memory.native_expires_at + 1
        self.api.room.update(therm_setpoint_mode="home", therm_setpoint_temperature=19)
        self.api.room.pop("therm_setpoint_end_time", None)
        await self.reconcile()
        self.assertEqual(self.commands()[-1][-1]["temp"], 19.5)

    async def test_unload_relinquishes_but_shutdown_retains_native_expiry(self):
        await self.ready()
        await self.reconcile()
        await self.controller.async_stop(turn_off=False)
        self.assertEqual(self.api.room["therm_setpoint_mode"], "manual")
        self.controller._closed = False
        await self.controller.async_stop(turn_off=True)
        self.assertEqual(self.api.room["therm_setpoint_mode"], "home")

    async def test_reference_alias_and_generated_water_cannot_enter_average(self):
        for id in ("climate.migo", "sensor.migo_ambient"):
            self.hass.entity_registry.add(id, [])
            entity = self.hass.entity_registry.async_get(id)
            entity.config_entry_id = "native"
            entity.platform = "migo_netatmo"
        for id in ("sensor.migo_ambient", "sensor.central_heating_zone_water_temperature"):
            with self.assertRaises(Exception):
                await self.bridge.configure({"room_sensors": {id: 1}})

    async def test_shared_duration_changes_only_on_explicit_enable_or_changed_config(self):
        await self.ready()
        count = len([call for call in self.api.calls if call[0] == "duration"])
        await self.bridge.configure({"minimum": 18})
        self.assertEqual(len([call for call in self.api.calls if call[0] == "duration"]), count)
        await self.bridge.configure({"manual_minutes": 120})
        self.assertEqual(len([call for call in self.api.calls if call[0] == "duration"]), count + 1)

    async def test_runtime_mapping_uses_selected_integration_without_credentials(self):
        self.hass.entity_registry.add("climate.migo", [])
        self.hass.entity_registry.async_get("climate.migo").config_entry_id = "native"
        self.hass.config_entries = SimpleNamespace(async_get_entry=lambda _: SimpleNamespace(domain="migo_netatmo", runtime_data=SimpleNamespace(api=self.api)))
        bridge = MigoBridge(self.controller)
        bridge.settings = self.bridge.settings
        self.assertIs(bridge._api(), self.api)

    async def test_invalid_stored_numeric_types_reset_without_ownership(self):
        raw = self.bridge.serialize()
        raw["memory"]["manual_expires_at"] = "1234"
        restored = MigoBridge(self.controller)
        restored.load(raw)
        self.assertEqual(restored.memory.owner, "miGo")
        self.assertIn("Invalid stored", restored.fault)

    async def test_unconfigured_global_override_is_actionable_error(self):
        with self.assertRaisesRegex(Exception, "Configure miGo"):
            await MigoBridge(self.controller).set_manual(23, 60)

    async def test_preparation_cannot_silently_erase_saved_desired_edits(self):
        before = deepcopy(self.bridge.schedule)
        with self.assertRaisesRegex(Exception, "Prepare"):
            await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 24}])
        self.assertEqual(self.bridge.schedule, before)
        self.assertEqual(self.bridge.memory.schedule_version, 0)

    async def test_interrupted_creation_recovers_without_duplicate(self):
        self.api.fail_sync_read = True
        with self.assertRaises(Exception):
            await self.bridge.prepare_schedule()
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        self.api.fail_sync_read = self.api.fail_read = False
        await restored.prepare_schedule()
        self.assertEqual(len([call for call in self.api.calls if call[0] == "/api/createnewhomeschedule"]), 1)
        self.assertEqual(restored.memory.managed_schedule_id, "managed")

    async def test_stable_comfort_ticks_do_not_write_storage_or_publish_protocol_payloads(self):
        self.controller.comfort = MigoBridge(self.controller)
        self.controller._saved = self.controller._serialize()
        with patch.object(self.controller._store, "async_delay_save") as delayed:
            for _ in range(10):
                await self.controller._run_comfort()
            delayed.assert_not_called()
        for key in ("native_schedule", "pending_sync", "pending_create", "confirmed_schedule"):
            self.assertNotIn(key, self.controller.comfort.snapshot)

    async def test_unknown_manual_before_first_command_and_restart_never_overwritten(self):
        await self.ready()
        self.api.room.update(therm_setpoint_mode="manual", therm_setpoint_temperature=22, therm_setpoint_end_time=self.now + 7200)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        await restored.reconcile()
        self.assertEqual(restored.memory.owner, "miGo")
        self.assertEqual(self.commands(), [])

    async def test_fresh_prewrite_read_detects_override_inside_poll_cache_window(self):
        await self.ready()
        self.api.room.update(therm_setpoint_mode="manual", therm_setpoint_temperature=22, therm_setpoint_end_time=self.now + 7200)
        self.bridge.last_poll = self.bridge.last_config_poll = self.now
        self.fresh()
        await self.bridge.reconcile()  # cached room still home; immediate pre-write read must win
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.commands(), [])

    async def test_fresh_prewrite_config_detects_native_away_inside_cache(self):
        await self.ready()
        self.api.home["therm_mode"] = "away"
        self.bridge.last_poll = self.bridge.last_config_poll = self.now
        self.fresh()
        await self.bridge.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.commands(), [])

    async def test_slow_cloud_calls_cannot_shorten_actual_command_cadence(self):
        await self.ready()
        self.api.status_delay = 30
        self.api.advance_clock = lambda seconds: setattr(self, "now", self.now + seconds)
        await self.reconcile()
        confirmed_at = self.bridge.memory.last_command_at
        self.now = self.api.command_times[-1] + 599
        await self.reconcile()
        self.assertEqual(len(self.commands()), 1)
        self.now = confirmed_at + 600
        await self.reconcile()
        self.assertEqual(len(self.commands()), 2)
        self.assertGreaterEqual(self.api.command_times[1] - self.api.command_times[0], 600)

    async def test_restart_during_unconfirmed_native_write_relinquishes_conservatively(self):
        await self.ready()
        self.bridge.memory.pending_command = True
        self.bridge.memory.last_command = 22
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        await restored.reconcile()
        self.assertEqual(restored.memory.owner, "miGo")
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
