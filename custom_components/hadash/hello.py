"""Morning hello (Pro): who was greeted today, so the next tablet down the hall does not greet them again.

A tablet asks before it greets: {"person": "person.radu"} for someone it knows, or "" when it does not know who it
is. A known person is greeted once a day; "anyone" is greeted only when nobody was greeted yet today.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import PRO

HELLO = "hadash_hello"


class Greeted:
    def __init__(self) -> None:
        self.day = ""
        self.people: set[str] = set()

    def claim(self, person: str) -> bool:
        today = dt_util.now().date().isoformat()
        if today != self.day:
            self.day = today
            self.people = set()
        if person:
            if person in self.people:
                return False
        elif self.people:
            return False
        self.people.add(person or "anyone")
        return True


@callback
def async_register(hass: HomeAssistant) -> None:
    hass.data[HELLO] = Greeted()
    websocket_api.async_register_command(hass, ws_claim)


@websocket_api.websocket_command({vol.Required("type"): "hadash/hello/claim", vol.Optional("person", default=""): str})
@callback
def ws_claim(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Works for non-admin logins, like the layout: wall tablets often use a plain user."""
    if hass.data[PRO].state()["plan"] == "free":
        connection.send_result(msg["id"], {"ok": False})
        return
    connection.send_result(msg["id"], {"ok": hass.data[HELLO].claim(msg["person"][:80])})
