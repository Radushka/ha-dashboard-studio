"""Wake-up alarms, set by voice on a wall tablet ("wake me up at 7", "an alarm for 6:30 on weekdays").

The tablet understands the sentence by itself (renderer lib/alarmWords.ts) and keeps the alarm here, so it is not
lost when the page reloads, and every screen of the house can list it and turn it off. The tablet that heard the
sentence is the one that rings; it reads this list and rings at the time by itself, even if Home Assistant is
away for a moment then.

One alarm: {id, time "07:00", days [0..6] (0 is Sunday; empty: one time only), date "2026-11-15" (one time only),
tablet, where (the screen's name, to show in the list), on, created}. Free, like the kitchen timer.
"""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_VERSION

ALARMS = "hadash_alarms"
ALARMS_STORAGE_KEY = "hadash.wake_alarms"
# More than any home needs; it stops a stuck tablet from filling the list.
MAX_ALARMS = 40

TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ALARM_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("time"): vol.All(str, vol.Match(TIME)),
        vol.Optional("days", default=list): [vol.All(int, vol.Range(min=0, max=6))],
        vol.Optional("date"): vol.Any(None, vol.All(str, vol.Match(DATE))),
        vol.Optional("tablet", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("where", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("on", default=True): bool,
    }
)


class AlarmStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, ALARMS_STORAGE_KEY)
        self.alarms: list[dict[str, Any]] = []
        self._listeners: set[Callable[[list[dict[str, Any]]], None]] = set()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.alarms = [a for a in data.get("alarms", []) if isinstance(a, dict) and a.get("id")]

    def _changed(self) -> None:
        self._store.async_delay_save(lambda: {"alarms": self.alarms}, 1)
        for listener in list(self._listeners):
            listener(self.alarms)

    def save(self, alarm: dict[str, Any]) -> bool:
        """Adds the alarm, or replaces the one with the same id. False when the list is full."""
        alarm = {**alarm, "days": sorted(set(alarm.get("days") or []))}
        if alarm["days"]:
            alarm["date"] = None
        for i, old in enumerate(self.alarms):
            if old["id"] == alarm["id"]:
                self.alarms[i] = {**alarm, "created": old.get("created")}
                self._changed()
                return True
        if len(self.alarms) >= MAX_ALARMS:
            return False
        self.alarms.append({**alarm, "created": dt_util.utcnow().isoformat()})
        self._changed()
        return True

    def remove(self, ids: list[str]) -> int:
        before = len(self.alarms)
        self.alarms = [a for a in self.alarms if a["id"] not in ids]
        if len(self.alarms) != before:
            self._changed()
        return before - len(self.alarms)

    @callback
    def async_listen(self, cb: Callable[[list[dict[str, Any]]], None]) -> Callable[[], None]:
        self._listeners.add(cb)
        return lambda: self._listeners.discard(cb)


async def async_setup(hass: HomeAssistant) -> None:
    store = AlarmStore(hass)
    await store.async_load()
    hass.data[ALARMS] = store
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_save)
    websocket_api.async_register_command(hass, ws_remove)
    websocket_api.async_register_command(hass, ws_subscribe)


# Like the layout, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/alarms"})
@callback
def ws_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"alarms": hass.data[ALARMS].alarms})


@websocket_api.websocket_command({vol.Required("type"): "hadash/alarms/save", vol.Required("alarm"): ALARM_SCHEMA})
@callback
def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    if not hass.data[ALARMS].save(msg["alarm"]):
        connection.send_error(msg["id"], "too_many", "There are too many alarms. Remove one first.")
        return
    connection.send_result(msg["id"], {"alarms": hass.data[ALARMS].alarms})


@websocket_api.websocket_command({vol.Required("type"): "hadash/alarms/remove", vol.Required("ids"): [str]})
@callback
def ws_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    removed = hass.data[ALARMS].remove(msg["ids"])
    connection.send_result(msg["id"], {"removed": removed, "alarms": hass.data[ALARMS].alarms})


@websocket_api.websocket_command({vol.Required("type"): "hadash/alarms/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The whole list now, and again after each change."""
    store: AlarmStore = hass.data[ALARMS]

    @callback
    def forward(alarms: list[dict[str, Any]]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"alarms": alarms}))

    connection.subscriptions[msg["id"]] = store.async_listen(forward)
    connection.send_result(msg["id"])
    forward(store.alarms)
