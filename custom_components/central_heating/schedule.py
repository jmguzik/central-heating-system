"""Documented Netatmo Energy schedule protocol and conservative round trips.

Reference: https://dev.netatmo.com/apidocumentation/energy#synchomeschedule
Only dedicated HA-created schedules are managed. Original schedules and native
away/frost values are never adopted or overwritten.
"""

from copy import deepcopy
import json
from urllib.parse import urlencode

from .comfort import validate_schedule
from .logic import temperature

API_BASE = "https://api.netatmo.com/api"


class NativeApiUnacknowledged(ValueError):
    """No successful API acknowledgement was received."""


def checked(response, operation=""):
    context = operation if operation in ("get_home_status", "get_homes_data", "set_manual_setpoint_duration", "set_room_state", "switch_home_schedule", "createnewhomeschedule", "synchomeschedule") else ""
    error = response.get("error") if isinstance(response, dict) else None
    body = response.get("body", {}) if isinstance(response, dict) else None
    if isinstance(body, dict):
        error = error or body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    # Never include raw cloud messages/payloads or credentials in diagnostics.
    details = [context] if context else []
    if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 999:
        details.append(f"error code {code}")
    message = "Native API did not acknowledge request" + (f" ({', '.join(details)})" if details else "")
    if not isinstance(response, dict) or response.get("error") or response.get("errors") or response.get("status") != "ok":
        raise NativeApiUnacknowledged(message)
    # miGo acknowledges some writes with an empty array, not an object.
    # Nonempty arrays/scalars are not a verified acknowledgement format.
    if body != [] and (not isinstance(body, dict) or body.get("error") or body.get("errors")):
        raise NativeApiUnacknowledged(message)
    return response


def checked_body(response, operation=""):
    body = checked(response, operation).get("body")
    if not isinstance(body, dict):
        raise ValueError("Native API returned an invalid read body")
    return body


async def request_schedule(api, operation, payload):
    if operation not in ("createnewhomeschedule", "synchomeschedule"):
        raise ValueError("Unsupported schedule operation")
    if not callable(getattr(api, "_api_request", None)):
        raise ValueError("Installed miGo transport does not support schedule bridge")
    # OpenAPI declares query parameters. JSON arrays, not Python repr, are
    # encoded as single parameter values; credentials stay with its transport.
    values = {key: json.dumps(value, separators=(",", ":")) if isinstance(value, list) else value for key, value in payload.items()}
    return checked(await api._api_request(f"{API_BASE}/{operation}?{urlencode(values)}"), operation)


def effective_entries(entries):
    """Remove no-op transitions without changing the Monday baseline or targets.

    miGo rejects consecutive identical zone IDs, even at different offsets.
    HA keeps the full editable draft, so read-back compares effective changes.
    """
    entries = validate_schedule(entries)
    return [row for index, row in enumerate(entries) if index == 0 or row["target"] != entries[index - 1]["target"]]


def _thermal_zones(zones):
    """Normalize only verified redundant fields returned by miGo homesdata."""
    if not isinstance(zones, list):
        raise ValueError("Invalid native zones; native schedule untouched")
    result = []
    for zone in zones:
        allowed = {"id", "type", "name", "rooms", "modules", "rooms_temp"}
        if not isinstance(zone, dict) or set(zone) - allowed or ("modules" in zone and zone["modules"] != []):
            raise ValueError("Complex/DHW zone unsupported; native schedule untouched")
        if "rooms_temp" in zone:
            rooms = zone.get("rooms")
            aliases = zone["rooms_temp"]
            if (not isinstance(rooms, list) or not isinstance(aliases, list)
                    or any(not isinstance(room, dict) for room in rooms)
                    or any(not isinstance(alias, dict) or temperature(alias.get("temp")) is None for alias in aliases)):
                raise ValueError("Native room temperature aliases mismatch; native schedule untouched")
            expected = [{"room_id": room.get("id"), "temp": room.get("therm_setpoint_temperature")} for room in rooms]
            if aliases != expected:
                raise ValueError("Native room temperature aliases mismatch; native schedule untouched")
        result.append({key: deepcopy(value) for key, value in zone.items() if key not in ("modules", "rooms_temp")})
    return result


