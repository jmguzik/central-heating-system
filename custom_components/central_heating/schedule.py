"""Documented Netatmo Energy schedule protocol and conservative round trips.

Reference: https://dev.netatmo.com/apidocumentation/energy#synchomeschedule
Only a dedicated HA-created thermal schedule is edited. Native schedules,
away/frost values and DHW/event schedules are not adopted or overwritten.
"""

from copy import deepcopy
import json
from urllib.parse import urlencode

from .comfort import validate_schedule
from .logic import temperature

API_BASE = "https://api.netatmo.com/api"


def checked(response):
    if not isinstance(response, dict) or response.get("error") or response.get("status") != "ok" or response.get("body", {}).get("errors"):
        raise ValueError("Native API did not acknowledge request")
    return response


async def request_schedule(api, operation, payload):
    if operation not in ("createnewhomeschedule", "synchomeschedule"):
        raise ValueError("Unsupported schedule operation")
    if not callable(getattr(api, "_api_request", None)):
        raise ValueError("Installed miGo transport does not support schedule bridge")
    # OpenAPI declares query parameters. JSON arrays, not Python repr, are
    # encoded as single parameter values; credentials stay with its transport.
    values = {key: json.dumps(value, separators=(",", ":")) if isinstance(value, list) else value for key, value in payload.items()}
    return checked(await api._api_request(f"{API_BASE}/{operation}?{urlencode(values)}"))


def canonical(schedule):
    return {key: deepcopy(schedule[key]) for key in ("name", "zones", "timetable", "hg_temp", "away_temp") if key in schedule}


def unrelated_schedules(home, managed_id):
    return {item["id"]: deepcopy(item) for item in home.get("schedules", []) if item.get("id") != managed_id}


def validate_native(schedule, home, room_id):
    if schedule.get("type") != "therm":
        raise ValueError("Only thermal schedules are supported")
    zones = schedule.get("zones", [])
    timetable = schedule.get("timetable", [])
    if not zones or not timetable:
        raise ValueError("Native schedule has no zones or timetable")
    if not {0, 1, 5}.issubset({zone.get("type") for zone in zones}):
        raise ValueError("Native Comfort, Night and Eco zones are required")
    # Currently this installation has one miGo thermostat. Do not guess how
    # other room schedules, DHW linked modules or complex zones should merge.
    if {room.get("id") for room in home.get("rooms", [])} != {room_id}:
        raise ValueError("Schedule bridge currently requires a single miGo room")
    for zone in zones:
        if set(zone) - {"id", "type", "name", "rooms"}:
            raise ValueError("Complex/DHW zone unsupported; native schedule untouched")
        if {room.get("id") for room in zone.get("rooms", [])} != {room_id}:
            raise ValueError("Native zones must contain the configured room only")
        if temperature(zone["rooms"][0].get("therm_setpoint_temperature")) is None:
            raise ValueError("Invalid native room target")
        if not isinstance(zone.get("id"), int) or isinstance(zone.get("id"), bool):
            raise ValueError("Invalid native zone id")
    if len({zone["id"] for zone in zones}) != len(zones):
        raise ValueError("Duplicate native zone id")
    offsets = []
    for slot in timetable:
        offset = slot.get("m_offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset < 10080 or slot.get("zone_id") not in {zone["id"] for zone in zones}:
            raise ValueError("Invalid native timetable")
        offsets.append(offset)
    if len(set(offsets)) != len(offsets):
        raise ValueError("Duplicate native timetable transition")
    for key in ("hg_temp", "away_temp"):
        value = temperature(schedule.get(key))
        if value is None or not 5 <= value <= 30:
            raise ValueError(f"Native {key} unavailable; cannot preserve it safely")


def native_entries(schedule, room_id):
    zones = {zone["id"]: zone for zone in schedule["zones"]}
    result = []
    for slot in sorted(schedule["timetable"], key=lambda row: row["m_offset"]):
        offset = slot["m_offset"]
        target = next(room["therm_setpoint_temperature"] for room in zones[slot["zone_id"]]["rooms"] if room["id"] == room_id)
        result.append({"day": offset // 1440, "time": f"{offset % 1440 // 60:02}:{offset % 60:02}", "target": target})
    if result[0]["day"] != 0 or result[0]["time"] != "00:00":
        result.insert(0, {"day": 0, "time": "00:00", "target": result[-1]["target"]})
    return validate_schedule(result)


def build_native(entries, template, home, room_id):
    validate_native(template, home, room_id)
    entries = validate_schedule(entries)
    zones = deepcopy(template["zones"])
    # Preserve mandatory native zones exactly, including id vs type. Custom
    # zones contain the uncorrected desired target, never a biased actuator target.
    highest = max(zone["id"] for zone in zones)
    target_ids = {}
    timetable = []
    for row in entries:
        target = row["target"]
        if target not in target_ids:
            highest += 1
            target_ids[target] = highest
            zones.append({"id": highest, "type": 4, "name": f"HA {target:g}°C", "rooms": [{"id": room_id, "therm_setpoint_temperature": target}]})
        hour, minute = map(int, row["time"].split(":"))
        timetable.append({"zone_id": target_ids[target], "m_offset": row["day"] * 1440 + hour * 60 + minute})
    return {"zones": zones, "timetable": timetable, "hg_temp": template["hg_temp"], "away_temp": template["away_temp"]}
