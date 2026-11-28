"""Care (family homes): a calm view of a family member's home on your own wall, with gentle alerts.

Two Home Assistants, each with Dashboard Studio. The family member's home (for example your parents') SHARES, and
your home WATCHES. Nothing else is needed: no extra sensors, no account and no monthly fee.

Linking, safely:
1. In the shared home's Studio, "Share this home" makes a care code: the home's own address, a link id and a long
   random key (32 bytes). Home Assistant keeps only a hash of the key. A code works for one family home and can be
   removed at any time.
2. In your Studio, "Add a family home" takes the code. From then on your Home Assistant asks the shared home for a
   short summary every few minutes, over the shared home's own address (Home Assistant Cloud, a domain or a VPN).
3. The key opens only this summary and a heart. It is not a Home Assistant login: it cannot see a device, a room, a
   camera or a name, and it cannot change anything. Wrong keys are counted, and an address that keeps trying is
   refused for an hour.

Dignity first. The summary is the shape of the day only: when the home woke up, whether it is calm, whether everyone
is out, and the inside temperature. Never rooms, cameras or device names. The shared home always shows that it is
shared, and one touch there pauses it until tomorrow. "I'm fine" on its wall clears any alert at once.

The home's normal, learned from its own history with no setup: the first sign of life each morning (motion, a door,
a light switched by hand, music or TV) and the longest calm stretch of a normal day. Gentle alerts:
1. morning: no sign of life an hour after the latest usual morning.
2. quiet: no sign of life during the day for much longer than usual (at least 3 hours), while someone is home.
3. cold / hot: the inside temperature is below 16 °C or above 30 °C for half an hour.
4. unreachable (on the watching side): the shared home has not answered for 45 minutes.
Everyone being out is normal, never an alert. Your walls show one calm line; your phones get one quiet notification
10 minutes later unless someone touched the line. When it is good again, one more line says so.

Our own twist: a heart. A touch on the care card sends one; the shared home's wall shows "Radu sends a heart", and a
touch there sends one back.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Any

from aiohttp import ClientError, ClientTimeout, web
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import PRO, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

CARE = "hadash_care"
STORAGE_KEY = "hadash.care"
VIEW_URL = "/api/hadash/care/{link_id}"
CODE_PREFIX = "DSCARE1."
HEADER_KEY = "X-Hadash-Care"
HEADER_FROM = "X-Hadash-From"

KEEP_DAYS = 28
LEARN_DAYS = 21
MIN_DAYS = 4
BUCKET_MIN = 15
BUCKETS = 24 * 60 // BUCKET_MIN
MORNING_FROM = 4 * 60
MORNING_LATEST = 13 * 60
MORNING_UNTIL = 14 * 60
MORNING_GRACE = 60
DAY_FROM, DAY_TO = 9 * 60, 20 * 60
QUIET_MIN_H, QUIET_MAX_H, QUIET_DEFAULT_H = 3, 6, 4
COLD_C, HOT_C = 16.0, 30.0
TEMP_HOLD_S = 30 * 60
FINE_HOLD_S = 4 * 3600

POLL_S = 5 * 60
POLL_FAST_S = 60
UNREACHABLE_S = 45 * 60
PHONE_AFTER_S = 10 * 60
QUIET_HOURS = (22, 7)
EVENT_KEEP_S = 2 * 3600
HEART_KEEP_S = 24 * 3600
UNUSED_CODE_S = 7 * 86400
MAX_LINKS = 6
MAX_HOMES = 4
WRONG_LIMIT = 20
WRONG_BLOCK_S = 3600

ATTENTION = ("morning", "quiet", "cold", "hot", "unreachable")

# What counts as a sign of life. Inside only: outdoor cameras see the street, not the family.
LIFE_CLASSES = {"motion", "occupancy", "presence", "door", "opening", "garage_door", "window", "moving"}
BY_HAND = {"light", "switch", "fan", "cover", "lock", "climate", "media_player", "input_boolean", "scene", "button", "remote", "vacuum"}
OUTSIDE = re.compile(r"outdoor|outside|exterior|street|driveway|yard|garden|porch|patio|deck|lawn|gate|doorbell|package|parcel|camera|cam_|_cam\b", re.I)
NOT_INSIDE_TEMP = re.compile(r"outdoor|outside|exterior|weather|garage|freezer|fridge|refrigerator|pool|tub|spa|cpu|processor|battery|water|aquarium|attic|crawl|soil|plant|car|oven|grill|smoker|probe", re.I)

# The words for phones, in the watching home's language. The wall's words are in the renderer.
WORDS: dict[str, dict[str, str]] = {
    "en": {
        "morning": "It is a quiet morning at {home}. There is no sign of life yet.",
        "quiet": "{home} has been quiet for a long time.",
        "cold": "It is cold inside {home}.",
        "hot": "It is very warm inside {home}.",
        "unreachable": "{home} has not answered for a while. It may be the internet.",
        "clear": "All good again at {home}.",
        "fine": "{home} says all is fine.",
        "title": "Care",
    },
    "fr": {
        "morning": "Matinée calme chez {home}. Pas encore de signe de vie.",
        "quiet": "Tout est calme chez {home} depuis longtemps.",
        "cold": "Il fait froid à l'intérieur chez {home}.",
        "hot": "Il fait très chaud à l'intérieur chez {home}.",
        "unreachable": "{home} ne répond plus depuis un moment. C'est peut-être Internet.",
        "clear": "Tout va bien de nouveau chez {home}.",
        "fine": "{home} dit que tout va bien.",
        "title": "Proches",
    },
    "de": {
        "morning": "Ein stiller Morgen bei {home}. Noch kein Lebenszeichen.",
        "quiet": "Bei {home} ist es schon lange still.",
        "cold": "Bei {home} ist es drinnen kalt.",
        "hot": "Bei {home} ist es drinnen sehr warm.",
        "unreachable": "{home} antwortet seit einer Weile nicht. Vielleicht ist es das Internet.",
        "clear": "Bei {home} ist wieder alles gut.",
        "fine": "{home} sagt, alles ist gut.",
        "title": "Familie",
    },
    "es": {
        "morning": "Mañana tranquila en {home}. Aún no hay señales de vida.",
        "quiet": "{home} lleva mucho tiempo en calma.",
        "cold": "Hace frío dentro de {home}.",
        "hot": "Hace mucho calor dentro de {home}.",
        "unreachable": "{home} no responde desde hace un rato. Puede ser Internet.",
        "clear": "Todo bien de nuevo en {home}.",
        "fine": "{home} dice que todo está bien.",
        "title": "Familia",
    },
}


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def make_code(url: str, link_id: str, key: str) -> str:
    raw = json.dumps({"u": url.rstrip("/"), "i": link_id, "k": key}, separators=(",", ":")).encode()
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_code(code: str) -> dict[str, str] | None:
    """The address, link id and key in a care code, or None when it is not one."""
    code = re.sub(r"\s+", "", code or "")
    if not code.startswith(CODE_PREFIX):
        return None
    body = code[len(CODE_PREFIX) :]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    url, link_id, key = data.get("u"), data.get("i"), data.get("k")
    if not (isinstance(url, str) and re.match(r"^https?://[^\s/]+", url) and isinstance(link_id, str) and isinstance(key, str)):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,40}", link_id) or len(key) < 30:
        return None
    return {"url": url.rstrip("/"), "id": link_id, "key": key}


# Days: {"2026-11-27": {"f": first minute of the morning or None, "a": hex bits of active buckets, "o": hex bits of
# buckets when everyone was out}}. Hex text, because the JSON in Home Assistant's storage keeps no big numbers.


def _bits(day: dict[str, Any] | None, field: str) -> int:
    try:
        return int((day or {}).get(field) or "0", 16)
    except ValueError:
        return 0


def _pct(values: list[int], p: float) -> int:
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(p * (len(v) - 1))))]


def usual_morning(days: dict[str, dict[str, Any]], today: str) -> dict[str, int] | None:
    """When the home usually wakes up (minutes after midnight): the usual window, and when a late morning is told."""
    firsts = []
    for key in sorted(days)[-LEARN_DAYS - 1 :]:
        if key == today:
            continue
        d = days[key]
        first = d.get("f")
        if first is None:
            continue
        # A morning away from home (out before the first sign) says nothing about their normal.
        out = _bits(d, "o")
        if out and out & ((1 << (first // BUCKET_MIN)) - 1) & ~((1 << (MORNING_FROM // BUCKET_MIN)) - 1):
            continue
        firsts.append(int(first))
    if len(firsts) < MIN_DAYS:
        return None
    lo, hi, late = _pct(firsts, 0.2), _pct(firsts, 0.8), _pct(firsts, 0.9)
    tell = max(late + MORNING_GRACE, lo + 90)
    # A home that usually wakes after noon gets no morning alert: the long quiet check still looks after it.
    return {"from": lo, "to": hi, "tell": tell if tell <= MORNING_LATEST else None, "days": len(firsts)}


def quiet_limit(days: dict[str, dict[str, Any]], today: str) -> int:
    """How long a calm stretch of the day may be before it is told, in minutes (learned, 3 to 6 hours)."""
    gaps = []
    a, b = DAY_FROM // BUCKET_MIN, DAY_TO // BUCKET_MIN
    for key in sorted(days)[-LEARN_DAYS - 1 :]:
        if key == today or days[key].get("f") is None:
            continue
        busy = _bits(days[key], "a") | _bits(days[key], "o")
        if not busy:
            continue
        run = best = 0
        for i in range(a, b):
            run = 0 if busy >> i & 1 else run + 1
            best = max(best, run)
        gaps.append(best)
    if len(gaps) < MIN_DAYS:
        return QUIET_DEFAULT_H * 60
    limit = (_pct(gaps, 0.9) + 2) * BUCKET_MIN
    return max(QUIET_MIN_H * 60, min(QUIET_MAX_H * 60, limit))


def _minute(d: datetime) -> int:
    return d.hour * 60 + d.minute


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _quiet_now(now: datetime) -> bool:
    start, end = QUIET_HOURS
    return now.hour >= start or now.hour < end


class Care:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # Sharing side.
        self.links: list[dict[str, Any]] = []
        self.paused_until = 0.0
        self.fine_at = 0.0
        self.heart_back_at = 0.0
        self.share_temp = True
        self.address: str | None = None
        self.hearts: list[dict[str, Any]] = []
        self.days: dict[str, dict[str, Any]] = {}
        self.last_life = 0.0
        self._temp_out: dict[str, float] = {}
        # Watching side.
        self.homes: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._wrong: dict[str, list[float]] = {}
        self._listeners: list[Any] = []
        self._unsubs: list[Any] = []
        self._started = time.time()
        self._polling: set[str] = set()

    # ---- storage ----

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.links = list(data.get("links", []))
        self.paused_until = float(data.get("paused_until", 0))
        self.fine_at = float(data.get("fine_at", 0))
        self.heart_back_at = float(data.get("heart_back_at", 0))
        self.share_temp = bool(data.get("share_temp", True))
        self.address = data.get("address")
        self.hearts = list(data.get("hearts", []))
        self.days = dict(data.get("days", {}))
        self.last_life = float(data.get("last_life", 0))
        self.homes = list(data.get("homes", []))
        self.events = list(data.get("events", []))

    def _data(self) -> dict[str, Any]:
        return {
            "links": self.links,
            "paused_until": self.paused_until,
            "fine_at": self.fine_at,
            "heart_back_at": self.heart_back_at,
            "share_temp": self.share_temp,
            "address": self.address,
            "hearts": self.hearts,
            "days": self.days,
            "last_life": self.last_life,
            "homes": self.homes,
            "events": self.events,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 5)

    def _tell(self) -> None:
        state = self.state()
        for listener in list(self._listeners):
            listener(state)

    # ---- start ----

    def start(self) -> None:
        self._unsubs.append(self.hass.bus.async_listen("state_changed", self._on_state))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=60)))

        # The minute timer ends with Home Assistant, so nothing is left running after it stops.
        @callback
        def on_stop(_event: Event) -> None:
            self.stop()

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

    def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    async def async_learn_from_history(self) -> None:
        """The first time, the last two weeks come from Home Assistant's own history, so it knows their normal at once."""
        if self.days or not self.links:
            return
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            return
        if "recorder" not in self.hass.config.components:
            return
        ids = [s.entity_id for s in self.hass.states.async_all("binary_sensor") if self._is_life_sensor(s)]
        ids += [s.entity_id for s in self.hass.states.async_all("media_player")]
        persons = [s.entity_id for s in self.hass.states.async_all("person") if s.attributes.get("device_trackers")]
        if not ids:
            return
        end = dt_util.utcnow()
        start = end - timedelta(days=14)
        try:
            found = await get_instance(self.hass).async_add_executor_job(
                lambda: history.get_significant_states(self.hass, start, end, ids + persons, None, False, False, True, True)
            )
        except Exception:  # noqa: BLE001 - learning from history is a bonus; live learning still works
            _LOGGER.debug("Care: could not read history", exc_info=True)
            return
        for eid, states in found.items():
            if eid.startswith("person."):
                continue
            for s in states:
                value = s["state"] if isinstance(s, dict) else s.state
                when = s.get("last_changed") if isinstance(s, dict) else s.last_changed
                if isinstance(when, str):
                    when = dt_util.parse_datetime(when)
                if when is None:
                    continue
                if (eid.startswith("binary_sensor.") and value == "on") or (eid.startswith("media_player.") and value == "playing"):
                    self._mark(dt_util.as_local(when), "a")
        # When everyone was out (the person entities' history), so a quiet stretch away is normal.
        if persons:
            changes: list[tuple[datetime, str, bool]] = []
            for eid in persons:
                for s in found.get(eid, []):
                    value = s["state"] if isinstance(s, dict) else s.state
                    when = s.get("last_changed") if isinstance(s, dict) else s.last_changed
                    if isinstance(when, str):
                        when = dt_util.parse_datetime(when)
                    if when is not None:
                        changes.append((when, eid, value == "home"))
            changes.sort()
            home = {p: True for p in persons}
            t = start
            for when, eid, at_home in changes + [(end, "", True)]:
                while t < when:
                    if not any(home.values()):
                        self._mark(dt_util.as_local(t), "o")
                    t += timedelta(minutes=BUCKET_MIN)
                if eid:
                    home[eid] = at_home
        self._trim()
        self._save()

    # ---- signs of life (the sharing home) ----

    def _is_life_sensor(self, s: State) -> bool:
        if s.attributes.get("device_class") not in LIFE_CLASSES:
            return False
        name = f"{s.entity_id} {s.attributes.get('friendly_name', '')}"
        if OUTSIDE.search(name):
            return False
        entry = er.async_get(self.hass).async_get(s.entity_id)
        if entry and entry.device_id:
            # A camera's motion sensor sees outside: skip every sensor of a camera's device.
            for e in er.async_entries_for_device(er.async_get(self.hass), entry.device_id):
                if e.domain == "camera":
                    return False
        return True

    @callback
    def _on_state(self, event: Event) -> None:
        if not self.links:
            return
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if new is None or old is None or new.state == old.state:
            return
        if new.state in ("unavailable", "unknown") or old.state in ("unavailable", "unknown"):
            return
        domain = new.domain
        life = False
        if domain == "binary_sensor":
            life = self._is_life_sensor(new) and (new.state == "on" or new.attributes.get("device_class") in ("door", "opening", "window", "garage_door"))
        elif domain == "media_player":
            life = new.state == "playing" and new.context.parent_id is None
        elif domain in BY_HAND:
            # By a person: from an app, a wall or a switch on the wall; never by an automation.
            life = new.context.parent_id is None and not OUTSIDE.search(new.entity_id)
        if life:
            self.life(dt_util.now())

    def life(self, now: datetime) -> None:
        self.last_life = now.timestamp()
        morning = self.days.get(now.date().isoformat(), {}).get("f") is None
        self._mark(now, "a")
        if morning and self.days[now.date().isoformat()].get("f") is not None:
            # The first sign of life of the day: the family's walls may change at once.
            self._tell()
        self._save()

    def _mark(self, when: datetime, field: str) -> None:
        day = self.days.setdefault(when.date().isoformat(), {})
        bit = 1 << (_minute(when) // BUCKET_MIN)
        day[field] = format(_bits(day, field) | bit, "x")
        if field == "a" and day.get("f") is None and _minute(when) >= MORNING_FROM:
            day["f"] = _minute(when)

    def _trim(self) -> None:
        keep = sorted(self.days)[-KEEP_DAYS:]
        self.days = {k: self.days[k] for k in keep}

    def everyone_out(self) -> bool | None:
        """True when every person with a tracker is away; None when the home has no such person."""
        persons = [s for s in self.hass.states.async_all("person") if s.attributes.get("device_trackers")]
        if not persons:
            return None
        return all(s.state not in ("home", "unknown", "unavailable") for s in persons)

    def inside_temp(self) -> float | None:
        """The inside temperature in °C: the thermostats, or else the inside temperature sensors (the middle one)."""
        values: list[float] = []
        for s in self.hass.states.async_all("climate"):
            t = s.attributes.get("current_temperature")
            if isinstance(t, (int, float)):
                values.append(float(t))
        unit = self.hass.config.units.temperature_unit
        if not values:
            for s in self.hass.states.async_all("sensor"):
                if s.attributes.get("device_class") != "temperature":
                    continue
                if NOT_INSIDE_TEMP.search(f"{s.entity_id} {s.attributes.get('friendly_name', '')}"):
                    continue
                try:
                    values.append(float(s.state))
                except ValueError:
                    continue
                unit = s.attributes.get("unit_of_measurement") or unit
        if not values:
            return None
        v = sorted(values)[len(values) // 2]
        if "F" in str(unit):
            v = (v - 32) * 5 / 9
        return round(v, 1)

    def attention(self, now: datetime) -> list[dict[str, Any]]:
        """What the family should gently hear about right now, from this (shared) home."""
        out: list[dict[str, Any]] = []
        today = now.date().isoformat()
        day = self.days.get(today, {})
        minute = _minute(now)
        away = self.everyone_out()
        fine = now.timestamp() - self.fine_at < FINE_HOLD_S
        if not away and not fine:
            usual = usual_morning(self.days, today)
            if usual and usual["tell"] is not None and day.get("f") is None and usual["tell"] <= minute < MORNING_UNTIL:
                out.append({"kind": "morning", "since": now.replace(hour=usual["tell"] // 60, minute=usual["tell"] % 60, second=0, microsecond=0).isoformat()})
            # After a restart the stretch counts from the start: Home Assistant did not see the time it was off.
            last = max(self.last_life, self._started, self.fine_at)
            limit = quiet_limit(self.days, today) * 60
            if day.get("f") is not None and DAY_FROM <= minute < DAY_TO and now.timestamp() - last >= limit:
                out.append({"kind": "quiet", "since": dt_util.as_local(dt_util.utc_from_timestamp(last)).isoformat()})
        temp = self.inside_temp() if self.share_temp else None
        for kind, bad in (("cold", temp is not None and temp < COLD_C), ("hot", temp is not None and temp > HOT_C)):
            if not bad:
                self._temp_out.pop(kind, None)
                continue
            since = self._temp_out.setdefault(kind, now.timestamp())
            if now.timestamp() - since >= TEMP_HOLD_S and not fine:
                out.append({"kind": kind, "since": dt_util.as_local(dt_util.utc_from_timestamp(since)).isoformat()})
        return out

    def summary(self, now: datetime | None = None) -> dict[str, Any]:
        """What a linked family home gets: the shape of the day, never rooms, cameras or names."""
        now = now or dt_util.now()
        base = {"v": 1, "now": now.isoformat(), "home": self.hass.config.location_name}
        if now.timestamp() < self.paused_until:
            return {**base, "paused": True, "until": dt_util.as_local(dt_util.utc_from_timestamp(self.paused_until)).isoformat()}
        today = now.date().isoformat()
        day = self.days.get(today, {})
        usual = usual_morning(self.days, today)
        att = self.attention(now)
        away = self.everyone_out()
        minute = _minute(now)
        first = day.get("f")
        if att and att[0]["kind"] in ("morning", "quiet"):
            state = att[0]["kind"]
        elif away:
            state = "out"
        elif first is None:
            state = "waking" if usual and minute >= usual["from"] - 30 else "night"
        elif minute >= 22 * 60 and now.timestamp() - self.last_life > 3600:
            state = "night"
        else:
            state = "calm"
        # Today's pulse: one sign per 15 minutes, up to now ("a" active, "o" out, "." calm).
        a, o = _bits(day, "a"), _bits(day, "o")
        pulse = "".join("a" if a >> i & 1 else "o" if o >> i & 1 else "." for i in range(minute // BUCKET_MIN + 1))
        temp = self.inside_temp() if self.share_temp else None
        last = self.last_life
        return {
            **base,
            "state": state,
            "first": now.replace(hour=first // 60, minute=first % 60, second=0, microsecond=0).isoformat() if first is not None else None,
            "usual": {"from": _hhmm(usual["from"]), "to": _hhmm(usual["to"])} if usual else None,
            "learning": None if usual else len([k for k in self.days if k != today and self.days[k].get("f") is not None]),
            "last": dt_util.as_local(dt_util.utc_from_timestamp(last)).isoformat() if last else None,
            "pulse": pulse,
            "temp": round(temp) if temp is not None else None,
            "attention": att,
            "fine": dt_util.as_local(dt_util.utc_from_timestamp(self.fine_at)).isoformat() if self.fine_at else None,
            "heart_back": dt_util.as_local(dt_util.utc_from_timestamp(self.heart_back_at)).isoformat() if self.heart_back_at else None,
        }

    # ---- sharing: codes and the door for family homes ----

    def address_for_code(self) -> tuple[str | None, bool]:
        """The address family homes use: the one set in the Studio, the Cloud or outside address, else the inside one."""
        if self.address:
            return self.address, True
        try:
            return get_url(self.hass, allow_internal=False, allow_ip=True, prefer_cloud=True), True
        except NoURLAvailableError:
            pass
        try:
            return get_url(self.hass), False
        except NoURLAvailableError:
            return None, False

    def new_code(self, url: str | None = None) -> dict[str, Any]:
        address, outside = (url.rstrip("/"), True) if url else self.address_for_code()
        if not address:
            return {"ok": False, "error": "no_address"}
        now = time.time()
        self.links = [x for x in self.links if x.get("seen") or now - x.get("made", now) < UNUSED_CODE_S][-(MAX_LINKS - 1) :]
        link_id = secrets.token_urlsafe(9)
        key = secrets.token_urlsafe(32)
        self.links.append({"id": link_id, "hash": _hash(key), "made": now, "seen": None, "name": None})
        self._save()
        self._tell()
        start = not self.days
        if start:
            self.hass.async_create_task(self.async_learn_from_history())
        return {"ok": True, "code": make_code(address, link_id, key), "address": address, "outside": outside}

    def remove_link(self, link_id: str) -> None:
        self.links = [x for x in self.links if x["id"] != link_id]
        self._save()
        self._tell()

    def check(self, link_id: str, key: str | None, remote: str) -> dict[str, Any] | None:
        now = time.time()
        tries = [t for t in self._wrong.get(remote, []) if now - t < WRONG_BLOCK_S]
        self._wrong[remote] = tries
        if len(tries) >= WRONG_LIMIT:
            return None
        for link in self.links:
            if link["id"] == link_id and key and hmac.compare_digest(link["hash"], _hash(key)):
                return link
        tries.append(now)
        return None

    def blocked(self, remote: str) -> bool:
        now = time.time()
        return len([t for t in self._wrong.get(remote, []) if now - t < WRONG_BLOCK_S]) >= WRONG_LIMIT

    def seen_by(self, link: dict[str, Any], name: str | None) -> None:
        first = link.get("seen") is None
        link["seen"] = time.time()
        if name:
            link["name"] = name[:60]
        if first:
            self._tell()
        self._save()

    def heart_in(self, link: dict[str, Any], name: str | None) -> None:
        now = time.time()
        self.hearts = [h for h in self.hearts if now - h["at"] < HEART_KEEP_S][-9:]
        self.hearts.append({"key": secrets.token_hex(6), "from": (name or link.get("name") or "")[:60], "at": now})
        self._save()
        self._tell()

    def pause(self, hours: float) -> None:
        if hours <= 0:
            self.paused_until = 0
        else:
            # "Until tomorrow" is until 7 in the morning.
            now = dt_util.now()
            until = now + timedelta(hours=hours)
            if hours >= 12:
                until = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
            self.paused_until = until.timestamp()
        self._save()
        self._tell()

    def fine(self) -> None:
        self.fine_at = time.time()
        self.life(dt_util.now())
        self._tell()

    def heart_back(self) -> None:
        self.heart_back_at = time.time()
        self.life(dt_util.now())
        self._tell()

    # ---- watching: family homes on this wall ----

    async def add_home(self, code: str, name: str, me: str) -> dict[str, Any]:
        found = read_code(code)
        if not found:
            return {"ok": False, "error": "bad_code"}
        if len(self.homes) >= MAX_HOMES and not any(h["id"] == found["id"] for h in self.homes):
            return {"ok": False, "error": "too_many"}
        home = {"id": found["id"], "url": found["url"], "key": found["key"], "name": name.strip()[:40], "me": me.strip()[:40], "notify": None, "summary": None, "ok_at": None, "fail_since": None, "told": {}, "polled": 0}
        result = await self._fetch(home)
        if result is None:
            return {"ok": False, "error": "no_answer"}
        if result == "refused":
            return {"ok": False, "error": "refused"}
        self.homes = [h for h in self.homes if h["id"] != home["id"]] + [home]
        self._apply(home, result)
        self._save()
        self._tell()
        return {"ok": True, "id": home["id"]}

    def remove_home(self, home_id: str) -> None:
        self.homes = [h for h in self.homes if h["id"] != home_id]
        self.events = [e for e in self.events if e.get("home") != home_id]
        self._save()
        self._tell()

    async def _fetch(self, home: dict[str, Any], heart: bool = False) -> dict[str, Any] | str | None:
        session = async_get_clientsession(self.hass)
        url = f"{home['url']}/api/hadash/care/{home['id']}"
        headers = {HEADER_KEY: home["key"], HEADER_FROM: home.get("me") or self.hass.config.location_name}
        try:
            if heart:
                resp = await session.post(url, headers=headers, json={"heart": True}, timeout=ClientTimeout(total=15))
            else:
                resp = await session.get(url, headers=headers, timeout=ClientTimeout(total=15))
            async with resp:
                if resp.status in (401, 403, 404):
                    return "refused"
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("v") == 1 else None

    async def heart(self, home_id: str) -> bool:
        home = next((h for h in self.homes if h["id"] == home_id), None)
        if home is None:
            return False
        result = await self._fetch(home, heart=True)
        if isinstance(result, dict):
            self._apply(home, result)
            self._tell()
            return True
        return False

    def _event(self, home: dict[str, Any], kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        self.events = [e for e in self.events if now - e["at"] < EVENT_KEEP_S]
        e = {"key": secrets.token_hex(6), "home": home["id"], "name": home["name"], "kind": kind, "at": now, **(extra or {})}
        self.events.append(e)
        return e

    def _apply(self, home: dict[str, Any], data: dict[str, Any]) -> None:
        """A new summary: new alerts, alerts that ended, a heart back or "I'm fine"."""
        old = home.get("summary") or {}
        home["summary"] = data
        home["ok_at"] = time.time()
        home["fail_since"] = None
        told: dict[str, Any] = home.setdefault("told", {})
        fine = bool(old) and bool(data.get("fine")) and data.get("fine") != old.get("fine")
        if fine:
            self._event(home, "fine")
        if old and data.get("heart_back") and data.get("heart_back") != old.get("heart_back"):
            self._event(home, "heart_back")
        if data.get("paused"):
            # Paused by the family member: what was told ends quietly, nothing new is told.
            told.clear()
            return
        now_kinds = {a["kind"] for a in data.get("attention", [])}
        for kind in list(told):
            if kind in now_kinds:
                continue
            t = told.pop(kind)
            if not fine:
                self._event(home, "clear", {"was": kind})
            if t.get("phoned") is not None:
                self._phone(home, "fine" if fine else "clear")
        for a in data.get("attention", []):
            if a["kind"] not in told:
                told[a["kind"]] = {"key": f"{home['id']}:{a['kind']}:{a['since']}", "at": time.time(), "since": a["since"], "phoned": None, "seen": False}

    def _failed(self, home: dict[str, Any]) -> None:
        now = time.time()
        if home.get("fail_since") is None:
            home["fail_since"] = now
        told = home.setdefault("told", {})
        if now - home["fail_since"] >= UNREACHABLE_S and "unreachable" not in told:
            since = dt_util.as_local(dt_util.utc_from_timestamp(home["fail_since"])).isoformat()
            told["unreachable"] = {"key": f"{home['id']}:unreachable:{since}", "at": now, "since": since, "phoned": None, "seen": False}

    def alerts(self) -> list[dict[str, Any]]:
        out = []
        for h in self.homes:
            for kind, t in (h.get("told") or {}).items():
                out.append({"key": t["key"], "home": h["id"], "name": h["name"], "kind": kind, "since": t["since"], "seen": t.get("seen", False)})
        return out

    def seen(self, key: str) -> None:
        for h in self.homes:
            for t in (h.get("told") or {}).values():
                if t["key"] == key:
                    t["seen"] = True
        self._save()

    def set_notify(self, home_id: str, persons: list[str] | None) -> None:
        for h in self.homes:
            if h["id"] == home_id:
                h["notify"] = persons
        self._save()
        self._tell()

    def _phone(self, home: dict[str, Any], kind: str) -> None:
        from .phones import phones_of

        lang = (self.hass.config.language or "en").split("-")[0]
        words = WORDS.get(lang, WORDS["en"])
        text = words[kind].format(home=home["name"])
        persons = home.get("notify")
        if persons is None:
            persons = [s.entity_id for s in self.hass.states.async_all("person")]
        for person in persons:
            for phone in phones_of(self.hass, person):
                self.hass.async_create_task(
                    self.hass.services.async_call("notify", phone["service"], {"title": words["title"], "message": text, "data": {"tag": f"hadash-care-{home['id']}"}})
                )

    async def _tick(self, _now: datetime | None = None) -> None:
        now = time.time()
        changed = False
        for home in list(self.homes):
            fast = bool(home.get("told")) or home.get("fail_since") is not None
            if now - home.get("polled", 0) < (POLL_FAST_S if fast else POLL_S) - 5 or home["id"] in self._polling:
                continue
            self._polling.add(home["id"])
            try:
                home["polled"] = now
                result = await self._fetch(home)
                if isinstance(result, dict):
                    self._apply(home, result)
                else:
                    self._failed(home)
                changed = True
            finally:
                self._polling.discard(home["id"])
            # The phone, 10 minutes after the wall, when nobody touched the line (and never at night).
            for kind, t in (home.get("told") or {}).items():
                if t.get("phoned") is None and not t.get("seen") and now - t["at"] >= PHONE_AFTER_S and not _quiet_now(dt_util.now()):
                    t["phoned"] = now
                    self._phone(home, kind)
        # The sharing side: a new day, and the pulse keeps going when everyone is out.
        if self.links:
            local = dt_util.now()
            if self.everyone_out():
                self._mark(local, "o")
            self._trim()
            self._save()
            if local.minute % 15 == 0:
                changed = True
        if changed:
            self._save()
            self._tell()

    def state(self) -> dict[str, Any]:
        now = time.time()
        return {
            "share": {
                "on": any(x.get("seen") for x in self.links),
                "links": [{"id": x["id"], "name": x.get("name"), "seen": x.get("seen"), "made": x.get("made")} for x in self.links],
                "paused_until": self.paused_until if self.paused_until > now else None,
                "temp": self.share_temp,
                "address": self.address,
                "hearts": [h for h in self.hearts if now - h["at"] < HEART_KEEP_S],
                "summary": self.summary() if self.links else None,
            },
            "homes": [
                {"id": h["id"], "name": h["name"], "me": h.get("me"), "summary": h.get("summary"), "ok_at": h.get("ok_at"), "fail_since": h.get("fail_since"), "notify": h.get("notify")}
                for h in self.homes
            ],
            "alerts": self.alerts(),
            "events": [e for e in self.events if now - e["at"] < EVENT_KEEP_S],
            "pro": self._pro(),
        }

    def _pro(self) -> bool:
        pro = self.hass.data.get(PRO)
        return pro is None or pro.state()["plan"] != "free"


class CareView(HomeAssistantView):
    """The door for linked family homes: the summary (GET) and a heart (POST). Only with the link's key."""

    url = VIEW_URL
    name = "api:hadash:care"
    requires_auth = False
    cors_allowed = False

    def __init__(self, care: Care) -> None:
        self.care = care

    def _link(self, request: web.Request, link_id: str) -> dict[str, Any] | web.Response:
        remote = request.remote or "?"
        if self.care.blocked(remote):
            return web.json_response({"error": "wait"}, status=429)
        link = self.care.check(link_id, request.headers.get(HEADER_KEY), remote)
        if link is None:
            return web.json_response({"error": "no"}, status=403)
        self.care.seen_by(link, request.headers.get(HEADER_FROM))
        return link

    async def get(self, request: web.Request, link_id: str) -> web.Response:
        link = self._link(request, link_id)
        if isinstance(link, web.Response):
            return link
        return web.json_response(self.care.summary(), headers={"Cache-Control": "no-store"})

    async def post(self, request: web.Request, link_id: str) -> web.Response:
        link = self._link(request, link_id)
        if isinstance(link, web.Response):
            return link
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("heart"):
            self.care.heart_in(link, request.headers.get(HEADER_FROM))
        return web.json_response(self.care.summary(), headers={"Cache-Control": "no-store"})


async def async_setup(hass: HomeAssistant) -> None:
    care = Care(hass)
    await care.async_load()
    hass.data[CARE] = care
    care.start()
    hass.http.register_view(CareView(care))
    for command in (ws_subscribe, ws_share_new, ws_share_remove, ws_share_settings, ws_pause, ws_fine, ws_heart_back, ws_add, ws_remove, ws_heart, ws_seen, ws_notify):
        websocket_api.async_register_command(hass, command)


# Wall tablets often use a plain user: they may see, send a heart, say "I'm fine" and pause. Linking is for admins.


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    care: Care = hass.data[CARE]

    @callback
    def forward(state: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], state))

    care._listeners.append(forward)

    @callback
    def unsub() -> None:
        if forward in care._listeners:
            care._listeners.remove(forward)

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    forward(care.state())


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/share/new", vol.Optional("address"): vol.Any(None, vol.All(str, vol.Match(r"^https?://\S+$")))})
@websocket_api.require_admin
@callback
def ws_share_new(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], hass.data[CARE].new_code(msg.get("address")))


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/share/remove", vol.Required("link"): str})
@websocket_api.require_admin
@callback
def ws_share_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[CARE].remove_link(msg["link"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/care/share/settings",
        vol.Optional("temp"): bool,
        vol.Optional("address"): vol.Any(None, vol.All(str, vol.Match(r"^https?://\S+$"))),
    }
)
@websocket_api.require_admin
@callback
def ws_share_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    care: Care = hass.data[CARE]
    if "temp" in msg:
        care.share_temp = msg["temp"]
    if "address" in msg:
        care.address = msg["address"].rstrip("/") if msg["address"] else None
    care._save()
    care._tell()
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/share/pause", vol.Required("hours"): vol.All(vol.Coerce(float), vol.Range(min=0, max=48))})
@callback
def ws_pause(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[CARE].pause(msg["hours"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/share/fine"})
@callback
def ws_fine(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[CARE].fine()
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/share/heart_back"})
@callback
def ws_heart_back(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[CARE].heart_back()
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/care/add",
        vol.Required("code"): vol.All(str, vol.Length(min=10, max=2000)),
        vol.Required("name"): vol.All(str, vol.Length(min=1, max=40)),
        vol.Optional("me", default=""): vol.All(str, vol.Length(max=40)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_add(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    care: Care = hass.data[CARE]
    if not care._pro():
        connection.send_result(msg["id"], {"ok": False, "error": "pro"})
        return
    me = msg["me"]
    if not me and connection.user:
        me = connection.user.name or ""
    connection.send_result(msg["id"], await care.add_home(msg["code"], msg["name"], me))


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/remove", vol.Required("home"): str})
@websocket_api.require_admin
@callback
def ws_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[CARE].remove_home(msg["home"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/heart", vol.Required("home"): str})
@websocket_api.async_response
async def ws_heart(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"ok": await hass.data[CARE].heart(msg["home"])})


@websocket_api.websocket_command({vol.Required("type"): "hadash/care/seen", vol.Required("key"): vol.All(str, vol.Length(max=200))})
@callback
def ws_seen(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[CARE].seen(msg["key"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/care/notify", vol.Required("home"): str, vol.Required("persons"): vol.Any(None, [vol.Match(r"^person\.[a-z0-9_]+$")])}
)
@websocket_api.require_admin
@callback
def ws_notify(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[CARE].set_notify(msg["home"], msg["persons"])
    connection.send_result(msg["id"])
