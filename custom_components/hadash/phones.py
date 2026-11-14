"""Find my phone (free): "where is my phone", "ring Angela's phone".

The tablet understands the sentence (renderer lib/phoneWords.ts) and asks here. For each phone of the person (their
device trackers from the Home Assistant app), this finds its battery, where it is (home, a zone, or away), and the
room when a room sensor knows it. Ringing sends a loud notification that plays even when the phone is on silent:
on Android, words spoken on the alarm stream at full volume; on an iPhone, a critical alert.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify


def phones_of(hass: HomeAssistant, person: str) -> list[dict[str, Any]]:
    """The phones of a person: name, notify service, iPhone or not, battery, where, room."""
    state = hass.states.get(person)
    if state is None:
        return []
    ents = er.async_get(hass)
    devs = dr.async_get(hass)
    out: list[dict[str, Any]] = []
    for tracker in state.attributes.get("device_trackers", []) or []:
        entry = ents.async_get(tracker)
        if entry is None or entry.platform != "mobile_app" or not entry.device_id:
            continue
        device = devs.async_get(entry.device_id)
        if device is None:
            continue
        name = device.name_by_user or device.name or ""
        service = f"mobile_app_{slugify(name)}"
        if not hass.services.has_service("notify", service) or any(p["service"] == service for p in out):
            continue
        battery = None
        room = None
        for e in er.async_entries_for_device(ents, device.id):
            if e.domain != "sensor":
                continue
            s = hass.states.get(e.entity_id)
            if s is None or s.state in ("unknown", "unavailable"):
                continue
            if battery is None and (e.device_class or e.original_device_class) == "battery" and "state" not in e.entity_id:
                try:
                    battery = round(float(s.state))
                except ValueError:
                    pass
        # A room sensor for this phone (Bermuda and others): sensor.<phone>_area or _room.
        slug = slugify(name)
        for s in hass.states.async_all("sensor"):
            if s.entity_id.startswith(f"sensor.{slug}_") and s.entity_id.endswith(("_area", "_room")) and s.state not in ("unknown", "unavailable", "not_home", ""):
                room = s.state
                break
        where = hass.states.get(tracker)
        out.append(
            {
                "name": name,
                "service": service,
                "ios": (device.manufacturer or "").lower() == "apple",
                "battery": battery,
                "where": where.state if where else None,
                "room": room,
            }
        )
    return out


async def ring(hass: HomeAssistant, phone: dict[str, Any], words: str) -> None:
    if phone["ios"]:
        data = {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}, "interruption-level": "critical"}}
        await hass.services.async_call("notify", phone["service"], {"title": words, "message": words, "data": data}, blocking=True)
    else:
        data = {"tts_text": words, "media_stream": "alarm_stream_max", "priority": "high", "ttl": 0}
        await hass.services.async_call("notify", phone["service"], {"message": "TTS", "data": data}, blocking=True)


async def async_setup(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_phones)
    websocket_api.async_register_command(hass, ws_ring)


# Like the layout, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/phones", vol.Required("person"): vol.Match(r"^person\.[a-z0-9_]+$")})
@callback
def ws_phones(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"phones": phones_of(hass, msg["person"])})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/phones/ring",
        vol.Required("person"): vol.Match(r"^person\.[a-z0-9_]+$"),
        vol.Optional("words", default="Here I am"): vol.All(str, vol.Length(min=1, max=120)),
    }
)
@websocket_api.async_response
async def ws_ring(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    phones = phones_of(hass, msg["person"])
    rung = []
    for phone in phones:
        try:
            await ring(hass, phone, msg["words"])
            rung.append(phone["name"])
        except Exception:  # noqa: BLE001 - one phone that fails must not stop the others
            continue
    connection.send_result(msg["id"], {"phones": phones, "rung": rung})
