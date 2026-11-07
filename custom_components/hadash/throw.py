"""Throw it to the TV (Pro): a card flicked up on a wall tablet opens full size on a TV.

The tablet asks for the TVs (hadash/throw/targets), then sends the card (hadash/throw). Home Assistant passes it to
the TV's page, which is listening already for Talk to the wall. A Fire TV set up with "Add a Fire TV" is first woken
and brought to the front over adb, the same way Send to screen does it; the card waits for its page to come back.
Sending None brings the card back: the TV closes it.
"""

from __future__ import annotations

import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import PRO, TVS
from .send import SEND, Sender
from .voice import VOICE, Voice

TEXT = {
    "pro": {
        "en": "Throw it to the TV is part of Dashboard Studio Pro.",
        "fr": "Lancer vers la télé fait partie de Dashboard Studio Pro.",
        "de": "Zum Fernseher werfen gehört zu Dashboard Studio Pro.",
        "es": "Lanzar a la tele es parte de Dashboard Studio Pro.",
    },
    "nowhere": {
        "en": "That TV is not on right now.",
        "fr": "Cette télé n'est pas allumée.",
        "de": "Dieser Fernseher ist gerade nicht an.",
        "es": "Esa tele no está encendida.",
    },
    "waking": {
        "en": "Waking {where}. The card shows in a moment.",
        "fr": "{where} se réveille. La carte s'affiche dans un instant.",
        "de": "{where} wird geweckt. Die Karte erscheint gleich.",
        "es": "Despertando {where}. La tarjeta aparece en un momento.",
    },
}


def text(hass: HomeAssistant, key: str, **args: str) -> str:
    lang = (hass.config.language or "en").split("-")[0].lower()
    return TEXT[key].get(lang, TEXT[key]["en"]).format(**args)


def targets(hass: HomeAssistant, here: str) -> list[dict[str, Any]]:
    """The TVs a tablet can throw to: every screen that is a TV, except the tablet's own."""
    sender: Sender = hass.data[SEND]
    return [{"id": s["id"], "name": s["name"], "on": s["on"]} for s in sender.screens() if s["tv"] and s["id"] != here]


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_targets)
    websocket_api.async_register_command(hass, ws_throw)


@websocket_api.websocket_command({vol.Required("type"): "hadash/throw/targets", vol.Optional("screen", default=""): str})
@callback
def ws_targets(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if hass.data[PRO].state()["plan"] == "free":
        connection.send_result(msg["id"], {"targets": []})
        return
    connection.send_result(msg["id"], {"targets": targets(hass, msg["screen"])})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/throw",
        vol.Required("screen"): str,
        vol.Optional("item"): vol.Any(None, dict),
        vol.Optional("from", default=""): str,
    }
)
@websocket_api.async_response
async def ws_throw(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if hass.data[PRO].state()["plan"] == "free":
        connection.send_result(msg["id"], {"ok": False, "say": text(hass, "pro")})
        return
    sender: Sender = hass.data[SEND]
    voice: Voice = hass.data[VOICE]
    screen = msg["screen"]
    name = next((s["name"] for s in sender.screens() if s["id"] == screen), screen)
    command = {"action": "throw", "item": msg.get("item"), "from": msg["from"][:60]}
    walls = [w for w in voice.walls.values() if w.screen == screen]
    hosts = [h for h, tv in hass.data[TVS].tvs.items() if tv.get("screen") == screen]
    # Bringing it back never wakes a TV: it only closes what is open there.
    if command["item"] is not None:
        for host in hosts:
            hass.async_create_background_task(sender._wake_tv(host), f"hadash wake {host}")
    if walls:
        answer = await sender._deliver_command(voice, walls, command)
        connection.send_result(msg["id"], {"ok": True, "say": answer or ""})
        return
    if hosts and command["item"] is not None:
        sender.pending[screen] = (command, time.monotonic())
        connection.send_result(msg["id"], {"ok": True, "say": text(hass, "waking", where=name)})
        return
    connection.send_result(msg["id"], {"ok": False, "say": text(hass, "nowhere")})
