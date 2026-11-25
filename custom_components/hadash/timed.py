"""On for a while (free): turn a heater, a pump or a fan on for some minutes, and Home Assistant turns it off.

A tablet holds a device card and picks 15 min, 30 min, 1 h or 2 h; voice works too ("turn on the towel heater for
20 minutes"), and automations use the action hadash.turn_on_for. The time is kept here, so the device goes off
even when every tablet sleeps, and after a restart of Home Assistant. Turning it off by hand ends the time;
starting again sets a new time.

One running time: {entity_id: {"until": ISO time, "minutes": n, "started": ISO time}}.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

TIMED = "hadash_timed"
TIMED_STORAGE_KEY = "hadash.timed"
DOMAINS = ("switch", "input_boolean", "fan", "light", "valve", "humidifier")
MAX_MINUTES = 12 * 60
# States that mean the device is off again.
OFF = ("off", "closed", "closing")

ENTITY = vol.All(cv.entity_id, vol.Match(rf"^({'|'.join(DOMAINS)})\."))
MINUTES = vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_MINUTES))


def _service(entity_id: str, on: bool) -> tuple[str, str]:
    domain = entity_id.split(".")[0]
    if domain == "valve":
        return "valve", "open_valve" if on else "close_valve"
    return domain, "turn_on" if on else "turn_off"


class TimedStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, TIMED_STORAGE_KEY)
        self.items: dict[str, dict[str, Any]] = {}
        self._watch: dict[str, Callable[[], None]] = {}
        self._listeners: set[Callable[[dict[str, dict[str, Any]]], None]] = set()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.items = {k: v for k, v in (data.get("timed") or {}).items() if isinstance(v, dict) and v.get("until")}
        for entity_id in list(self.items):
            self._arm(entity_id)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop)

    @callback
    def _stop(self, _event: Event | None = None) -> None:
        for unsub in self._watch.values():
            unsub()
        self._watch.clear()

    def _changed(self) -> None:
        self._store.async_delay_save(lambda: {"timed": self.items}, 1)
        for listener in list(self._listeners):
            listener(self.items)

    @callback
    def async_listen(self, cb: Callable[[dict[str, dict[str, Any]]], None]) -> Callable[[], None]:
        self._listeners.add(cb)
        return lambda: self._listeners.discard(cb)

    async def start(self, entity_id: str, minutes: int) -> None:
        """Turns the device on (when it is off) and keeps the time when it goes off."""
        now = dt_util.utcnow()
        self._forget(entity_id)
        self.items[entity_id] = {"until": (now + timedelta(minutes=minutes)).isoformat(), "minutes": minutes, "started": now.isoformat()}
        state = self.hass.states.get(entity_id)
        if state is None or state.state in OFF:
            domain, service = _service(entity_id, True)
            await self.hass.services.async_call(domain, service, {"entity_id": entity_id}, blocking=True)
        # Armed after the device is on, so its own "on" does not end the time.
        self._arm(entity_id)
        self._changed()

    def cancel(self, entity_id: str) -> bool:
        """Ends the time and leaves the device as it is ("Keep on")."""
        if entity_id not in self.items:
            return False
        self._forget(entity_id)
        self._changed()
        return True

    def _forget(self, entity_id: str) -> None:
        if unsub := self._watch.pop(entity_id, None):
            unsub()
        self.items.pop(entity_id, None)

    def _arm(self, entity_id: str) -> None:
        item = self.items.get(entity_id)
        until = dt_util.parse_datetime(item["until"]) if item else None
        if until is None:
            self.items.pop(entity_id, None)
            return
        if until <= dt_util.utcnow():
            # The time ended while Home Assistant was off.
            self.hass.async_create_task(self._end(entity_id))
            return

        @callback
        def at_time(_now: datetime) -> None:
            self.hass.async_create_task(self._end(entity_id))

        @callback
        def changed(event: Event) -> None:
            new = event.data.get("new_state")
            if new is not None and new.state in OFF:
                # Turned off by hand, or by something else: the time is over.
                self._forget(entity_id)
                self._changed()

        unsubs = [async_track_point_in_utc_time(self.hass, at_time, until), async_track_state_change_event(self.hass, [entity_id], changed)]

        def stop() -> None:
            for unsub in unsubs:
                unsub()

        self._watch[entity_id] = stop

    async def _end(self, entity_id: str) -> None:
        if entity_id not in self.items:
            return
        self._forget(entity_id)
        self._changed()
        domain, service = _service(entity_id, False)
        try:
            await self.hass.services.async_call(domain, service, {"entity_id": entity_id}, blocking=True)
        except Exception:  # noqa: BLE001 - a device that cannot be reached must not stop the others
            _LOGGER.warning("On for a while: could not turn off %s", entity_id, exc_info=True)


async def async_setup(hass: HomeAssistant) -> None:
    store = TimedStore(hass)
    await store.async_load()
    hass.data[TIMED] = store
    for command in (ws_subscribe, ws_start, ws_cancel):
        websocket_api.async_register_command(hass, command)

    async def turn_on_for(call: ServiceCall) -> None:
        for entity_id in call.data["entity_id"]:
            await store.start(entity_id, call.data["minutes"])

    hass.services.async_register(
        DOMAIN, "turn_on_for", turn_on_for, schema=vol.Schema({vol.Required("entity_id"): vol.All(cv.ensure_list, [ENTITY]), vol.Required("minutes"): MINUTES})
    )


# Like the reminders, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/timed/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Every running time now, and again after each change."""
    store: TimedStore = hass.data[TIMED]

    @callback
    def forward(items: dict[str, dict[str, Any]]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"timed": items}))

    connection.subscriptions[msg["id"]] = store.async_listen(forward)
    connection.send_result(msg["id"])
    forward(store.items)


@websocket_api.websocket_command({vol.Required("type"): "hadash/timed/start", vol.Required("entity_id"): ENTITY, vol.Required("minutes"): MINUTES})
@websocket_api.async_response
async def ws_start(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    try:
        await hass.data[TIMED].start(msg["entity_id"], msg["minutes"])
    except Exception as err:  # noqa: BLE001 - the tablet shows why
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {"timed": hass.data[TIMED].items})


@websocket_api.websocket_command({vol.Required("type"): "hadash/timed/cancel", vol.Required("entity_id"): cv.entity_id})
@callback
def ws_cancel(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"ok": hass.data[TIMED].cancel(msg["entity_id"])})
