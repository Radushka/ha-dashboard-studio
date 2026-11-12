"""Voice check: what the voice assistant (Assist) may use, made easy (free, admin only).

Home Assistant keeps one long flat list of switches for this (Settings > Voice assistants > Expose). Most people
never find it, so voice cannot reach half of their home. This looks at the whole home and answers three things:
1. Useful things that voice cannot use yet, by room. The safe ones are ticked already.
2. Sensitive things (locks, garage doors, door sensors, cameras, the alarm, where people are, calendars). They are
   never ticked for the person. Each one carries a short risk code; the Studio shows a one-line note for it.
3. Problems: things that are gone but still exposed, two things with the same name in the same room, and exposed
   things with no room. Things made in YAML without a unique_id cannot have a room; they are explained, not changed.

One Apply makes all the changes, and one Undo puts back exactly what was there before the last Apply. The undo
record is saved, so it still works after a restart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN

ASSISTANT = "conversation"
STORE_KEY = f"{DOMAIN}.voice_check"
DATA = f"{DOMAIN}_voice_check"

# Integrations whose things are never offered: the Studio's own tablets, and old browser screens.
SKIP_PLATFORMS = {DOMAIN, "browser_mod"}

# Sensors worth asking about ("how warm is the kitchen?"). The rest (power, battery, signal) is noise to voice.
SENSOR_CLASSES = {"temperature", "humidity", "carbon_dioxide", "aqi", "pm25"}
# Binary sensors that answer a useful question. Doors and windows tell anyone who asks what is open.
SAFE_BINARY = {"moisture", "smoke", "carbon_monoxide", "gas"}
OPENING_BINARY = {"door", "window", "garage_door", "opening"}
# Covers that let someone in. Shades and blinds are safe.
OPENING_COVERS = {"garage", "gate", "door"}

# Kind of thing for each domain; the Studio sorts and names them. The order is the order in a room.
KINDS = {
    "light": "light", "switch": "switch", "fan": "fan", "cover": "cover", "climate": "climate",
    "media_player": "media", "vacuum": "vacuum", "lawn_mower": "vacuum", "humidifier": "climate",
    "water_heater": "climate", "valve": "switch", "input_boolean": "switch", "scene": "scene", "script": "scene",
    "sensor": "sensor", "binary_sensor": "sensor", "todo": "list", "weather": "weather",
    "lock": "lock", "camera": "camera", "alarm_control_panel": "alarm", "person": "person", "calendar": "calendar",
}
# Always sensitive, with the risk code the Studio explains.
RISK = {"lock": "lock", "camera": "camera", "alarm_control_panel": "alarm", "person": "location", "calendar": "calendar"}
# Things that are about a room. Exposed with no room, voice cannot tell "the light" in one room from another.
ROOM_DOMAINS = {"light", "switch", "fan", "cover", "climate", "media_player", "vacuum", "humidifier", "valve",
                "sensor", "binary_sensor", "lock", "camera", "water_heater"}


def _risk(domain: str, device_class: str | None) -> str | None | bool:
    """The risk code of a useful thing, None when it is safe, False when it is not useful to voice."""
    if domain in RISK:
        return RISK[domain]
    if domain == "sensor":
        return None if device_class in SENSOR_CLASSES else False
    if domain == "binary_sensor":
        if device_class in OPENING_BINARY:
            return "door"
        return None if device_class in SAFE_BINARY else False
    if domain == "cover" and device_class in OPENING_COVERS:
        return "garage"
    return None if domain in KINDS else False


@callback
def _exposed_settings(hass: HomeAssistant) -> dict[str, bool] | None:
    """Every thing's saved choice. None when Home Assistant's expose settings are not loaded."""
    try:
        from homeassistant.components.homeassistant.exposed_entities import async_get_assistant_settings
        settings = async_get_assistant_settings(hass, ASSISTANT)
    except (ImportError, KeyError):
        return None
    return {eid: bool(s.get("should_expose")) for eid, s in settings.items() if "should_expose" in s}


@callback
def _should_expose(hass: HomeAssistant, entity_id: str) -> bool:
    from homeassistant.components.homeassistant.exposed_entities import async_should_expose
    return async_should_expose(hass, ASSISTANT, entity_id)


@callback
def _expose(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
    async_expose_entity(hass, ASSISTANT, entity_id, on)


def _gone(state: State | None) -> bool:
    """No longer there: no state at all, or a state Home Assistant only restored because no integration claims it."""
    return state is None or (state.state == STATE_UNAVAILABLE and bool(state.attributes.get("restored")))


@callback
def async_check(hass: HomeAssistant) -> dict[str, Any] | None:
    """The whole picture, for the Studio. None when expose settings are not loaded yet."""
    saved = _exposed_settings(hass)
    if saved is None:
        return None
    ents = er.async_get(hass)
    devs = dr.async_get(hass)
    areas = ar.async_get(hass)

    def room_of(entry: er.RegistryEntry | None) -> str | None:
        if not entry:
            return None
        if entry.area_id:
            return entry.area_id
        device = devs.async_get(entry.device_id) if entry.device_id else None
        return device.area_id if device else None

    def name_of(entity_id: str, state: State | None, entry: er.RegistryEntry | None) -> str:
        if state and state.name:
            return state.name
        if entry and (entry.name or entry.original_name):
            return entry.name or entry.original_name  # type: ignore[return-value]
        return entity_id.split(".", 1)[1].replace("_", " ").capitalize()

    items: list[dict[str, Any]] = []
    exposed: list[dict[str, Any]] = []
    gone: list[dict[str, Any]] = []
    count = 0

    seen: set[str] = set()
    for state in hass.states.async_all():
        entity_id = state.entity_id
        seen.add(entity_id)
        domain = entity_id.split(".", 1)[0]
        entry = ents.async_get(entity_id)
        if entry and (entry.platform in SKIP_PLATFORMS or entry.disabled_by):
            continue
        is_on = _should_expose(hass, entity_id) if domain in KINDS or entity_id in saved else False
        thing = {
            "id": entity_id,
            "name": name_of(entity_id, state, entry),
            "area": room_of(entry),
            "kind": KINDS.get(domain, "other"),
            "yaml": entry is None,
        }
        if is_on:
            count += 1
            if _gone(state):
                gone.append(thing)
            else:
                exposed.append({**thing, "domain": domain})
            continue
        if domain not in KINDS or _gone(state) or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        if entry and (entry.entity_category is not None or entry.hidden_by):
            continue
        risk = _risk(domain, state.attributes.get("device_class"))
        if risk is False:
            continue
        items.append({**thing, "risk": risk, "tick": risk is None})

    # Registry things without any state: their integration is gone. Still exposed, they only confuse voice.
    for entity_id, on in saved.items():
        if on and entity_id not in seen:
            entry = ents.async_get(entity_id)
            if entry and (entry.platform in SKIP_PLATFORMS or entry.disabled_by):
                continue
            if entry is None and entity_id.split(".", 1)[0] not in KINDS:
                continue
            count += 1
            gone.append({"id": entity_id, "name": name_of(entity_id, None, entry), "area": room_of(entry),
                         "kind": KINDS.get(entity_id.split(".", 1)[0], "other"), "yaml": entry is None})

    # Two exposed things with one name in one room: voice cannot know which one is meant.
    by_name: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for x in exposed:
        by_name.setdefault((x["name"].strip().casefold(), x["area"]), []).append(x)
    same = [[_short(x) for x in group] for group in by_name.values() if len(group) > 1]

    no_room = [_short(x) for x in exposed if x["area"] is None and x["domain"] in ROOM_DOMAINS]

    rooms = sorted(({"id": a.id, "name": a.name} for a in areas.async_list_areas()), key=lambda a: a["name"].casefold())
    undo = hass.data.get(DATA, {}).get("undo")
    return {
        "exposed": count,
        "rooms": rooms,
        "items": sorted(items, key=lambda x: x["name"].casefold()),
        "gone": sorted(gone, key=lambda x: x["name"].casefold()),
        "same": same,
        "noRoom": sorted(no_room, key=lambda x: x["name"].casefold()),
        "undo": {"at": undo["at"], "count": len(undo["expose"]) + len(undo["areas"])} if undo else None,
    }


def _short(x: dict[str, Any]) -> dict[str, Any]:
    return {k: x[k] for k in ("id", "name", "area", "kind", "yaml")}


async def _store(hass: HomeAssistant) -> Store:
    data = hass.data.setdefault(DATA, {})
    if "store" not in data:
        data["store"] = Store(hass, 1, STORE_KEY)
        saved = await data["store"].async_load() or {}
        data["undo"] = saved.get("undo")
    return data["store"]


async def async_apply(
    hass: HomeAssistant, expose: list[str], unexpose: list[str], areas: dict[str, str | None]
) -> None:
    """Makes the changes and keeps what was there before, for Undo."""
    store = await _store(hass)
    ents = er.async_get(hass)
    known_areas = {a.id for a in ar.async_get(hass).async_list_areas()}
    before: dict[str, bool] = {}
    for entity_id, on in [(e, True) for e in expose] + [(e, False) for e in unexpose]:
        if entity_id in before:
            continue
        before[entity_id] = _should_expose(hass, entity_id)
        if before[entity_id] != on:
            _expose(hass, entity_id, on)
    rooms_before: dict[str, str | None] = {}
    for entity_id, area_id in areas.items():
        entry = ents.async_get(entity_id)
        if entry is None or (area_id is not None and area_id not in known_areas):
            continue  # made in YAML without an ID, or a room that was removed meanwhile
        rooms_before[entity_id] = entry.area_id
        if entry.area_id != area_id:
            ents.async_update_entity(entity_id, area_id=area_id)
    undo = {"at": datetime.now(timezone.utc).isoformat(), "expose": before, "areas": rooms_before}
    hass.data[DATA]["undo"] = undo
    await store.async_save({"undo": undo})


async def async_undo(hass: HomeAssistant) -> bool:
    """Puts back what was there before the last Apply. False when there is nothing to undo."""
    store = await _store(hass)
    undo = hass.data[DATA].get("undo")
    if not undo:
        return False
    ents = er.async_get(hass)
    known_areas = {a.id for a in ar.async_get(hass).async_list_areas()}
    for entity_id, on in undo["expose"].items():
        _expose(hass, entity_id, on)
    for entity_id, area_id in undo["areas"].items():
        if ents.async_get(entity_id) and (area_id is None or area_id in known_areas):
            ents.async_update_entity(entity_id, area_id=area_id)
    hass.data[DATA]["undo"] = None
    await store.async_save({"undo": None})
    return True


def _not_ready(connection: websocket_api.ActiveConnection, msg_id: int) -> None:
    connection.send_error(msg_id, "not_ready", "Home Assistant's voice settings are not loaded yet.")


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/voice_check"})
@websocket_api.async_response
async def ws_voice_check(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await _store(hass)
    if (result := async_check(hass)) is None:
        return _not_ready(connection, msg["id"])
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/voice_check/apply",
        vol.Optional("expose", default=list): [str],
        vol.Optional("unexpose", default=list): [str],
        vol.Optional("areas", default=dict): {str: vol.Any(str, None)},
    }
)
@websocket_api.async_response
async def ws_voice_check_apply(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if _exposed_settings(hass) is None:
        return _not_ready(connection, msg["id"])
    await async_apply(hass, msg["expose"], msg["unexpose"], msg["areas"])
    connection.send_result(msg["id"], async_check(hass))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/voice_check/undo"})
@websocket_api.async_response
async def ws_voice_check_undo(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if _exposed_settings(hass) is None:
        return _not_ready(connection, msg["id"])
    await async_undo(hass)
    connection.send_result(msg["id"], async_check(hass))
