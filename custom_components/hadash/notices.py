"""Notices, the home that talks first (Pro): one calm line, once, on the screen nearest the person.

Home Assistant watches the house all day, so every screen (Fire, Android, iPad) gets the same notices and the same
learning. Each minute it looks for things worth one sentence:

1. door: a lock is still unlocked at night (from 10 PM), for 15 minutes or more.
2. garage: a garage door or gate has been open for a while (30 minutes at first; see "learning").
3. rain: rain starts within 3 hours while windows, awnings or the garage are open.
4. package: a camera's package sensor (Eufy, Ring, Nest, UniFi, Doorstep watch) says one is waiting.
5. done: the washer, dryer or dishwasher finished.
6. battery: a sensor's battery is low (15% or less). Once a week at most.

Nothing is sent to phones and no screen talks by itself. When someone walks up to a screen, that screen asks here
for the most useful notice (claim). When two screens see the person at the same moment, the one that sees them best
(a touch or a face before a motion sensor) wins: claims wait 0.6 seconds and the best one shows it. A notice is
said once: the same garage opening is never told twice.

Learning ("it knows your normal"): 30 minutes after a notice, Home Assistant looks at the thing again. Somebody did
something (the garage closed, the package was taken), or someone touched the line: it was useful. Nothing changed:
it was ignored. For the garage and the night lock, each ignore doubles the wait (30 minutes, then 1 hour, 2 hours,
at most 4), and a quick answer brings it back down. For the others, three ignores in a row rest that notice for
7 days. The Studio shows what was learned and can forget it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

NOTICES = "hadash_notices"
STORAGE_KEY = "hadash.notices"
KINDS = ("door", "garage", "rain", "package", "done", "battery")
# The order when several fit: safety first, then what needs hands soon, then the rest.
RANK = {k: i for i, k in enumerate(KINDS)}
CONTEST_S = 0.6
JUDGE_S = 30 * 60
QUICK_S = 10 * 60
REST_S = 7 * 86400
SAID_KEEP_S = 3 * 86400
# Learned waits (minutes): where they start and how far they may grow.
WAIT = {"garage": (30, 240), "door": (15, 120)}
NIGHT_FROM, NIGHT_TO = 22, 6
RAIN_AHEAD_S = 3 * 3600
LOW_BATTERY = 15
START_GRACE_S = 5 * 60
FORECAST_EVERY_S = 15 * 60

PACKAGE = re.compile(r"package_(delivered|stranded|watch|waiting|detected)|parcel|paket|colis|paquete", re.I)
APPLIANCE = re.compile(r"washer|washing|dryer|dishwasher|laundry|lave|seche|sèche|wasch|trockner|spül|spuel|geschirr|lavadora|secadora|lavavajillas", re.I)
RUNNING = re.compile(r"^(on|run|running|wash|washing|rinse|rinsing|spin|spinning|dry|drying|active|in_use|in use|program|cycle|heating|main_wash)", re.I)
FINISHED = re.compile(r"^(off|idle|stop|stopped|standby|complete|completed|finished|finish|end|done|ready|cycle_finished|cycle finished)$", re.I)
WET = re.compile(r"rain|pour|storm|lightning|hail", re.I)
BATTERY_WORDS = re.compile(r"\s*(battery level|battery|batterie|batería|bateria|pile)\s*", re.I)
PACKAGE_WORDS = re.compile(r"\s*(package|parcel|paket|colis|paquete)\s*(delivered|stranded|watch|waiting|detected)?\s*", re.I)
SKIP_ID = re.compile(r"tablet|phone|iphone|ipad|fire_hd|watch|pixel|galaxy|macbook", re.I)


def _since(s: State) -> float:
    return s.last_changed.timestamp()


class Notices:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # "kind:entity" -> learned wait in minutes; ignores in a row; rest until (0 = off by "stop these").
        self.waits: dict[str, int] = {}
        self.ignored: dict[str, int] = {}
        self.rest: dict[str, float] = {}
        # Notice key -> when it was said (so it is said once).
        self.said: dict[str, float] = {}
        self.pending: list[dict[str, Any]] = []
        self.forecast: list[dict[str, Any]] = []
        self._forecast_at = 0.0
        self._last: dict[str, str] = {}
        self._finished: dict[str, float] = {}
        self._contests: dict[str, list[tuple[float, asyncio.Future[bool]]]] = {}
        self._shown: dict[str, dict[str, Any]] = {}
        self._listeners: list[Any] = []
        self._unsubs: list[Any] = []
        self._started = time.time()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.waits = {k: int(v) for k, v in data.get("waits", {}).items()}
        self.ignored = {k: int(v) for k, v in data.get("ignored", {}).items()}
        self.rest = {k: float(v) for k, v in data.get("rest", {}).items()}
        now = time.time()
        self.said = {k: float(v) for k, v in data.get("said", {}).items() if now - float(v) < SAID_KEEP_S}

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"waits": self.waits, "ignored": self.ignored, "rest": self.rest, "said": self.said}, 2)

    def state(self) -> dict[str, Any]:
        return {"notices": self.pending, "learned": self.learned()}

    def _tell(self) -> None:
        for listener in list(self._listeners):
            listener(self.state())

    def learned(self) -> list[dict[str, Any]]:
        """What the Studio shows: longer waits and rests, by name."""
        now = time.time()
        out: list[dict[str, Any]] = []
        for key in sorted(set(self.waits) | {k for k, until in self.rest.items() if until == 0 or until > now}):
            kind, _, entity_id = key.partition(":")
            item: dict[str, Any] = {"key": key, "kind": kind, "name": self._name(entity_id) if entity_id else ""}
            if key in self.waits and self.waits[key] != WAIT.get(kind, (0, 0))[0]:
                item["wait"] = self.waits[key]
            if key in self.rest:
                item["rest"] = self.rest[key]
            if len(item) > 3:
                out.append(item)
        return out

    def forget(self, key: str | None) -> None:
        """The Studio's "Forget": one learned thing, or all of them."""
        for d in (self.waits, self.ignored, self.rest):
            if key is None:
                d.clear()
            else:
                d.pop(key, None)
        self._save()
        self.check()

    def stop(self, kind: str, entity_id: str | None) -> None:
        """"Not these" on a screen: this kind (for this thing) is not told again until the Studio forgets it."""
        self.rest[f"{kind}:{entity_id or ''}"] = 0
        self._save()
        self.check()

    def _resting(self, kind: str, entity_id: str) -> bool:
        now = time.time()
        for key in (f"{kind}:{entity_id}", f"{kind}:"):
            until = self.rest.get(key)
            if until is not None and (until == 0 or until > now):
                return True
        return False

    def _wait(self, kind: str, entity_id: str) -> int:
        return self.waits.get(f"{kind}:{entity_id}", WAIT[kind][0])

    # Names, the way people say them.

    def _name(self, entity_id: str) -> str:
        s = self.hass.states.get(entity_id)
        reg = er.async_get(self.hass)
        entry = reg.async_get(entity_id)
        name = None
        if s is not None:
            name = s.attributes.get("friendly_name")
        if not name and entry is not None:
            name = entry.name or entry.original_name
        return str(name or entity_id.split(".", 1)[-1].replace("_", " "))

    def _area(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        area_id = entry.area_id if entry else None
        if not area_id and entry and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return area.name if area else None

    def _device_name(self, entity_id: str, strip: re.Pattern[str]) -> str:
        """A battery or a package sensor by its device's name: "Back door", not "Back door battery level"."""
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device and (device.name_by_user or device.name):
                return str(device.name_by_user or device.name)
        name = strip.sub(" ", self._name(entity_id)).strip() or self._name(entity_id)
        return name[:1].upper() + name[1:]

    def _thing(self, s: State) -> dict[str, str]:
        return {"id": s.entity_id, "name": self._name(s.entity_id)}

    # Watching.

    def start(self) -> None:
        @callback
        def tick(_now: Any) -> None:
            self.hass.async_create_task(self.refresh())

        self._unsubs.append(async_track_time_interval(self.hass, tick, timedelta(minutes=1)))

        @callback
        def appliance(event: Event) -> None:
            new: State | None = event.data.get("new_state")
            if new is not None:
                self._watch_appliance(new)

        @callback
        def appliances_only(data: Any) -> bool:
            return bool(APPLIANCE.search(str(data["entity_id"])))

        self._unsubs.append(self.hass.bus.async_listen("state_changed", appliance, event_filter=appliances_only))

        @callback
        def on_stop(_event: Event) -> None:
            self.shutdown()

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)
        self.hass.async_create_task(self.refresh())

    def shutdown(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []

    @callback
    def _watch_appliance(self, s: State) -> None:
        """The washer finished: it was running, and now it is off, idle or "complete"."""
        was = self._last.get(s.entity_id)
        self._last[s.entity_id] = s.state
        if s.entity_id.split(".")[0] not in ("sensor", "binary_sensor", "select"):
            return
        if was is not None and RUNNING.match(was) and FINISHED.match(s.state):
            self._finished[s.entity_id] = time.time()
        elif RUNNING.match(s.state):
            self._finished.pop(s.entity_id, None)

    async def refresh(self) -> None:
        now = time.time()
        if now - self._forecast_at > FORECAST_EVERY_S:
            self._forecast_at = now
            try:
                from .assistant import weather_forecast

                self.forecast = (await weather_forecast(self.hass, None, "hourly")).get("forecast", [])
            except Exception:  # noqa: BLE001 - no weather: no rain notice
                self.forecast = []
        self.check()

    @callback
    def check(self) -> None:
        found = self.find(dt_util.now())
        if found != self.pending:
            self.pending = found
            self._tell()

    def find(self, now: datetime) -> list[dict[str, Any]]:
        """Every notice that fits now, the most useful first. A notice already said is left out."""
        t = now.timestamp()
        # Right after Home Assistant starts, states come back from "unknown": nothing is told yet.
        if t - self._started < START_GRACE_S:
            return []
        out: list[dict[str, Any]] = []
        states = self.hass.states
        night = now.hour >= NIGHT_FROM or now.hour < NIGHT_TO

        def add(kind: str, key: str, things: list[dict[str, str]], **extra: Any) -> None:
            if key in self.said:
                return
            out.append({"key": key, "kind": kind, "things": things, **extra})

        # 1. A door unlocked at night.
        if night:
            for s in states.async_all("lock"):
                if s.state == "unlocked" and t - _since(s) >= self._wait("door", s.entity_id) * 60 and not self._resting("door", s.entity_id):
                    add("door", f"door:{s.entity_id}:{int(_since(s))}", [self._thing(s)], minutes=int((t - _since(s)) // 60))

        # 2. A garage open for a while.
        open_outside: list[State] = []
        for s in states.async_all("cover"):
            dc = s.attributes.get("device_class")
            if s.state not in ("open", "opening"):
                continue
            if dc in ("garage", "gate"):
                open_outside.append(s)
                if t - _since(s) >= self._wait("garage", s.entity_id) * 60 and not self._resting("garage", s.entity_id):
                    add("garage", f"garage:{s.entity_id}:{int(_since(s))}", [self._thing(s)], minutes=int((t - _since(s)) // 60))
            elif dc in ("awning", "window") or re.search(r"umbrella|parasol|sonnenschirm|sombrilla|skylight", s.entity_id):
                open_outside.append(s)
        windows = [s for s in states.async_all("binary_sensor") if s.attributes.get("device_class") == "window" and s.state == "on"]

        # 3. Rain coming while things are open outside. Not when it already rains.
        weather = next(iter(states.async_all("weather")), None)
        if self.forecast and (windows or open_outside) and not (weather and WET.search(weather.state)):
            at = None
            for f in self.forecast:
                try:
                    when = dt_util.parse_datetime(str(f.get("datetime")))
                except (TypeError, ValueError):
                    continue
                if when is None or when.timestamp() < t - 1800 or when.timestamp() > t + RAIN_AHEAD_S:
                    continue
                if (f.get("precipitation_probability") or 0) >= 60 or WET.search(str(f.get("condition", ""))):
                    at = when
                    break
            # 45 minutes or less: Home moments' Weather change and Soon take over, with a plan.
            if at is not None and at.timestamp() - t > 45 * 60:
                things = [self._thing(s) for s in windows + open_outside if not self._resting("rain", s.entity_id)]
                if things:
                    add("rain", f"rain:{at.date()}:{at.hour}", things, at=at.isoformat())

        # 4. A package waiting.
        for s in states.async_all("binary_sensor"):
            if s.state == "on" and PACKAGE.search(s.entity_id) and t - _since(s) >= 180 and not self._resting("package", s.entity_id):
                place = self._area(s.entity_id) or self._device_name(s.entity_id, PACKAGE_WORDS)
                add("package", f"package:{s.entity_id}:{int(_since(s))}", [{"id": s.entity_id, "name": place}])

        # 5. The washer finished (from 2 minutes to 3 hours ago; at night it waits for the morning).
        for entity_id, at in list(self._finished.items()):
            if t - at > 3 * 3600:
                self._finished.pop(entity_id, None)
            elif t - at >= 120 and not night and not self._resting("done", entity_id):
                add("done", f"done:{entity_id}:{int(at)}", [{"id": entity_id, "name": self._device_name(entity_id, re.compile(r"\s*(status|state|operation|machine state|run state|cycle)\s*", re.I))}], minutes=int((t - at) // 60))

        # 6. Low batteries, in the day, once a week.
        if 9 <= now.hour < 21:
            low: list[tuple[float, dict[str, str]]] = []
            for s in states.async_all("sensor"):
                if s.attributes.get("device_class") != "battery" or SKIP_ID.search(s.entity_id) or self._resting("battery", s.entity_id):
                    continue
                try:
                    level = float(s.state)
                except ValueError:
                    continue
                if 0 <= level <= LOW_BATTERY:
                    low.append((level, {"id": s.entity_id, "name": self._device_name(s.entity_id, BATTERY_WORDS)}))
            for s in states.async_all("binary_sensor"):
                if s.attributes.get("device_class") == "battery" and s.state == "on" and not SKIP_ID.search(s.entity_id) and not self._resting("battery", s.entity_id):
                    low.append((0, {"id": s.entity_id, "name": self._device_name(s.entity_id, BATTERY_WORDS)}))
            if low:
                low.sort(key=lambda x: x[0])
                week = now.isocalendar()
                add("battery", f"battery:{week[0]}-{week[1]}", [x[1] for x in low])

        out.sort(key=lambda n: RANK[n["kind"]])
        return out

    # Telling: the nearest screen, once.

    async def claim(self, key: str, score: float) -> bool:
        """A screen with someone at it asks to say [key]. The best one within 0.6 seconds gets it."""
        if key in self.said or not any(n["key"] == key for n in self.pending):
            return False
        fut: asyncio.Future[bool] = self.hass.loop.create_future()
        contest = self._contests.get(key)
        if contest is None:
            contest = self._contests[key] = []
            async_call_later(self.hass, CONTEST_S, lambda _now: self._decide(key))
        contest.append((score, fut))
        return await fut

    @callback
    def _decide(self, key: str) -> None:
        contest = self._contests.pop(key, [])
        if not contest:
            return
        best = max(range(len(contest)), key=lambda i: (contest[i][0], -i))
        for i, (_score, fut) in enumerate(contest):
            if not fut.done():
                fut.set_result(i == best)
        notice = next((n for n in self.pending if n["key"] == key), None)
        self.said[key] = time.time()
        self.pending = [n for n in self.pending if n["key"] != key]
        self._save()
        self._tell()
        if notice is not None:
            before = {x["id"]: self._snapshot(x["id"]) for x in notice["things"]}
            shown = time.time()
            notice["_heard"] = False
            self._shown[key] = notice
            async_call_later(self.hass, JUDGE_S, lambda _now: self._judge(notice, before, shown))

    def heard(self, key: str) -> None:
        """Someone touched the line: it was useful."""
        n = self._shown.get(key)
        if n is not None:
            n["_heard"] = True
            for x in n["things"]:
                self.ignored.pop(f"{n['kind']}:{x['id']}", None)
            self._save()

    def _snapshot(self, entity_id: str) -> tuple[str, float] | None:
        s = self.hass.states.get(entity_id)
        return (s.state, _since(s)) if s else None

    @callback
    def _judge(self, notice: dict[str, Any], before: dict[str, Any], shown: float) -> None:
        """30 minutes later: was it useful (something changed, or someone touched it), or ignored?"""
        self._shown.pop(notice["key"], None)
        kind = notice["kind"]
        for x in notice["things"]:
            key = f"{kind}:{x['id']}"
            now_s = self._snapshot(x["id"])
            acted = now_s is not None and before.get(x["id"]) is not None and now_s != before[x["id"]]
            s = self.hass.states.get(x["id"])
            if kind == "battery":
                # A battery level moves by itself: only a new battery (a jump up) is "acted".
                try:
                    acted = s is not None and float(s.state) > LOW_BATTERY + 20
                except ValueError:
                    acted = False
            quick = acted and s is not None and _since(s) - shown < QUICK_S
            if acted or notice.get("_heard"):
                self.ignored.pop(key, None)
                if kind in WAIT and quick and key in self.waits:
                    start, _top = WAIT[kind]
                    self.waits[key] = max(start, int(self.waits[key] * 0.75))
                    if self.waits[key] == start:
                        self.waits.pop(key)
                continue
            if kind in WAIT:
                start, top = WAIT[kind]
                self.waits[key] = min(top, self._wait(kind, x["id"]) * 2)
            else:
                self.ignored[key] = self.ignored.get(key, 0) + 1
                if self.ignored[key] >= 3:
                    self.rest[key] = time.time() + REST_S
                    self.ignored.pop(key, None)
        self._save()
        self._tell()


async def async_setup(hass: HomeAssistant) -> None:
    notices = Notices(hass)
    await notices.async_load()
    hass.data[NOTICES] = notices
    notices.start()
    for command in (ws_subscribe, ws_claim, ws_heard, ws_stop, ws_forget):
        websocket_api.async_register_command(hass, command)


# Like Home moments, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/notices/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    notices: Notices = hass.data[NOTICES]

    @callback
    def forward(state: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], state))

    notices._listeners.append(forward)

    @callback
    def unsub() -> None:
        if forward in notices._listeners:
            notices._listeners.remove(forward)

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    forward(notices.state())


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/notices/claim", vol.Required("key"): vol.All(str, vol.Length(min=1, max=300)), vol.Optional("score", default=1): vol.Coerce(float)}
)
@websocket_api.async_response
async def ws_claim(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"ok": await hass.data[NOTICES].claim(msg["key"], msg["score"])})


@websocket_api.websocket_command({vol.Required("type"): "hadash/notices/heard", vol.Required("key"): vol.All(str, vol.Length(min=1, max=300))})
@callback
def ws_heard(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[NOTICES].heard(msg["key"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/notices/stop", vol.Required("kind"): vol.In(KINDS), vol.Optional("entity_id"): vol.Any(None, str)}
)
@callback
def ws_stop(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[NOTICES].stop(msg["kind"], msg.get("entity_id"))
    connection.send_result(msg["id"], hass.data[NOTICES].state())


@websocket_api.websocket_command({vol.Required("type"): "hadash/notices/forget", vol.Optional("key"): vol.Any(None, str)})
@callback
def ws_forget(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[NOTICES].forget(msg.get("key"))
    connection.send_result(msg["id"], hass.data[NOTICES].state())
