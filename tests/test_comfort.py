"""Comfort policy and mocked native transport. No live actuator writes."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_controller import Hass, controller_module
from adapter_under_test.comfort import ComfortMemory, ComfortSettings, Correction, ambient_value, schedule_target, validate_schedule, weighted_average
from adapter_under_test.migo import MigoBridge
from adapter_under_test.schedule import build_dhw, build_native, canonical, canonical_dhw, checked, checked_body, effective_entries, native_entries, validate_native
import adapter_under_test.migo as migo


def native_schedule(id="original", name="User schedule", selected=True):
    return {"id": id, "name": name, "type": "therm", "selected": selected, "default": True,
            "hg_temp": 7, "away_temp": 16,
            "zones": [{"id": id, "type": kind, "name": label, "rooms": [{"id": "room", "therm_setpoint_temperature": target}]}
                      for id, kind, label, target in ((0, 0, "Comfort", 22), (1, 1, "Night", 20), (4, 5, "Eco", 18))],
            "timetable": [{"zone_id": 1, "m_offset": 0}, {"zone_id": 0, "m_offset": 420}]}


def migo_schedule_metadata(schedule):
    result = deepcopy(schedule)
    for zone in result["zones"]:
        zone.setdefault("modules", [])
        zone.setdefault("rooms_temp", [{"room_id": room["id"], "temp": room["therm_setpoint_temperature"]} for room in zone["rooms"]])
    return result


class Api:
    def __init__(self, clock):
        self.clock = clock
        self.home = {"id": "home", "timezone": "Europe/Warsaw", "therm_mode": "schedule", "temperature_control_mode": "heating", "therm_setpoint_default_duration": 720,
                     "rooms": [{"id": "room"}], "schedules": [native_schedule(), {"id": "dhw", "name": "DHW", "type": "event", "zones": [{"id": 0, "modules": [{"id": "boiler", "dhw_enabled": True}]}], "timetable": [{"zone_id": 0, "m_offset": 0}]}]}
        self.room = {"id": "room", "reachable": True, "therm_measured_temperature": 21, "therm_setpoint_temperature": 20, "therm_setpoint_mode": "home"}
        self.calls = []
        self.read_calls = []
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
        self.migo_zone_metadata = False
        self.write_ack = {"status": "ok"}

    def with_dhw(self):
        self.home["modules"] = [{"id": "boiler", "type": "NAVaillant"}]
        self.home["linked_schedules"] = [{"therm": "original", "event": "dhw"}]
        self.home["schedules"][1].update(selected=True, default=False, timetable_sunrise=[], timetable_sunset=[],
            zones=[{"id": id, "rooms": [], "modules": [{"id": "boiler", "dhw_enabled": enabled}]} for id, enabled in ((0, True), (1, False), (4, False))])

    async def get_homes_data(self):
        self.read_calls.append("config")
        if self.fail_read:
            raise ValueError("Offline")
        home = deepcopy(self.home)
        if self.migo_zone_metadata:
            home["schedules"] = [migo_schedule_metadata(item) if item.get("type") == "therm" else item for item in home["schedules"]]
        return {"status": "ok", "body": {"homes": [home]}}

    async def get_home_status(self, home):
        self.read_calls.append("status")
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
        return deepcopy(self.write_ack)

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
        return deepcopy(self.write_ack)

    async def switch_home_schedule(self, home, id):
        self.calls.append(("switch", id))
        kind = next(item["type"] for item in self.home["schedules"] if item["id"] == id)
        for item in self.home["schedules"]:
            if item["type"] == kind:
                item["selected"] = item["id"] == id
        return deepcopy(self.write_ack)

    async def _api_request(self, url):
        if urlsplit(url).scheme != "https" or urlsplit(url).netloc != "api.netatmo.com":
            raise AssertionError("Schedule calls must use the documented public HTTPS server")
        params = parse_qs(urlsplit(url).query)
        values = {key: json.loads(value[0]) if key in ("zones", "timetable") else value[0] for key, value in params.items()}
        self.calls.append((urlsplit(url).path, deepcopy(values)))
        if self.http_error:
            return {"status": "error", "error": {"code": 21}}
        slots = values.get("timetable", [])
        if any(first["zone_id"] == second["zone_id"] for first, second in zip(slots, slots[1:])):
            return {"status": "ok", "body": {"error": {"code": 21, "message": "two same consecutive zone_id in timetable"}}}
        if urlsplit(url).path.endswith("createnewhomeschedule"):
            kind = values.get("schedule_type", "therm")
            self.home["schedules"].append({"id": "managed_dhw" if kind == "event" else "managed", "type": kind, "selected": False, **values})
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
        return deepcopy(self.write_ack)


class PolicyTests(unittest.TestCase):
    def test_export_removes_midnight_noops_without_changing_weekly_targets(self):
        entries = [{"day": day, "time": when, "target": target} for day in range(7)
                   for when, target in (("00:00", 21.5), ("07:30", 20), ("20:00", 21.5))]
        original = deepcopy(entries)
        payload = build_native(entries, native_schedule(), {"rooms": [{"id": "room"}]}, "room")
        slots = payload["timetable"]
        self.assertEqual(len(slots), 15)
        self.assertEqual(slots[0]["m_offset"], 0)
        self.assertTrue(all(first["zone_id"] != second["zone_id"] for first, second in zip(slots, slots[1:])))
        exported = native_entries(payload, "room")
        self.assertEqual(exported, effective_entries(entries))
        for day in range(7):
            for minute in (0, 449, 450, 1199, 1200, 1439):
                when = datetime(2026, 10, 12) + timedelta(days=day, minutes=minute)
                self.assertEqual(schedule_target(entries, when), schedule_target(exported, when))
        self.assertEqual(entries, original)
        constant = [{"day": day, "time": "00:00", "target": 20} for day in range(7)]
        self.assertEqual(effective_entries(constant), constant[:1])
        self.assertEqual(len(build_native(constant, native_schedule(), {"rooms": [{"id": "room"}]}, "room")["timetable"]), 1)

    def test_native_error_reports_operation_and_code_not_cloud_message(self):
        response = {"status": "ok", "body": {"error": {"code": 21, "message": "private cloud payload"}}}
        with self.assertRaises(ValueError) as caught:
            checked(response, "synchomeschedule")
        self.assertEqual(str(caught.exception), "Native API did not acknowledge request (synchomeschedule, error code 21)")
        self.assertNotIn("private", str(caught.exception))

    def test_acknowledgements_accept_empty_array_but_reject_errors(self):
        for response in ({"status": "ok"}, {"status": "ok", "body": []}, {"status": "ok", "body": {}}, {"status": "ok", "body": {"schedule_id": "managed"}}):
            with self.subTest(response=response):
                self.assertEqual(checked(response), response)
        for response in ([], {"status": "error", "body": []}, {"status": "ok", "errors": [21]}, {"status": "ok", "error": {"code": 21}},
                         {"status": "ok", "body": {"errors": [21]}}, {"status": "ok", "body": {"error": {"code": 21}}},
                         {"status": "ok", "body": [{"error": 21}]}, {"status": "ok", "body": ["ok"]}, {"status": "ok", "body": None}, {"status": "ok", "body": True}):
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, "acknowledge"):
                checked(response)
        for response in ({"status": "ok"}, {"status": "ok", "body": []}):
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, "read body"):
                checked_body(response)

    def test_settings_validation(self):
        for data in ({"minimum": 31}, {"manual_minutes": 4}, {"minimum": "18"}, {"room_sensors": {"sensor.a": float("nan")}}, {"room_sensors": {"sensor.a": "1"}}, {"coverage": 0}, {"correction_seconds": 59}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ComfortSettings(**data).validate()
        ComfortSettings(manual_minutes=5).validate()
        for entity in ("sensor.water", "climate.migo"):
            with self.assertRaises(ValueError):
                ComfortSettings(migo_entity="climate.migo", room_sensors={entity: 1}).validate({"sensor.water"})

    def test_correction_interval_range_keeps_default_and_accepts_shorter_intervals(self):
        self.assertEqual(ComfortSettings().correction_seconds, 600)
        for seconds in (60, 120, 180, 599, 600, 900, 7200):
            with self.subTest(seconds=seconds):
                ComfortSettings(correction_seconds=seconds).validate()
        for seconds in (0, 59, 7201, 60.5, "60", True, float("nan")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                ComfortSettings(correction_seconds=seconds).validate()

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

    def test_configured_interval_drives_cadence_and_hysteresis_hold(self):
        for seconds in (60, 180, 599, 900):
            with self.subTest(seconds=seconds):
                settings = ComfortSettings(correction_seconds=seconds, smoothing_seconds=0)
                memory = ComfortMemory(last_command=22.5, last_command_at=1000)
                correction = Correction()
                self.assertFalse(correction.evaluate(settings, memory, 22, 22.24, 22, 1001, 22.5)["due"])
                self.assertFalse(correction.evaluate(settings, memory, 22, 22.24, 22, 1000 + seconds - 0.01, 22.5)["due"])
                result = correction.evaluate(settings, memory, 22, 22.24, 22, 1000 + seconds, 22.5)
                self.assertTrue(result["due"])
                self.assertEqual(result["next_correction_at"], 1000 + seconds)
                self.assertEqual(result["requested_output"], 22.5)
                self.assertEqual(correction.evaluate(settings, memory, 22, 22.24, 22, 1001 + 2 * seconds - 0.01, 22.5)["requested_output"], 22.5)
                self.assertEqual(correction.evaluate(settings, memory, 22, 22.24, 22, 1001 + 2 * seconds, 22.5)["requested_output"], 22)

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

    def test_migo_redundant_zone_fields_normalize_without_mutation(self):
        plain = native_schedule()
        source = migo_schedule_metadata(plain)
        before = deepcopy(source)
        home = {"rooms": [{"id": "room"}]}
        validate_native(source, home, "room")
        self.assertEqual(canonical(source), canonical(plain))
        entries = [{"day": 0, "time": "00:00", "target": 21.5}]
        payload = build_native(entries, source, home, "room")
        self.assertEqual(payload["zones"][:3], plain["zones"])
        self.assertTrue(all("modules" not in zone and "rooms_temp" not in zone for zone in payload["zones"]))
        self.assertEqual(native_entries(migo_schedule_metadata(payload), "room"), entries)
        self.assertEqual(source, before)
        for field in ("modules", "rooms_temp"):
            variant = deepcopy(source)
            for zone in variant["zones"]:
                zone.pop(field)
            with self.subTest(omitted=field):
                validate_native(variant, home, "room")
                self.assertEqual(canonical(variant), canonical(plain))

    def test_migo_zone_metadata_cannot_hide_dhw_or_unknown_fields(self):
        for extra in ({"modules": [{"id": "boiler", "dhw_enabled": True}]}, {"modules": None}, {"modules": {}}, {"dhw_enabled": False}, {"unknown": []}):
            source = migo_schedule_metadata(native_schedule())
            source["zones"][0].update(extra)
            for validate in (lambda: validate_native(source, {"rooms": [{"id": "room"}]}, "room"), lambda: canonical(source)):
                with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "Complex/DHW"):
                    validate()

    def test_migo_room_aliases_must_match_exactly(self):
        for aliases in (None, {}, [], [{"room_id": "other", "temp": 22}], [{"room_id": "room", "temp": 21}], [{"room_id": "room", "temp": True}], [{"room_id": "room", "temp": 22, "unknown": 1}], [{"room_id": "room", "temp": 22}] * 2):
            source = migo_schedule_metadata(native_schedule())
            source["zones"][0]["rooms_temp"] = aliases
            with self.subTest(aliases=aliases), self.assertRaisesRegex(ValueError, "aliases mismatch"):
                canonical(source)
        source = migo_schedule_metadata(native_schedule())
        source["zones"][0]["rooms"][0]["therm_setpoint_temperature"] = 23
        with self.assertRaisesRegex(ValueError, "aliases mismatch"):
            canonical(source)


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

    async def test_dedicated_copy_and_explicit_save_activate_without_editing_originals(self):
        before = deepcopy(self.api.home["schedules"])
        await self.bridge.prepare_schedule()
        self.assertEqual(self.api.home["schedules"][:2], before)
        entries = [{"day": 0, "time": "00:00", "target": 21.5}]
        await self.bridge.set_schedule(entries)
        before[0]["selected"] = False
        self.assertEqual(self.api.home["schedules"][:2], before)
        self.assertTrue(self.api.home["schedules"][-1]["selected"])
        payload = next(call[1] for call in self.api.calls if call[0] == "/api/synchomeschedule")
        self.assertEqual(payload["schedule_id"], "managed")
        self.assertEqual(native_entries(self.api.home["schedules"][-1], "room"), entries)
        self.assertEqual(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)

    async def test_migo_metadata_create_sync_and_restart_preserve_dhw(self):
        self.api.migo_zone_metadata = True
        self.api.home["schedules"][0] = migo_schedule_metadata(self.api.home["schedules"][0])
        before = deepcopy(self.api.home["schedules"])
        await self.bridge.prepare_schedule()
        entries = [{"day": 0, "time": "00:00", "target": 21.5}, {"day": 6, "time": "22:00", "target": 18}]
        await self.bridge.set_schedule(entries)
        self.assertIsNone(self.bridge.fault)
        self.assertEqual(self.bridge.memory.confirmed_schedule, entries)
        before[0]["selected"] = False
        self.assertEqual(self.api.home["schedules"][:2], before)
        self.assertTrue(self.api.home["schedules"][-1]["selected"])
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.commands(), [])
        for operation, payload in self.api.calls:
            self.assertIn(operation, ("/api/createnewhomeschedule", "/api/synchomeschedule", "switch"))
            if operation != "switch":
                self.assertTrue(all("modules" not in zone and "rooms_temp" not in zone for zone in payload["zones"]))
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        count = len(self.api.calls)
        await restored.prepare_schedule()
        self.assertEqual(len(self.api.calls), count)
        self.assertEqual(restored.memory.confirmed_schedule, entries)
        self.assertEqual(self.api.home["schedules"][:2], before)

    async def test_invalid_migo_metadata_blocks_creation_without_writes(self):
        source = self.api.home["schedules"][0]
        for extra in ({"modules": [{"id": "boiler", "dhw_enabled": True}]}, {"rooms_temp": [{"room_id": "room", "temp": 25}]}):
            self.api.home["schedules"][0] = migo_schedule_metadata(source)
            self.api.home["schedules"][0]["zones"][0].update(extra)
            before = deepcopy(self.api.home["schedules"])
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                await self.bridge.prepare_schedule()
            self.assertEqual(self.api.calls, [])
            self.assertEqual(self.api.home["schedules"], before)
            self.assertEqual(self.bridge.memory.pending_create, {})

    async def test_migo_alias_mismatch_in_readback_does_not_confirm_sync(self):
        self.api.migo_zone_metadata = True
        await self.bridge.prepare_schedule()
        original_read = self.api.get_homes_data

        async def inconsistent_readback():
            response = await original_read()
            if any(call[0] == "/api/synchomeschedule" for call in self.api.calls):
                managed = response["body"]["homes"][0]["schedules"][-1]
                managed["zones"][-1]["rooms_temp"][0]["temp"] += 0.5
            return response

        self.api.get_homes_data = inconsistent_readback
        await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 23}])
        self.assertIn("aliases mismatch", self.bridge.fault)
        self.assertTrue(self.bridge.memory.pending_sync)
        self.assertLess(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.commands(), [])

    async def test_migo_metadata_interrupted_sync_recovers_without_second_write(self):
        self.api.migo_zone_metadata = True
        await self.bridge.prepare_schedule()
        before = deepcopy(self.api.home["schedules"][:2])
        self.api.fail_sync_read = True
        entries = [{"day": 0, "time": "00:00", "target": 23}]
        await self.bridge.set_schedule(entries)
        self.assertTrue(self.bridge.memory.pending_sync)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        self.api.fail_read = self.api.fail_sync_read = False
        count = len(self.api.calls)
        await restored.retry_schedule()
        self.assertEqual(len(self.api.calls), count + 1)
        self.assertEqual(self.api.calls[-1], ("switch", "managed"))
        self.assertEqual(restored.memory.pending_sync, {})
        self.assertEqual(restored.memory.confirmed_schedule, entries)
        before[0]["selected"] = False
        self.assertEqual(self.api.home["schedules"][:2], before)

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

    async def test_short_interval_configuration_is_persisted_and_respected(self):
        await self.bridge.configure({"correction_seconds": 60})
        await self.ready()
        await self.reconcile()
        confirmed_at = self.bridge.memory.last_command_at
        self.assertEqual(self.commands()[-1][-1]["temp"], 20.5)
        self.now = confirmed_at + 59
        await self.reconcile()
        self.assertEqual(len(self.commands()), 1)
        self.assertEqual(self.bridge.snapshot["status"], "Waiting for actuator limiter")
        self.now = confirmed_at + 60
        await self.reconcile()
        self.assertEqual(len(self.commands()), 2)
        self.assertEqual(self.commands()[-1][-1]["temp"], 21)
        self.assertGreaterEqual(self.api.command_times[-1] - self.api.command_times[-2], 60)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        self.assertEqual(restored.settings.correction_seconds, 60)
        self.assertEqual(restored.memory.last_command_at, self.bridge.memory.last_command_at)

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

    async def test_read_outage_keeps_ha_selection_and_retries_without_writes(self):
        await self.ready()
        await self.reconcile()
        saved = self.bridge.serialize()
        writes = deepcopy(self.api.calls)
        self.api.fail_read = True
        self.now += 60
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "HA")
        self.assertEqual(self.bridge.serialize(), saved)
        self.assertEqual(self.api.calls, writes)
        self.assertTrue(self.bridge.snapshot["connection_retrying"])
        self.assertIsNone(self.bridge.snapshot["reference_temperature"])
        self.assertIsNone(self.bridge.snapshot["native_target"])
        self.assertNotIn("requested_output", self.bridge.snapshot)
        reads = len(self.api.read_calls)
        self.api.fail_read = False
        self.now += 59
        await self.reconcile()
        self.assertEqual(len(self.api.read_calls), reads)
        self.assertEqual(self.api.calls, writes)
        self.now += 1
        await self.reconcile()
        self.assertEqual(self.api.read_calls[-2:], ["status", "config"])
        self.assertEqual(self.bridge.memory.owner, "HA")
        self.assertIsNone(self.bridge.fault)
        self.assertFalse(self.bridge.snapshot["connection_retrying"])
        self.assertEqual(self.api.calls, writes)

    async def test_unacknowledged_reads_retry_without_releasing_ha(self):
        await self.ready()
        await self.reconcile()
        for method in ("get_home_status", "get_homes_data"):
            for response in ({"status": "error", "error": {"code": 500, "message": "private-secret"}},
                             {"status": "error"}, None):
                with self.subTest(method=method, response=response):
                    saved = self.bridge.serialize()
                    writes = deepcopy(self.api.calls)
                    self.now += 60
                    with patch.object(self.api, method, return_value=response):
                        await self.reconcile()
                    self.assertEqual(self.bridge.serialize(), saved)
                    self.assertEqual(self.api.calls, writes)
                    self.assertTrue(self.bridge.snapshot["connection_retrying"])
                    self.assertIsNone(self.bridge.snapshot["native_target"])
                    self.assertNotIn("private-secret", self.bridge.fault)
                    self.now += 60
                    await self.reconcile()
                    self.assertEqual(self.bridge.memory.owner, "HA")
                    self.assertIsNone(self.bridge.fault)
                    self.assertFalse(self.bridge.snapshot["connection_retrying"])

    async def test_acknowledged_invalid_read_schema_is_not_an_outage(self):
        await self.ready()
        await self.reconcile()
        self.now += 60
        with patch.object(self.api, "get_home_status", return_value={"status": "ok", "body": []}):
            await self.reconcile()
        self.assertFalse(self.bridge.snapshot["connection_retrying"])
        self.assertNotEqual(self.bridge.memory.owner, "HA")
        self.assertIn("invalid read body", self.bridge.fault)

    async def test_config_read_outage_forces_both_fresh_reads_on_recovery(self):
        await self.ready()
        await self.reconcile()
        writes = deepcopy(self.api.calls)
        with patch.object(self.api, "get_homes_data", side_effect=ConnectionError("private-token-must-not-leak")):
            self.now += 60
            await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "HA")
        self.assertIsNone(self.bridge.last_poll)
        self.assertIsNone(self.bridge.last_config_poll)
        self.assertNotIn("private-token", self.bridge.fault)
        self.assertIsNone(self.bridge.snapshot["native_dhw_active"])
        self.now += 60
        await self.reconcile()
        self.assertEqual(self.api.read_calls[-2:], ["status", "config"])
        self.assertEqual(self.api.calls, writes)
        self.assertIsNone(self.bridge.fault)

    async def test_prewrite_read_failure_does_not_release_or_send_a_cached_command(self):
        await self.ready()
        await self.reconcile()
        saved = self.bridge.serialize()
        writes = deepcopy(self.api.calls)
        self.now += 600
        self.fresh()
        self.bridge.last_poll = self.bridge.last_config_poll = self.now
        with patch.object(self.api, "get_home_status", side_effect=ConnectionError("Offline")):
            await self.bridge.reconcile()
        self.assertEqual(self.bridge.serialize(), saved)
        self.assertEqual(self.api.calls, writes)
        self.assertNotIn("requested_output", self.bridge.snapshot)
        self.now += 60
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "HA")
        self.assertEqual(self.commands()[-1][-1]["temp"], 21)

    async def test_outage_recovery_after_native_expiry_uses_actual_slew_baseline(self):
        await self.ready()
        await self.reconcile()
        expiry = self.bridge.memory.native_expires_at
        self.now = expiry - 100
        self.api.fail_read = True
        await self.reconcile()
        self.now = expiry + 10
        self.api.fail_read = False
        self.api.room.update(therm_setpoint_mode="home", therm_setpoint_temperature=19)
        self.api.room.pop("therm_setpoint_end_time", None)
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "HA")
        self.assertEqual(self.commands()[-1][-1]["temp"], 19.5)

    async def test_read_outage_selection_survives_restart_without_reenabling(self):
        await self.ready()
        await self.reconcile()
        self.api.fail_read = True
        self.now += 60
        await self.reconcile()
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        self.assertEqual(restored.memory.owner, "HA")
        self.api.fail_read = False
        writes = deepcopy(self.api.calls)
        await restored.reconcile()
        self.assertEqual(restored.memory.owner, "HA")
        self.assertEqual(self.api.calls, writes)

    async def test_manual_override_during_outage_stops_automatic_recovery(self):
        await self.ready()
        await self.reconcile()
        self.api.fail_read = True
        self.now += 60
        await self.reconcile()
        self.api.room.update(therm_setpoint_mode="manual", therm_setpoint_temperature=24, therm_setpoint_end_time=self.now + 7200)
        writes = deepcopy(self.api.calls)
        self.api.fail_read = False
        self.now += 60
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.api.calls, writes)
        self.assertEqual(self.api.room["therm_setpoint_temperature"], 24)
        self.now += 600
        await self.reconcile()
        self.assertEqual(self.api.calls, writes)

    async def test_early_cancellation_during_outage_is_not_reapplied(self):
        await self.ready()
        await self.reconcile()
        self.api.fail_read = True
        self.now += 60
        await self.reconcile()
        self.api.room.update(therm_setpoint_mode="home", therm_setpoint_temperature=20)
        self.api.room.pop("therm_setpoint_end_time", None)
        writes = deepcopy(self.api.calls)
        self.api.fail_read = False
        self.now += 60
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.api.calls, writes)

    async def test_schedule_change_during_outage_is_not_overwritten(self):
        await self.ready()
        await self.reconcile()
        self.api.fail_read = True
        self.now += 60
        await self.reconcile()
        for schedule in self.api.home["schedules"]:
            if schedule.get("type") == "therm":
                schedule["selected"] = schedule["id"] == "original"
        writes = deepcopy(self.api.calls)
        self.api.fail_read = False
        self.now += 60
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.api.calls, writes)

    async def test_explicit_relinquish_during_outage_remains_disabled_after_recovery(self):
        await self.ready()
        await self.reconcile()
        manual_count = len(self.commands())
        self.api.fail_read = True
        self.now += 60
        await self.reconcile()
        with self.assertRaises(Exception):
            await self.bridge.relinquish()
        self.assertEqual(self.bridge.memory.owner, "Release pending")
        self.api.fail_read = False
        self.now += 60
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(self.api.room["therm_setpoint_mode"], "home")
        self.now += 600
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(len(self.commands()), manual_count)

    async def test_selected_ha_enable_is_idempotent_even_during_outage(self):
        await self.ready()
        await self.reconcile()
        saved = self.bridge.serialize()
        writes = deepcopy(self.api.calls)
        reads = len(self.api.read_calls)
        self.api.fail_read = True
        await self.bridge.enable()
        self.assertEqual(self.bridge.serialize(), saved)
        self.assertEqual(self.api.calls, writes)
        self.assertEqual(len(self.api.read_calls), reads)

    async def test_read_deadline_preserves_ha_and_schedules_retry(self):
        await self.ready()
        await self.reconcile()
        self.api.wait = asyncio.Event()
        self.bridge.last_poll = None
        timeout = asyncio.timeout
        with patch.object(migo.asyncio, "timeout", side_effect=lambda _: timeout(0.01)):
            await self.bridge.reconcile()
        self.assertEqual(self.bridge.memory.owner, "HA")
        self.assertTrue(self.bridge.snapshot["connection_retrying"])
        self.assertEqual(len(self.commands()), 1)

    async def test_repeated_read_failures_keep_selection_without_hammering_cloud(self):
        await self.ready()
        await self.reconcile()
        saved = self.bridge.serialize()
        self.api.fail_read = True
        self.now += 60
        await self.reconcile()
        reads = len(self.api.read_calls)
        for _ in range(3):
            self.now += 59
            await self.reconcile()
            self.assertEqual(len(self.api.read_calls), reads)
            self.now += 1
            await self.reconcile()
            reads += 1
            self.assertEqual(len(self.api.read_calls), reads)
            self.assertEqual(self.bridge.serialize(), saved)
            self.assertEqual(len(self.commands()), 1)

    async def test_unacknowledged_postwrite_read_is_not_automatically_replayed(self):
        await self.ready()
        write = self.api.set_room_state
        read = self.api.get_home_status
        failed = False

        async def write_then_reject_read(*args, **kwargs):
            nonlocal failed
            result = await write(*args, **kwargs)
            failed = True
            return result

        async def read_response(*args, **kwargs):
            if failed:
                return {"status": "error", "error": {"code": 500}}
            return await read(*args, **kwargs)

        with patch.object(self.api, "set_room_state", side_effect=write_then_reject_read), patch.object(self.api, "get_home_status", side_effect=read_response):
            await self.reconcile()
        self.assertNotEqual(self.bridge.memory.owner, "HA")
        self.assertFalse(self.bridge.snapshot["connection_retrying"])
        commands = len(self.commands())
        self.now += 600
        await self.reconcile()
        self.assertNotEqual(self.bridge.memory.owner, "HA")
        self.assertEqual(len(self.commands()), commands)

    async def test_write_readback_timeout_does_not_blindly_replay_command(self):
        await self.ready()
        write = self.api.set_room_state

        async def write_then_stall(*args, **kwargs):
            result = await write(*args, **kwargs)
            self.api.wait = asyncio.Event()
            return result

        timeout = asyncio.timeout
        with patch.object(self.api, "set_room_state", side_effect=write_then_stall), patch.object(migo.asyncio, "timeout", side_effect=lambda _: timeout(0.01)):
            await self.bridge.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertIn("confirmation unavailable", self.bridge.fault)
        self.assertEqual(len(self.commands()), 1)
        self.api.wait = None
        self.now += 600
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(len(self.commands()), 1)

    async def test_invalid_native_read_body_is_not_treated_as_connection_retry(self):
        await self.ready()
        await self.reconcile()
        with patch.object(self.api, "get_home_status", return_value={"status": "ok", "body": []}):
            await self.reconcile()
        self.assertNotEqual(self.bridge.memory.owner, "HA")
        self.assertFalse(self.bridge.snapshot["connection_retrying"])
        self.assertIn("invalid read body", self.bridge.fault)

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

    async def test_empty_array_acknowledgements_confirm_create_sync_and_enable(self):
        self.api.write_ack = {"status": "ok", "body": []}
        await self.bridge.prepare_schedule()
        entries = [{"day": 0, "time": "00:00", "target": 22}]
        await self.bridge.set_schedule(entries)
        await self.bridge.enable()
        self.assertEqual(self.bridge.memory.confirmed_schedule, entries)
        self.assertEqual(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)
        self.assertFalse(self.bridge.memory.pending_sync)
        self.assertEqual(self.bridge.memory.owner, "HA")
        self.assertIsNone(self.bridge.fault)

    async def test_repeated_daily_targets_roundtrip_keeps_editable_ha_plan(self):
        await self.bridge.prepare_schedule()
        entries = [{"day": day, "time": when, "target": target} for day in range(7)
                   for when, target in (("00:00", 21.5), ("07:30", 20), ("20:00", 21.5))]
        original = deepcopy(self.api.home["schedules"][:2])
        await self.bridge.set_schedule(entries)
        self.assertIsNone(self.bridge.fault)
        self.assertEqual(self.bridge.schedule, entries)
        self.assertEqual(self.bridge.memory.confirmed_schedule, entries)
        self.assertEqual(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)
        self.assertEqual(native_entries(self.api.home["schedules"][-1], "room"), effective_entries(entries))
        original[0]["selected"] = False
        self.assertEqual(self.api.home["schedules"][:2], original)

    async def test_compacted_pending_readback_recovers_without_rewriting_after_restart(self):
        await self.bridge.prepare_schedule()
        self.api.fail_sync_read = True
        entries = [{"day": day, "time": "00:00", "target": 22} for day in range(7)]
        await self.bridge.set_schedule(entries)
        self.assertTrue(self.bridge.memory.pending_sync)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        self.api.fail_sync_read = self.api.fail_read = False
        await restored.enable()
        self.assertEqual(restored.memory.confirmed_schedule, entries)
        self.assertEqual(restored.memory.confirmed_version, restored.memory.schedule_version)
        self.assertEqual(restored.memory.owner, "HA")
        self.assertFalse(restored.memory.pending_sync)
        self.assertEqual(len([call for call in self.api.calls if call[0] == "/api/synchomeschedule"]), 1)

    async def test_http200_body_error_is_not_confirmation(self):
        await self.bridge.prepare_schedule()

        async def reject(url):
            return {"status": "ok", "body": {"error": {"code": 21, "message": "private cloud response"}}}

        self.api._api_request = reject
        await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 22}])
        self.assertTrue(self.bridge.memory.pending_sync)
        self.assertLess(self.bridge.memory.confirmed_version, self.bridge.memory.schedule_version)
        self.assertIn("synchomeschedule, error code 21", self.bridge.fault)
        self.assertNotIn("private", self.bridge.fault)

    async def test_enable_recovers_successful_pending_sync_without_repeating_write(self):
        await self.bridge.prepare_schedule()
        self.api.fail_sync_read = True
        entries = [{"day": 0, "time": "00:00", "target": 22}]
        await self.bridge.set_schedule(entries)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        self.api.fail_sync_read = self.api.fail_read = False
        await restored.enable()
        self.assertEqual(restored.memory.confirmed_schedule, entries)
        self.assertFalse(restored.memory.pending_sync)
        self.assertEqual(restored.memory.owner, "HA")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "/api/synchomeschedule"]), 1)

    async def test_enable_never_retries_unacknowledged_schedule_write(self):
        await self.bridge.prepare_schedule()
        self.api.http_error = True
        await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 22}])
        before = deepcopy(self.api.calls)
        with self.assertRaisesRegex(Exception, "Confirm the real native"):
            await self.bridge.enable()
        self.assertEqual(self.api.calls, before)
        self.assertTrue(self.bridge.memory.pending_sync)
        self.assertEqual(self.bridge.memory.owner, "miGo")

    async def test_enable_pending_readback_rejects_external_changes(self):
        for change in (lambda home: home.update(timezone="Europe/Paris"),
                       lambda home: home["schedules"][0].update(name="Edited source"),
                       lambda home: home["schedules"][-1].update(selected=True)):
            self.bridge = MigoBridge(self.controller)
            self.bridge.settings = ComfortSettings(migo_entity="climate.migo", home_id="home", room_id="room", schedule_id="original")
            self.api = Api(lambda: self.now)
            self.bridge._api = lambda: self.api
            await self.bridge.prepare_schedule()
            self.api.fail_sync_read = True
            await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 22}])
            self.api.fail_sync_read = self.api.fail_read = False
            change(self.api.home)
            before = deepcopy(self.api.calls)
            with self.assertRaisesRegex(ValueError, "requires inspection"):
                await self.bridge.enable()
            self.assertEqual(self.api.calls, before)
            self.assertTrue(self.bridge.memory.pending_sync)
            self.assertEqual(self.bridge.memory.owner, "miGo")

    async def test_empty_array_read_body_blocks_without_native_writes(self):
        async def invalid_read(*args):
            return {"status": "ok", "body": []}

        self.api.get_homes_data = invalid_read
        for action in (self.bridge.prepare_schedule, self.bridge.discover, self.bridge.enable):
            with self.assertRaisesRegex(ValueError, "read body"):
                await action()
        self.api.get_home_status = invalid_read
        with self.assertRaisesRegex(ValueError, "read body"):
            await self.bridge._read(self.api)
        self.assertEqual(self.api.calls, [])

    async def test_dhw_copy_defaults_on_and_both_ha_schedules_are_active(self):
        self.api.with_dhw()
        original = deepcopy(self.api.home["schedules"])
        self.api.write_ack = {"status": "ok", "body": []}
        await self.bridge.prepare_schedule()
        self.assertEqual(self.api.home["schedules"][:2], original)
        dhw = self.api.home["schedules"][-1]
        self.assertEqual(dhw["type"], "event")
        self.assertTrue(all(zone["modules"][0]["dhw_enabled"] for zone in dhw["zones"]))
        self.assertEqual(dhw["timetable"], original[1]["timetable"])
        self.assertEqual(self.bridge.memory.managed_dhw_id, "managed_dhw")
        entries = [{"day": 0, "time": "00:00", "target": 22}]
        await self.bridge.set_schedule(entries)
        self.assertIsNone(self.bridge.fault)
        self.assertEqual({item["id"] for item in self.api.home["schedules"] if item.get("selected")}, {"managed", "managed_dhw"})
        for item in original:
            item["selected"] = False
        self.assertEqual(self.api.home["schedules"][:2], original)
        self.assertEqual(self.api.home["linked_schedules"], [{"therm": "original", "event": "dhw"}])
        await self.bridge.enable()
        self.assertEqual(self.bridge.memory.owner, "HA")
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        restored._api = lambda: self.api
        count = len(self.api.calls)
        await restored.enable()
        self.assertEqual(restored.memory.managed_dhw_id, "managed_dhw")
        self.assertEqual(restored.memory.native_dhw_schedule, canonical_dhw(dhw))
        self.assertEqual(len([call for call in self.api.calls[count:] if call[0] != "duration"]), 0)

    async def test_selection_requires_real_unique_readback_without_content_changes(self):
        await self.bridge.prepare_schedule()
        switch = self.api.switch_home_schedule

        async def ignored(home, schedule_id):
            return {"status": "ok", "body": []}

        self.api.switch_home_schedule = ignored
        with self.assertRaisesRegex(ValueError, "not confirmed uniquely"):
            await self.bridge.enable()
        self.assertEqual(self.bridge.memory.owner, "miGo")

        async def corrupt(home, schedule_id):
            response = await switch(home, schedule_id)
            self.api.home["schedules"][0]["name"] = "Unrelated edit"
            return response

        self.api.switch_home_schedule = corrupt
        with self.assertRaisesRegex(ValueError, "content/mode changed"):
            await self.bridge.enable()
        self.assertEqual(self.bridge.memory.owner, "miGo")

    async def test_pending_dhw_create_recovers_after_restart_without_duplicate(self):
        self.api.with_dhw()
        request = self.api._api_request

        async def interrupted(url):
            result = await request(url)
            if "schedule_type=event" in url:
                self.api.fail_read = True
            return result

        self.api._api_request = interrupted
        with self.assertRaisesRegex(ValueError, "read unavailable"):
            await self.bridge.prepare_schedule()
        self.assertTrue(self.bridge.memory.pending_dhw_create)
        restored = MigoBridge(self.controller)
        restored.load(self.bridge.serialize())
        self.api.fail_read = False
        restored._api = lambda: self.api
        count = len(self.api.calls)
        await restored.prepare_schedule()
        self.assertEqual(len(self.api.calls), count)
        self.assertEqual(restored.memory.managed_dhw_id, "managed_dhw")
        self.assertFalse(restored.memory.pending_dhw_create)

    async def test_dhw_creation_rejects_changed_native_source_before_retry(self):
        self.api.with_dhw()
        await self.bridge.prepare_schedule()
        # Simulate the persisted pre-write transaction after a rejected write.
        self.api.home["schedules"].pop()
        self.bridge.memory.managed_dhw_id = ""
        self.bridge.memory.native_dhw_schedule = {}
        self.api.http_error = True
        with self.assertRaisesRegex(ValueError, "acknowledge"):
            await self.bridge.prepare_schedule()
        self.api.http_error = False
        self.api.home["schedules"][0]["name"] = "External edit"
        count = len(self.api.calls)
        with self.assertRaisesRegex(ValueError, "changed before DHW creation"):
            await self.bridge.prepare_schedule()
        self.assertEqual(len(self.api.calls), count)

    async def test_dhw_external_edit_or_selection_relinquishes_without_writes(self):
        self.api.with_dhw()
        await self.ready()
        self.api.home["schedules"][-1]["selected"] = False
        count = len(self.api.calls)
        await self.reconcile()
        self.assertEqual(self.bridge.memory.owner, "miGo")
        self.assertEqual(len(self.api.calls), count)
        self.api.home["schedules"][-1]["zones"][0]["modules"][0]["dhw_enabled"] = False
        with self.assertRaisesRegex(ValueError, "DHW schedule changed externally"):
            await self.bridge.enable()
        self.assertEqual(len(self.api.calls), count)

    async def test_schedule_activation_preserves_away_and_user_manual_override(self):
        self.api.with_dhw()
        await self.bridge.prepare_schedule()
        self.api.home["therm_mode"] = "away"
        await self.bridge.set_schedule([{"day": 0, "time": "00:00", "target": 22}])
        self.assertIn("away/frost", self.bridge.fault)
        self.assertEqual({item["id"] for item in self.api.home["schedules"] if item.get("selected")}, {"original", "dhw"})
        self.api.home["therm_mode"] = "schedule"
        self.api.room.update(therm_setpoint_mode="manual", therm_setpoint_temperature=23, therm_setpoint_end_time=self.now + 3600)
        with self.assertRaisesRegex(Exception, "Cancel native override"):
            await self.bridge.enable()
        self.assertEqual({item["id"] for item in self.api.home["schedules"] if item.get("selected")}, {"original", "dhw"})

    async def test_dhw_schema_rejects_unknown_modules_links_and_solar_slots(self):
        self.api.with_dhw()
        home = deepcopy(self.api.home)
        for mutate in (lambda h: h.update(linked_schedules=[]),
                       lambda h: h["schedules"][1]["zones"][0]["modules"][0].update(unknown=True),
                       lambda h: h["schedules"][1]["zones"][0]["modules"][0].update(id="different"),
                       lambda h: h["schedules"][1]["zones"][0]["modules"][0].update(dhw_enabled=1),
                       lambda h: h["schedules"][1].update(timetable_sunrise=[{"zone_id": 0}])):
            changed = deepcopy(home)
            mutate(changed)
            with self.assertRaises(ValueError):
                build_dhw(changed, "original", "HA")
        self.assertEqual(home, self.api.home)

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

    async def test_installed_runtime_temporarily_unavailable_keeps_ha_selected(self):
        await self.ready()
        await self.reconcile()
        self.hass.entity_registry.add("climate.migo", [])
        self.hass.entity_registry.async_get("climate.migo").config_entry_id = "native"
        entry = SimpleNamespace(domain="migo_netatmo", runtime_data=None)
        self.hass.config_entries = SimpleNamespace(async_get_entry=lambda _: entry)
        bridge = MigoBridge(self.controller)
        bridge.load(self.bridge.serialize())
        writes = deepcopy(self.api.calls)
        await bridge.reconcile()
        self.assertEqual(bridge.memory.owner, "HA")
        self.assertTrue(bridge.snapshot["connection_retrying"])
        self.now += 60
        self.fresh()
        entry.runtime_data = SimpleNamespace(api=self.api)
        await bridge.reconcile()
        self.assertEqual(bridge.memory.owner, "HA")
        self.assertFalse(bridge.snapshot["connection_retrying"])
        self.assertEqual(self.api.calls, writes)

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
        await self.bridge.configure({"correction_seconds": 180})
        await self.ready()
        self.api.status_delay = 30
        self.api.advance_clock = lambda seconds: setattr(self, "now", self.now + seconds)
        await self.reconcile()
        confirmed_at = self.bridge.memory.last_command_at
        self.now = self.api.command_times[-1] + 179
        await self.reconcile()
        self.assertEqual(len(self.commands()), 1)
        self.now = confirmed_at + 180
        await self.reconcile()
        self.assertEqual(len(self.commands()), 2)
        self.assertGreaterEqual(self.api.command_times[1] - self.api.command_times[0], 180)

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
