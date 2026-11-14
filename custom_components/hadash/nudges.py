"""Home moments, the proactive wall: what the tablets share, and "When everyone has left".

The tablets notice a moment themselves (renderer lib/nudgeRules.ts): Leaving home, Good night, Weather change. Here
Home Assistant keeps what they share, so a moment shows on one tablet only and a family's answers are kept:

1. claim: the first tablet to claim a moment's key shows it; the others do not (for 12 hours).
2. missed: nobody answered the knock; the moment may come back in 20 minutes.
3. answer: yes, later, ignored, or "stop these". Three times ignored in a row: that moment rests for 7 days.
   "Stop these" turns it off until it is turned on again in the Studio.

"When everyone has left" (off by default): when every person has been away for 10 minutes, Home Assistant does
what the family chose (lights off, doors locked, garage closed) and tells their phones in one short message.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION
from .reminders import phone_services

_LOGGER = logging.getLogger(__name__)

NUDGES = "hadash_nudges"
STORAGE_KEY = "hadash.nudges"
EVENT_AWAY = "hadash_everyone_left"
KINDS = ("leave", "night", "weather")
CLAIM_S = 12 * 3600
MISSED_S = 20 * 60
REST_S = 7 * 86400
AWAY_DELAY = timedelta(minutes=10)
START_GRACE_S = 15 * 60
NEAR_S = 120
NEAR_GARAGE = re.compile(r"garage|driveway|drive_way|front_?yard|einfahrt|garaje|entrada|allee", re.I)
SEES = ("motion", "occupancy", "presence", "moving")

AWAY_SCHEMA = vol.Schema({vol.Required("on"): bool, vol.Optional("lights", default=True): bool, vol.Optional("lock", default=False): bool, vol.Optional("garage", default=False): bool})

WORDS = {
    # head, subject, lights, locked, closed, "and", garage kept open
    "en": ("Everyone left.", "I ", "turned off {n} lights", "locked {names}", "closed {names}", "and", "{names} stays open, because someone was near it."),
    "fr": ("Tout le monde est parti.", "J'ai ", "éteint {n} lumières", "verrouillé {names}", "fermé {names}", "et", "{names} reste ouvert, car quelqu'un était à côté."),
    "de": ("Alle sind weg.", "Ich habe ", "{n} Lichter ausgeschaltet", "{names} abgeschlossen", "{names} geschlossen", "und", "{names} bleibt offen, weil jemand in der Nähe war."),
    "es": ("Todos salieron.", "", "apagué {n} luces", "cerré con llave {names}", "cerré {names}", "y", "{names} sigue abierto, porque había alguien cerca."),
}


def _name(hass: HomeAssistant, entity_id: str) -> str:
    s = hass.states.get(entity_id)
    return str(s.attributes.get("friendly_name") or entity_id.split(".")[1].replace("_", " ")) if s else entity_id


class Nudges:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.off: dict[str, float] = {}
        self.ignored: dict[str, int] = {}
        self.claimed: dict[str, float] = {}
        self.away: dict[str, Any] = {"on": False, "lights": True, "lock": False, "garage": False}
        self._timer: Any = None
        self._unsubs: list[Any] = []
        self._listeners: list[Any] = []
        self._started = time.time()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.off = dict(data.get("off", {}))
        self.ignored = dict(data.get("ignored", {}))
        self.away = {**self.away, **data.get("away", {})}

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"off": self.off, "ignored": self.ignored, "away": self.away}, 2)
        for listener in list(self._listeners):
            listener(self.state())

    def state(self) -> dict[str, Any]:
        now = time.time()
        return {"off": [k for k, until in self.off.items() if until == 0 or until > now], "away": self.away}

    def claim(self, key: str) -> bool:
        now = time.time()
        self.claimed = {k: t for k, t in self.claimed.items() if now - t < CLAIM_S}
        if key in self.claimed:
            return False
        self.claimed[key] = now
        return True

    def missed(self, key: str) -> None:
        """Nobody answered the knock (on the phone, busy): not "ignored". It may come back in 20 minutes."""
        self.claimed[key] = time.time() - CLAIM_S + MISSED_S

    def answer(self, kind: str, how: str) -> None:
        if how == "stop":
            self.off[kind] = 0
            self.ignored.pop(kind, None)
        elif how == "ignored":
            self.ignored[kind] = self.ignored.get(kind, 0) + 1
            if self.ignored[kind] >= 3:
                self.off[kind] = time.time() + REST_S
                self.ignored[kind] = 0
        else:
            self.ignored.pop(kind, None)
        self._save()

    def turn_on(self, kinds: list[str]) -> None:
        """The Studio turned these on again: "stop these" and rests are forgotten."""
        for k in kinds:
            self.off.pop(k, None)
            self.ignored.pop(k, None)
        self._save()

    def set_away(self, settings: dict[str, Any]) -> None:
        self.away = settings
        self._save()
        self._check()

    # When everyone has left.

    def start(self) -> None:
        @callback
        def changed(event: Event) -> None:
            if event.data["entity_id"].startswith("person."):
                self._check()

        @callback
        def people_only(data: Any) -> bool:
            return str(data["entity_id"]).startswith("person.")

        # Every person, also one added later.
        self._unsubs.append(self.hass.bus.async_listen("state_changed", changed, event_filter=people_only))

        @callback
        def stop(_event: Event) -> None:
            self.stop()

        self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))

    def stop(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None
        for u in self._unsubs:
            u()
        self._unsubs = []

    def _near_garage(self, garages: list[str]) -> bool:
        """A camera or motion sensor near a garage saw someone in the last 2 minutes: the garage is never closed then."""
        reg = er.async_get(self.hass)

        def area(entity_id: str) -> str | None:
            entry = reg.async_get(entity_id)
            return entry.area_id if entry else None

        areas = {a for a in (area(g) for g in garages) if a}
        now = time.time()
        for s in self.hass.states.async_all("binary_sensor"):
            if s.attributes.get("device_class") not in SEES and not re.search(r"person|people|human", s.entity_id):
                continue
            near = area(s.entity_id) in areas or NEAR_GARAGE.search(s.entity_id) or NEAR_GARAGE.search(str(s.attributes.get("friendly_name", "")))
            if near and (s.state == "on" or (s.state == "off" and now - s.last_changed.timestamp() < NEAR_S)):
                return True
        return False

    def _everyone_away(self) -> bool:
        people = self.hass.states.async_all("person")
        # After a power cut, states are "unknown" for a while: that is never "away".
        return bool(people) and all(p.state not in ("home", "unknown", "unavailable") for p in people)

    @callback
    def _check(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None
        if self.away.get("on") and self._everyone_away():
            self._timer = async_call_later(self.hass, AWAY_DELAY, self._left)

    async def _left(self, _now: Any) -> None:
        self._timer = None
        if not self.away.get("on") or not self._everyone_away():
            return
        # Right after Home Assistant starts (often after a power cut), devices may still report wrong states.
        if time.time() - self._started < START_GRACE_S:
            self._timer = async_call_later(self.hass, timedelta(seconds=START_GRACE_S - (time.time() - self._started) + 5), self._left)
            return
        lights = [s.entity_id for s in self.hass.states.async_all("light") if s.state == "on"] if self.away.get("lights") else []
        locks = [s.entity_id for s in self.hass.states.async_all("lock") if s.state == "unlocked"] if self.away.get("lock") else []
        garages = (
            [s.entity_id for s in self.hass.states.async_all("cover") if s.attributes.get("device_class") in ("garage", "gate") and s.state in ("open", "opening")]
            if self.away.get("garage")
            else []
        )
        kept: list[str] = []
        if garages and self._near_garage(garages):
            kept, garages = garages, []
        if not (lights or locks or garages or kept):
            return
        for domain, service, ids in (("light", "turn_off", lights), ("lock", "lock", locks), ("cover", "close_cover", garages)):
            if ids:
                try:
                    await self.hass.services.async_call(domain, service, {"entity_id": ids}, blocking=True)
                except Exception:  # noqa: BLE001 - one that fails must not stop the others
                    _LOGGER.warning("When everyone has left: %s.%s failed", domain, service)
        self.hass.bus.async_fire(EVENT_AWAY, {"lights": lights, "locks": locks, "garages": garages, "kept_open": kept})
        text = self.message(lights, locks, garages, kept)
        services: list[str] = []
        for p in self.hass.states.async_all("person"):
            for s in phone_services(self.hass, p.entity_id):
                if s not in services:
                    services.append(s)
        for s in services:
            try:
                await self.hass.services.async_call("notify", s, {"message": text}, blocking=True)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("When everyone has left: could not tell %s", s)

    def message(self, lights: list[str], locks: list[str], garages: list[str], kept: list[str] | None = None) -> str:
        lang = (self.hass.config.language or "en")[:2]
        head, subject, w_lights, w_locked, w_closed, w_and, w_kept = WORDS.get(lang, WORDS["en"])
        tail = f" {w_kept.format(names=', '.join(_name(self.hass, x) for x in kept))}" if kept else ""
        parts = []
        if lights:
            parts.append(w_lights.format(n=len(lights)))
        if locks:
            parts.append(w_locked.format(names=", ".join(_name(self.hass, x) for x in locks)))
        if garages:
            parts.append(w_closed.format(names=", ".join(_name(self.hass, x) for x in garages)))
        if not parts:
            return f"{head}{tail}"
        body = subject + (parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} {w_and} {parts[-1]}")
        return f"{head} {body[0].upper()}{body[1:]}.{tail}"


async def async_setup(hass: HomeAssistant) -> None:
    nudges = Nudges(hass)
    await nudges.async_load()
    hass.data[NUDGES] = nudges
    nudges.start()
    for command in (ws_claim, ws_missed, ws_answer, ws_subscribe, ws_turn_on, ws_away):
        websocket_api.async_register_command(hass, command)


# Like the layout, these work for non-admin logins: wall tablets often use a plain user. Only the Studio's
# "When everyone has left" needs an admin, because it lets Home Assistant act alone.


@websocket_api.websocket_command({vol.Required("type"): "hadash/nudges/claim", vol.Required("key"): vol.All(str, vol.Length(min=1, max=300))})
@callback
def ws_claim(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"ok": hass.data[NUDGES].claim(msg["key"])})


@websocket_api.websocket_command({vol.Required("type"): "hadash/nudges/missed", vol.Required("key"): vol.All(str, vol.Length(min=1, max=300))})
@callback
def ws_missed(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[NUDGES].missed(msg["key"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/nudges/answer", vol.Required("kind"): vol.In(KINDS), vol.Required("how"): vol.In(["yes", "later", "ignored", "stop"])}
)
@callback
def ws_answer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[NUDGES].answer(msg["kind"], msg["how"])
    connection.send_result(msg["id"], hass.data[NUDGES].state())


@websocket_api.websocket_command({vol.Required("type"): "hadash/nudges/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    nudges: Nudges = hass.data[NUDGES]

    @callback
    def forward(state: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], state))

    nudges._listeners.append(forward)

    @callback
    def unsub() -> None:
        if forward in nudges._listeners:
            nudges._listeners.remove(forward)

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    forward(nudges.state())


@websocket_api.websocket_command({vol.Required("type"): "hadash/nudges/turn_on", vol.Required("kinds"): [vol.In(KINDS)]})
@callback
def ws_turn_on(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[NUDGES].turn_on(msg["kinds"])
    connection.send_result(msg["id"], hass.data[NUDGES].state())


@websocket_api.websocket_command({vol.Required("type"): "hadash/nudges/away", vol.Required("settings"): AWAY_SCHEMA})
@websocket_api.require_admin
@callback
def ws_away(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[NUDGES].set_away(msg["settings"])
    connection.send_result(msg["id"], hass.data[NUDGES].state())
