"""Intercom between the wall tablets (Pro): "tell the kitchen dinner is ready", "dinner bell", "reply ...".

Each tablet subscribes with its id and the name of its place ("Kitchen"). Home Assistant passes a message on at
once to the tablets it is for and keeps nothing: the words, or a short recording of the sender's own voice
(8 kHz mu-law, base64, at most about 15 seconds), or the dinner bell. Every subscriber also gets the list of tablets
that are online, so the sender can say "tell the bedroom" and the reply can go back to the right tablet.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

INTERCOM = "hadash_intercom"
EVENT = "hadash_intercom"
# 15 seconds of 8 kHz mu-law is 120 kB, 160 kB in base64.
MAX_AUDIO = 170_000


class Intercom:
    def __init__(self) -> None:
        # Subscription key -> (tablet id, place name, send function).
        self.tablets: dict[int, tuple[str, str, Callable[[dict[str, Any]], None]]] = {}
        self._next = 0

    def online(self) -> list[dict[str, str]]:
        seen: dict[str, dict[str, str]] = {}
        for tablet, where, _ in self.tablets.values():
            seen[tablet] = {"tablet": tablet, "where": where}
        return list(seen.values())

    def join(self, tablet: str, where: str, send: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        key = self._next
        self._next += 1
        self.tablets[key] = (tablet, where, send)
        self._roster()

        @callback
        def leave() -> None:
            self.tablets.pop(key, None)
            self._roster()

        return leave

    def _roster(self) -> None:
        roster = {"kind": "tablets", "tablets": self.online()}
        for _, _, send in list(self.tablets.values()):
            send(roster)

    def send(self, message: dict[str, Any]) -> int:
        """Passes a message to the tablets it is for (all but the sender when "to" is empty). Returns how many."""
        to = set(message.get("to") or [])
        count = 0
        for tablet, _, send in list(self.tablets.values()):
            if tablet == message["from_tablet"] and not message.get("echo"):
                continue
            if to and tablet not in to:
                continue
            send({"kind": "message", "message": message})
            count += 1
        return count


MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("to", default=list): [vol.All(str, vol.Length(max=80))],
        vol.Required("from_tablet"): vol.All(str, vol.Length(max=80)),
        vol.Optional("from_where", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("from_name", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("text", default=""): vol.All(str, vol.Length(max=300)),
        vol.Optional("audio"): vol.Any(None, vol.All(str, vol.Length(max=MAX_AUDIO))),
        vol.Optional("bell", default=False): bool,
        vol.Optional("reply_to"): vol.Any(None, vol.All(str, vol.Length(max=64))),
    }
)


async def async_setup(hass: HomeAssistant) -> None:
    hass.data[INTERCOM] = Intercom()
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_send)


# Like the layout, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/intercom/subscribe",
        vol.Required("tablet"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Optional("where", default=""): vol.All(str, vol.Length(max=80)),
    }
)
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    intercom: Intercom = hass.data[INTERCOM]

    @callback
    def forward(data: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], data))

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = intercom.join(msg["tablet"], msg["where"], forward)


@websocket_api.websocket_command({vol.Required("type"): "hadash/intercom/send", vol.Required("message"): MESSAGE_SCHEMA})
@callback
def ws_send(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    message = {**msg["message"], "at": time.time()}
    if not message["text"] and not message.get("audio") and not message["bell"]:
        connection.send_error(msg["id"], "empty", "A message needs words, a recording or the bell.")
        return
    count = hass.data[INTERCOM].send(message)
    # Automations can follow along (without the recording).
    hass.bus.async_fire(EVENT, {k: v for k, v in message.items() if k != "audio"})
    connection.send_result(msg["id"], {"delivered": count})
