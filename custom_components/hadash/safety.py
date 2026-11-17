"""Emergency and Soon moments: the wall when something matters now.

Emergency (on by default, free): a smoke, carbon monoxide, gas or water leak sensor turns on. Home Assistant keeps
one shared emergency, so every tablet shows it at once, even when no tablet is awake: it tells every phone, and for
smoke, carbon monoxide and gas it turns the lights on, so people can see their way out. It never unlocks a door or
opens the garage. It ends on every tablet when someone touches "I see it" (or says it). When the sensor clears, the
tablets say so and stop the alarm.

Soon (the tablets notice it, renderer lib/nudgeRules.ts): something that matters within minutes, such as rain in a
few minutes with windows open, or the fridge door left open. A tablet asks here before each round: one tablet shows
it, again every 5 minutes, at most 3 times; then Home Assistant tells the phones once instead.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION
from .reminders import phone_services

_LOGGER = logging.getLogger(__name__)

SAFETY = "hadash_safety"
STORAGE_KEY = "hadash.safety"
EVENT_EMERGENCY = "hadash_emergency"
# Device class of the sensor: the kind of emergency.
KINDS = {"smoke": "smoke", "carbon_monoxide": "co", "gas": "gas", "moisture": "leak"}
# Lights on for these (seeing the way out); a water leak does not need them.
LIGHTS_FOR = ("smoke", "co", "gas")
# Right after Home Assistant starts, sensors come back from "unknown": only a real change counts then.
START_S = 120
SOON_EVERY_S = 5 * 60
SOON_ROUNDS = 3
SOON_REST_S = 3600

SETTINGS_SCHEMA = vol.Schema({vol.Required("on"): bool, vol.Optional("lights", default=True): bool})

WORDS = {
    # The phone message: kind -> "{what} in {where}." / "{what}.".
    "en": {"smoke": "Smoke", "co": "Carbon monoxide", "gas": "Gas", "leak": "Water leak", "in": "{what} in {where}.", "alone": "{what}."},
    "fr": {"smoke": "Fumée", "co": "Monoxyde de carbone", "gas": "Gaz", "leak": "Fuite d'eau", "in": "{what} : {where}.", "alone": "{what}."},
    "de": {"smoke": "Rauch", "co": "Kohlenmonoxid", "gas": "Gas", "leak": "Wasserleck", "in": "{what}: {where}.", "alone": "{what}."},
    "es": {"smoke": "Humo", "co": "Monóxido de carbono", "gas": "Gas", "leak": "Fuga de agua", "in": "{what} en {where}.", "alone": "{what}."},
}


class Safety:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.settings: dict[str, Any] = {"on": True, "lights": True}
        self.emergency: dict[str, Any] | None = None
        self.soons: dict[str, dict[str, float]] = {}
        self._listeners: list[Any] = []
        self._unsubs: list[Any] = []
        self._started = time.time()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.settings = {**self.settings, **data.get("settings", {})}

    def state(self) -> dict[str, Any]:
        return {"settings": self.settings, "emergency": self.emergency}

    def _tell(self) -> None:
        for listener in list(self._listeners):
            listener(self.state())

    def set_settings(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self._store.async_delay_save(lambda: {"settings": self.settings}, 2)
        self._tell()

    # Emergency.

    def start(self) -> None:
        @callback
        def sensors_only(data: Any) -> bool:
            new = data.get("new_state")
            return str(data["entity_id"]).startswith("binary_sensor.") and new is not None and new.attributes.get("device_class") in KINDS

        @callback
        def changed(event: Event) -> None:
            old, new = event.data.get("old_state"), event.data["new_state"]
            was = old.state if old else None
            if new.state == "on" and was != "on":
                if was in (None, "unknown", "unavailable") and time.time() - self._started < START_S:
                    return
                self.hass.async_create_task(self.raise_for(new.entity_id))
            elif new.state == "off" and self.emergency and self.emergency["entity_id"] == new.entity_id and not self.emergency.get("clear"):
                self.emergency = {**self.emergency, "clear": True}
                self._tell()

        self._unsubs.append(self.hass.bus.async_listen("state_changed", changed, event_filter=sensors_only))

        @callback
        def stop(_event: Event) -> None:
            for u in self._unsubs:
                u()
            self._unsubs = []

        # Kept out of _unsubs: a one-time listener removes itself when it runs, and removing it again logs an error.
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)

    def _where(self, entity_id: str) -> str:
        entry = er.async_get(self.hass).async_get(entity_id)
        area_id = entry.area_id if entry else None
        if entry and not area_id and entry.device_id:
            from homeassistant.helpers import device_registry as dr

            device = dr.async_get(self.hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return area.name if area else ""

    async def raise_for(self, entity_id: str) -> None:
        if not self.settings.get("on"):
            return
        s = self.hass.states.get(entity_id)
        if s is None:
            return
        kind = KINDS[s.attributes.get("device_class")]
        # The same sensor again while its emergency is on screen: nothing new.
        if self.emergency and self.emergency["entity_id"] == entity_id and not self.emergency.get("clear") and not self.emergency.get("seen"):
            return
        where = self._where(entity_id)
        name = str(s.attributes.get("friendly_name") or entity_id)
        self.emergency = {"id": f"{entity_id}:{int(time.time())}", "kind": kind, "entity_id": entity_id, "name": name, "where": where, "since": time.time()}
        self._tell()
        self.hass.bus.async_fire(EVENT_EMERGENCY, {"kind": kind, "entity_id": entity_id, "where": where})
        lit = False
        if kind in LIGHTS_FOR and self.settings.get("lights"):
            lights = [x.entity_id for x in self.hass.states.async_all("light") if x.state == "off"]
            if lights:
                try:
                    await self.hass.services.async_call("light", "turn_on", {"entity_id": lights, "brightness_pct": 100}, blocking=False)
                    lit = True
                except Exception:  # noqa: BLE001 - the alarm still goes out
                    _LOGGER.warning("Emergency: could not turn the lights on")
        told = await self._phones(self.text(kind, where), critical=True)
        # The tablets say what was done, only when it was.
        if self.emergency and self.emergency["entity_id"] == entity_id and (told or lit):
            self.emergency = {**self.emergency, "phones": told, "lights": lit}
            self._tell()

    def text(self, kind: str, where: str) -> str:
        lang = (self.hass.config.language or "en")[:2]
        w = WORDS.get(lang, WORDS["en"])
        return w["in"].format(what=w[kind], where=where) if where else w["alone"].format(what=w[kind])

    async def _phones(self, text: str, critical: bool = False) -> int:
        services: list[str] = []
        for p in self.hass.states.async_all("person"):
            for s in phone_services(self.hass, p.entity_id):
                if s not in services:
                    services.append(s)
        # Critical: it sounds through a phone's silent mode (iPhone critical alert, Android alarm channel).
        data = {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}, "ttl": 0, "priority": "high", "channel": "alarm_stream"} if critical else {}
        told = 0
        for s in services:
            try:
                await self.hass.services.async_call("notify", s, {"title": "Dashboard Studio", "message": text, **({"data": data} if data else {})}, blocking=True)
                told += 1
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Emergency: could not tell %s", s)
        return told

    def seen(self, emergency_id: str) -> None:
        """Someone touched "I see it": the alarm ends on every tablet."""
        if self.emergency and self.emergency["id"] == emergency_id:
            self.emergency = None
            self._tell()

    # Soon.

    def soon(self, key: str) -> str:
        """A tablet asks before a round of a Soon moment: "show", "wait", or "phones" (Home Assistant told them)."""
        now = time.time()
        self.soons = {k: v for k, v in self.soons.items() if now - v["last"] < 2 * 3600}
        s = self.soons.setdefault(key, {"rounds": 0, "last": 0.0, "rest": 0.0})
        if now < s["rest"] or now - s["last"] < SOON_EVERY_S:
            return "wait"
        s["last"] = now
        if s["rounds"] >= SOON_ROUNDS:
            s["rest"] = now + SOON_REST_S
            return "phones"
        s["rounds"] += 1
        return "show"

    def soon_answered(self, key: str) -> None:
        """Someone answered: the moment rests for an hour."""
        now = time.time()
        self.soons[key] = {"rounds": 0, "last": now, "rest": now + SOON_REST_S}


async def async_setup(hass: HomeAssistant) -> None:
    safety = Safety(hass)
    await safety.async_load()
    hass.data[SAFETY] = safety
    safety.start()
    for command in (ws_subscribe, ws_seen, ws_settings, ws_soon, ws_soon_answered):
        websocket_api.async_register_command(hass, command)


# Wall tablets often use a plain (non-admin) user: they may follow, answer and ask. Only the Studio's settings
# need an admin.


@websocket_api.websocket_command({vol.Required("type"): "hadash/safety/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    safety: Safety = hass.data[SAFETY]

    @callback
    def forward(state: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], state))

    safety._listeners.append(forward)

    @callback
    def unsub() -> None:
        if forward in safety._listeners:
            safety._listeners.remove(forward)

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    forward(safety.state())


@websocket_api.websocket_command({vol.Required("type"): "hadash/safety/seen", vol.Required("emergency"): str})
@callback
def ws_seen(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[SAFETY].seen(msg["emergency"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/safety/settings", vol.Required("settings"): SETTINGS_SCHEMA})
@websocket_api.require_admin
@callback
def ws_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[SAFETY].set_settings(msg["settings"])
    connection.send_result(msg["id"], hass.data[SAFETY].state())


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/safety/soon", vol.Required("key"): vol.All(str, vol.Length(min=1, max=200)), vol.Required("text"): vol.All(str, vol.Length(min=1, max=300))}
)
@websocket_api.async_response
async def ws_soon(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    safety: Safety = hass.data[SAFETY]
    what = safety.soon(msg["key"])
    if what == "phones":
        await safety._phones(msg["text"])
    connection.send_result(msg["id"], {"do": what})


@websocket_api.websocket_command({vol.Required("type"): "hadash/safety/soon_answered", vol.Required("key"): vol.All(str, vol.Length(min=1, max=200))})
@callback
def ws_soon_answered(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[SAFETY].soon_answered(msg["key"])
    connection.send_result(msg["id"])