def canonical(schedule):
    result = {key: deepcopy(schedule[key]) for key in ("name", "zones", "timetable", "hg_temp", "away_temp") if key in schedule}
    if "zones" in result:
        result["zones"] = _thermal_zones(result["zones"])
    return result


def unrelated_schedules(home, managed_id):
    return {item["id"]: deepcopy(item) for item in home.get("schedules", []) if item.get("id") != managed_id}


def dhw_gateway(home):
    modules = [item["id"] for item in home.get("modules", []) if item.get("type") == "NAVaillant"]
    if len(modules) > 1:
        raise ValueError("Multiple miGo DHW gateways unsupported")
    return modules[0] if modules else None


def canonical_dhw(schedule):
    if (not isinstance(schedule, dict) or not isinstance(schedule.get("name"), str)
            or not isinstance(schedule.get("zones"), list) or not isinstance(schedule.get("timetable"), list)):
        raise ValueError("Invalid DHW schedule; native schedule untouched")
    result = {key: deepcopy(schedule[key]) for key in ("name", "zones", "timetable")}
    for zone in result["zones"]:
        if not isinstance(zone, dict) or set(zone) - {"id", "name", "type", "rooms", "modules"} or zone.get("rooms", []) != []:
            raise ValueError("Complex DHW zone unsupported; native schedule untouched")
        zone.pop("rooms", None)  # miGo returns this redundant empty array.
    if schedule.get("timetable_sunrise", []) != [] or schedule.get("timetable_sunset", []) != []:
        raise ValueError("Solar DHW timetable unsupported; native schedule untouched")
    return result


def build_dhw(home, source_id, name):
    """Clone only the source's verified single-gateway DHW plan, available 24/7.

    Official Home web client maps type to schedule_type for both create/sync:
    https://home.netatmo.com/chunk-MDSWWFG7.js
    The installed miGo API reference also documents event zones/modules.
    No undocumented linked_schedules write or global DHW boost is used.
    """
    gateway = dhw_gateway(home)
    links = [link["event"] for link in home.get("linked_schedules", []) if link.get("therm") == source_id and link.get("event")]
    if gateway is None or len(links) != 1:
        raise ValueError("Native source has no unique linked DHW schedule")
    source = next((item for item in home.get("schedules", []) if item.get("id") == links[0] and item.get("type") == "event"), None)
    if source is None:
        raise ValueError("Native source DHW schedule missing")
    payload = canonical_dhw(source)
    if not payload["zones"] or not payload["timetable"]:
        raise ValueError("Empty DHW schedule unsupported")
    ids = set()
    for zone in payload["zones"]:
        if not isinstance(zone.get("id"), int) or isinstance(zone["id"], bool) or zone["id"] in ids:
            raise ValueError("Invalid DHW zone identity")
        ids.add(zone["id"])
        modules = zone.get("modules")
        if (not isinstance(modules, list) or len(modules) != 1 or not isinstance(modules[0], dict)
                or set(modules[0]) != {"id", "dhw_enabled"} or modules[0]["id"] != gateway
                or not isinstance(modules[0]["dhw_enabled"], bool)):
            raise ValueError("Complex DHW modules unsupported; native schedule untouched")
        modules[0]["dhw_enabled"] = True
    offsets = []
    for slot in payload["timetable"]:
        offset = slot.get("m_offset")
        if set(slot) != {"zone_id", "m_offset"} or slot["zone_id"] not in ids or not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset < 10080:
            raise ValueError("Invalid DHW timetable")
        offsets.append(offset)
    if len(set(offsets)) != len(offsets):
        raise ValueError("Duplicate DHW transition")
    payload["name"] = name
    return payload


def validate_native(schedule, home, room_id):
    if schedule.get("type") != "therm":
        raise ValueError("Only thermal schedules are supported")
    zones = _thermal_zones(schedule.get("zones", []))
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
    entries = effective_entries(entries)
    zones = _thermal_zones(template["zones"])
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
