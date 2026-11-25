"""Reminders, set by voice on a wall tablet (free).

"Remind me at 6 to call Mom", "remind me when I get home to take out the trash", "remind me when Angela gets home
to give her the keys", "remind me when the dishwasher is done to empty it". The tablet understands the sentence
(renderer lib/reminderWords.ts) and keeps the reminder here. Home Assistant watches for the moment, so it comes
even when no tablet is awake: at the time, when the person's state turns "home", or when the machine finishes.

Then the reminder is "due": every tablet shows it as a small paper note until someone touches Done, and the tablet
that heard it says it once. When the person it is for is away from home, it goes to their phone through the Home
Assistant app instead. Automations can use the event hadash_reminder too.

Notes from automations (free): the action hadash.show_note puts a note on every tablet at once, in the same paper
note look ("The garage is still open"). It can go away by itself when its reason ends: when a device reaches a state
("until the garage door is closed"), when the device changes at all, or after some minutes. OK takes it away
everywhere. hadash.clear_note takes it away from an automation. A note has kind "note", a title (may be ""),
and until_entity, until_state and until (ISO time) when it goes away by itself.

One reminder: {id, text, kind: "time" | "arrive" | "done", at (ISO time), person (person.x), entity (the machine),
for (person.x it is for, may be ""), tablet, where, due (ISO time when it came, or None), created}.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import re
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

REMINDERS = "hadash_reminders"
REMINDERS_STORAGE_KEY = "hadash.reminders"
EVENT = "hadash_reminder"
MAX_REMINDERS = 60
MAX_NOTES = 12

# A machine is done when it leaves one of these for something else, or reaches one of DONE.
RUNNING = re.compile(r"^(on|run|running|active|washing|wash|rinse|rinsing|spin|spinning|drying|dry|printing|print|cleaning|in_?progress|busy|heating|program_?running)$")
DONE = re.compile(r"^(finish|finished|end|ended|complete|completed|done|cycle_?finished|ready)$")

REMINDER_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("text"): vol.All(str, vol.Length(min=1, max=200)),
        vol.Required("kind"): vol.In(["time", "arrive", "done"]),
        vol.Optional("at"): vol.Any(None, str),
        vol.Optional("person"): vol.Any(None, vol.All(str, vol.Match(r"^person\.[a-z0-9_]+$"))),
        vol.Optional("entity"): vol.Any(None, vol.All(str, vol.Match(r"^[a-z_]+\.[a-z0-9_]+$"))),
        vol.Optional("for", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("tablet", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("where", default=""): vol.All(str, vol.Length(max=80)),
    }
)


class ReminderStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, REMINDERS_STORAGE_KEY)
        self.items: list[dict[str, Any]] = []
        self._watch: dict[str, Callable[[], None]] = {}
        self._listeners: set[Callable[[list[dict[str, Any]]], None]] = set()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.items = [r for r in data.get("reminders", []) if isinstance(r, dict) and r.get("id")]
        for r in list(self.items):
            if r.get("kind") == "note":
                self._arm_note(r)
            else:
                self._arm(r)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop)

    @callback
    def _stop(self, _event: Event | None = None) -> None:
        for unsub in self._watch.values():
            unsub()
        self._watch.clear()

    def _changed(self) -> None:
        self._store.async_delay_save(lambda: {"reminders": self.items}, 1)
        for listener in list(self._listeners):
            listener(self.items)

    @callback
    def async_listen(self, cb: Callable[[list[dict[str, Any]]], None]) -> Callable[[], None]:
        self._listeners.add(cb)
        return lambda: self._listeners.discard(cb)

    # ---- Changes from the tablets ----

    def add(self, reminder: dict[str, Any]) -> bool:
        if len(self.items) >= MAX_REMINDERS:
            return False
        r = {**reminder, "due": None, "created": dt_util.utcnow().isoformat()}
        if r["kind"] == "time" and not _when(r.get("at")):
            raise vol.Invalid("A reminder at a time needs a time.")
        if r["kind"] == "arrive" and not r.get("person"):
            raise vol.Invalid("A reminder on arrival needs a person.")
        if r["kind"] == "done" and not r.get("entity"):
            raise vol.Invalid("A reminder for a machine needs the machine.")
        self.remove([r["id"]], quiet=True)
        self.items.append(r)
        self._arm(r)
        self._changed()
        return True

    def remove(self, ids: list[str], quiet: bool = False) -> int:
        before = len(self.items)
        for rid in ids:
            if unsub := self._watch.pop(rid, None):
                unsub()
        self.items = [r for r in self.items if r["id"] not in ids]
        if not quiet and len(self.items) != before:
            self._changed()
        return before - len(self.items)

    def later(self, rid: str, minutes: int) -> bool:
        """Comes back in a few minutes, as a reminder at a time."""
        for r in self.items:
            if r["id"] == rid:
                if unsub := self._watch.pop(rid, None):
                    unsub()
                r.update(kind="time", at=(dt_util.utcnow() + timedelta(minutes=minutes)).isoformat(), due=None)
                self._arm(r)
                self._changed()
                return True
        return False

    # ---- Notes from automations ----

    def show_note(self, note: dict[str, Any]) -> bool:
        """Shows a note on every tablet. False when its reason has already ended (the garage is closed already)."""
        nid = note.get("id") or f"note_{secrets.token_hex(4)}"
        entity, state = note.get("until_entity") or None, note.get("until_state") or None
        if entity and state is not None and (cur := self.hass.states.get(entity)) is not None and cur.state == str(state):
            self.remove([nid])
            return False
        minutes = note.get("minutes")
        now = dt_util.utcnow()
        r = {
            "id": nid,
            "text": note["message"],
            "kind": "note",
            "title": note.get("title") or "",
            "for": "",
            "tablet": "",
            "where": "",
            "until_entity": entity,
            "until_state": None if state is None else str(state),
            # Without a state, the note goes when the device changes from the state it has now.
            "from_state": None if not entity or state is not None else getattr(self.hass.states.get(entity), "state", None),
            "until": (now + timedelta(minutes=minutes)).isoformat() if minutes else None,
            "due": now.isoformat(),
            "created": now.isoformat(),
        }
        self.remove([nid], quiet=True)
        notes = [x for x in self.items if x.get("kind") == "note"]
        if len(notes) >= MAX_NOTES:
            self.remove([notes[0]["id"]], quiet=True)
        if len(self.items) >= MAX_REMINDERS:
            return False
        self.items.append(r)
        self._arm_note(r)
        self._changed()
        return True

    def _arm_note(self, r: dict[str, Any]) -> None:
        rid = r["id"]
        unsubs: list[Callable[[], None]] = []
        until = _when(r.get("until"))
        if until:
            if until <= dt_util.utcnow():
                self.hass.loop.call_soon(self.remove, [rid])
                return

            @callback
            def at_time(_now: datetime) -> None:
                self.remove([rid])

            unsubs.append(async_track_point_in_utc_time(self.hass, at_time, until))
        if entity := r.get("until_entity"):
            want, was = r.get("until_state"), r.get("from_state")
            cur = self.hass.states.get(entity)
            if cur is not None and ((want is not None and cur.state == want) or (want is None and was is not None and cur.state != was)):
                # It ended while Home Assistant was off.
                self.hass.loop.call_soon(self.remove, [rid])
                return

            @callback
            def changed(event: Event) -> None:
                new = event.data.get("new_state")
                if new is None or new.state in ("unavailable", "unknown"):
                    return
                if (want is not None and new.state == want) or (want is None and new.state != was):
                    self.remove([rid])

            unsubs.append(async_track_state_change_event(self.hass, [entity], changed))
        if unsubs:

            def stop() -> None:
                for unsub in unsubs:
                    unsub()

            self._watch[rid] = stop

    # ---- Watching for the moment ----

    def _arm(self, r: dict[str, Any]) -> None:
        if r.get("due"):
            return
        rid = r["id"]
        if r["kind"] == "time":
            when = _when(r.get("at"))
            if not when:
                return
            if when <= dt_util.utcnow():
                # Missed while Home Assistant was off: it comes now.
                self.hass.loop.call_soon(self._fire, rid)
                return

            @callback
            def at_time(_now: datetime) -> None:
                self._fire(rid)

            self._watch[rid] = async_track_point_in_utc_time(self.hass, at_time, when)
        elif r["kind"] == "arrive":

            @callback
            def arrived(event: Event) -> None:
                old, new = event.data.get("old_state"), event.data.get("new_state")
                if new is not None and new.state == "home" and (old is None or old.state != "home"):
                    self._fire(rid)

            self._watch[rid] = async_track_state_change_event(self.hass, [r["person"]], arrived)
        elif r["kind"] == "done":

            @callback
            def changed(event: Event) -> None:
                old, new = event.data.get("old_state"), event.data.get("new_state")
                if old is None or new is None or new.state in ("unavailable", "unknown"):
                    return
                was, now = old.state.lower(), new.state.lower()
                if was == now:
                    return
                if DONE.match(now) or (RUNNING.match(was) and not RUNNING.match(now)):
                    self._fire(rid)

            self._watch[rid] = async_track_state_change_event(self.hass, [r["entity"]], changed)

    @callback
    def _fire(self, rid: str) -> None:
        r = next((x for x in self.items if x["id"] == rid), None)
        if r is None or r.get("due"):
            return
        if unsub := self._watch.pop(rid, None):
            unsub()
        r["due"] = dt_util.utcnow().isoformat()
        self._changed()
        self.hass.bus.async_fire(EVENT, {"id": rid, "text": r["text"], "for": r.get("for", ""), "kind": r["kind"]})
        person = r.get("for") or ""
        state = self.hass.states.get(person) if person else None
        # Away from home: the phone gets it, through the Home Assistant app.
        if state is not None and state.state != "home":
            self.hass.async_create_task(self._to_phone(person, r["text"]))

    async def _to_phone(self, person: str, text: str) -> None:
        for service in phone_services(self.hass, person):
            try:
                await self.hass.services.async_call("notify", service, {"title": "Reminder", "message": text}, blocking=False)
            except Exception:  # noqa: BLE001 - a phone that cannot be reached must not stop the others
                _LOGGER.debug("Reminder: notify.%s failed", service, exc_info=True)


def phone_services(hass: HomeAssistant, person: str) -> list[str]:
    """The notify services of a person's phones (the Home Assistant app), from the person's device trackers."""
    state = hass.states.get(person)
    if state is None:
        return []
    ents = er.async_get(hass)
    devs = dr.async_get(hass)
    out: list[str] = []
    for tracker in state.attributes.get("device_trackers", []) or []:
        entry = ents.async_get(tracker)
        if entry is None or entry.platform != "mobile_app" or not entry.device_id:
            continue
        device = devs.async_get(entry.device_id)
        if device is None:
            continue
        service = f"mobile_app_{slugify(device.name_by_user or device.name or '')}"
        if hass.services.has_service("notify", service) and service not in out:
            out.append(service)
    return out


def _when(at: str | None) -> datetime | None:
    if not at:
        return None
    when = dt_util.parse_datetime(at)
    if when is None:
        return None
    return dt_util.as_utc(when if when.tzinfo else when.replace(tzinfo=dt_util.get_default_time_zone()))


async def async_setup(hass: HomeAssistant) -> None:
    store = ReminderStore(hass)
    await store.async_load()
    hass.data[REMINDERS] = store
    for command in (ws_list, ws_add, ws_done, ws_later, ws_subscribe, ws_phone):
        websocket_api.async_register_command(hass, command)

    async def show(call: ServiceCall) -> None:
        store.show_note(dict(call.data))

    async def clear(call: ServiceCall) -> None:
        store.remove(call.data["id"])

    hass.services.async_register(DOMAIN, "show_note", show, schema=SHOW_NOTE_SCHEMA)
    hass.services.async_register(DOMAIN, "clear_note", clear, schema=CLEAR_NOTE_SCHEMA)


SHOW_NOTE_SCHEMA = vol.Schema(
    {
        vol.Required("message"): vol.All(cv.string, vol.Length(min=1, max=200)),
        vol.Optional("title"): vol.All(cv.string, vol.Length(max=40)),
        vol.Optional("id"): vol.All(cv.string, vol.Length(min=1, max=64)),
        vol.Optional("until_entity"): cv.entity_id,
        vol.Optional("until_state"): cv.string,
        vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=24 * 60)),
    }
)
CLEAR_NOTE_SCHEMA = vol.Schema({vol.Required("id"): vol.All(cv.ensure_list, [cv.string])})


# Like the layout, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/reminders"})
@callback
def ws_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"reminders": hass.data[REMINDERS].items})


@websocket_api.websocket_command({vol.Required("type"): "hadash/reminders/add", vol.Required("reminder"): REMINDER_SCHEMA})
@callback
def ws_add(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    try:
        ok = hass.data[REMINDERS].add(msg["reminder"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    if not ok:
        connection.send_error(msg["id"], "too_many", "There are too many reminders. Finish one first.")
        return
    connection.send_result(msg["id"], {"reminders": hass.data[REMINDERS].items})


@websocket_api.websocket_command({vol.Required("type"): "hadash/reminders/done", vol.Required("ids"): [str]})
@callback
def ws_done(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    removed = hass.data[REMINDERS].remove(msg["ids"])
    connection.send_result(msg["id"], {"removed": removed})


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/reminders/later", vol.Required("reminder"): str, vol.Optional("minutes", default=10): vol.All(int, vol.Range(min=1, max=24 * 60))}
)
@callback
def ws_later(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"ok": hass.data[REMINDERS].later(msg["reminder"], msg["minutes"])})


@websocket_api.websocket_command({vol.Required("type"): "hadash/reminders/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The whole list now, and again after each change (a new reminder, one that came, one that is done)."""
    store: ReminderStore = hass.data[REMINDERS]

    @callback
    def forward(items: list[dict[str, Any]]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"reminders": items}))

    connection.subscriptions[msg["id"]] = store.async_listen(forward)
    connection.send_result(msg["id"])
    forward(store.items)


@websocket_api.websocket_command({vol.Required("type"): "hadash/reminders/phones", vol.Required("person"): str})
@callback
def ws_phone(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The phones a reminder (or "find my phone") can reach for this person."""
    connection.send_result(msg["id"], {"services": phone_services(hass, msg["person"])})
