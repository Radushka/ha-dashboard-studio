"""Habits, the home that learns you: ready automations from the family's own days, one tap to accept.

Home Assistant already keeps three weeks of history. Once a night (and soon after a start) this looks at it, on the
home's own machine, for things the family does by hand again and again:

1. time: the same thing at about the same time on most days ("Kitchen and hall lights go off at about 11 PM most
   nights"). Weekdays and weekends are learned on their own, and a light that follows the sunset more than the clock
   is learned by the sunset ("Porch light comes on about 10 minutes after sunset").
2. after: one thing right after another ("When the garage opens after dark, the hall light comes on").

Only changes made by a person count: a tap in an app, a wall switch or a voice. What an automation or a script already
does is left alone, so an idea never copies an automation the home already has. Unlocking, garage doors and gates are
never suggested.

The wall shows one idea at a time as a tile, in one plain sentence, with a small preview of what would happen and how
often it happened ("11 of the last 14 nights"). "Yes" turns it on, and the integration runs it from then on: no YAML,
nothing to set up, and the Studio lists every habit with Stop.

Careful by default. Lights go off softly, and not while someone is still moving in that room (it waits up to half an
hour). Lights come on only when someone is home. Shades wait while someone is near them. After each run the tile offers
Undo for 30 minutes. When someone changes it back twice in a row, the habit pauses itself and says so.

Free and Pro: everyone sees the ideas and can keep one habit. Pro keeps as many as the home finds.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import logging
import re
import statistics
import time
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import PRO, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

HABITS = "hadash_habits"
STORAGE_KEY = "hadash.habits"
# Three weeks of history, and at least one week before the first idea.
DAYS = 21
MIN_DAYS = 7
# A day runs from 4 AM to 4 AM: lights off at 12:30 AM belong to the evening before.
DAY_START = 4 * 60
# "About the same time": within 25 minutes either way.
WINDOW = 25
# How often it must have happened: share of the days, and at least this many days.
NEED = {"all": (0.7, 8), "weekdays": (0.75, 6), "weekends": (0.75, 4)}
# An automation already did it on this many days near that time: leave it alone.
AUTO_DAYS = 3
# Things close in time with the same action become one idea ("Kitchen, hall and living lights").
MERGE = 15
MAX_THINGS = 8
MAX_IDEAS = 3
# "After": the second thing follows within 3 minutes (not at the same second: that is a group or a scene).
AFTER_MIN_S, AFTER_MAX_S = 3, 180
AFTER_NEED = (0.6, 6)
# Running carefully.
UNDO_S = 30 * 60
WATCH_FIX_S = 10 * 60
FIXES_TO_PAUSE = 2
QUIET_ROOM_S = 5 * 60
QUIET_SHADE_S = 2 * 60
WAIT_ROOM_S = 30 * 60
LEARN_AT = (4, 20)
START_LEARN_S = 10 * 60
MAX_ENTITIES = 160

SKIP_ID = re.compile(r"tablet|phone|iphone|ipad|fire_hd|watch|pixel|galaxy|macbook|screen|kiosk|hadash|backlight|led_indicator|child_lock|do_not_disturb", re.I)
SHADE_CLASSES = ("blind", "curtain", "shade", "shutter", "awning")
DOOR_CLASSES = ("door", "garage_door", "opening")

# action -> (domain service, state it leaves)
ACTIONS: dict[str, tuple[str, str]] = {
    "on": ("turn_on", "on"),
    "off": ("turn_off", "off"),
    "open": ("open_cover", "open"),
    "close": ("close_cover", "closed"),
    "lock": ("lock", "locked"),
}
STATE_ACTION = {"on": "on", "off": "off", "open": "open", "closed": "close", "locked": "lock"}


@dataclass
class Change:
    at: float
    state: str
    # "person" (an app or a voice), "other" (a wall switch, a remote), or "auto" (an automation or a script).
    by: str


def _local(ts: float) -> datetime:
    return dt_util.as_local(dt_util.utc_from_timestamp(ts))


def day_and_minute(ts: float) -> tuple[date, int]:
    """The home's day (4 AM to 4 AM) and the minute in it, after midnight: 12:30 AM is minute 1470 of the day before."""
    t = _local(ts)
    m = t.hour * 60 + t.minute
    if m < DAY_START:
        return t.date() - timedelta(days=1), m + 1440
    return t.date(), m


def in_days(d: date, days: str) -> bool:
    wd = d.weekday()
    return days == "all" or (days == "weekdays" and wd < 5) or (days == "weekends" and wd >= 5)


def clock(m: float) -> str:
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _round5(m: float) -> int:
    return int(5 * round(m / 5))


def _spread(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mid = statistics.median(values)
    return float(statistics.median([abs(v - mid) for v in values]))


def _best_window(per_day: dict[date, list[int]]) -> tuple[float, dict[date, int]]:
    """The time most days share: the centre, and each matching day's closest minute."""
    best: tuple[int, float, float, dict[date, int]] | None = None
    for minutes in per_day.values():
        for c in minutes:
            matched = {}
            for d, ms in per_day.items():
                near = min(ms, key=lambda m: abs(m - c))
                if abs(near - c) <= WINDOW:
                    matched[d] = near
            spread = _spread(list(matched.values()))
            if best is None or len(matched) > best[0] or (len(matched) == best[0] and spread < best[1]):
                best = (len(matched), spread, c, matched)
    if best is None:
        return 0.0, {}
    matched = best[3]
    centre = statistics.median(matched.values())
    # Once more around the true centre, so one early evening does not pull the window aside.
    again = {}
    for d, ms in per_day.items():
        near = min(ms, key=lambda m: abs(m - centre))
        if abs(near - centre) <= WINDOW:
            again[d] = near
    if len(again) >= len(matched):
        matched = again
        centre = statistics.median(matched.values())
    return float(centre), matched


def find_time_habits(
    changes: dict[str, list[Change]],
    days: list[date],
    sun: Callable[[date, str], int | None] | None = None,
    sun_ok: Callable[[str, str], bool] | None = None,
) -> list[dict[str, Any]]:
    """One entry per thing and action that happens at about the same time on most days.

    changes: each thing's changes, oldest first. days: the whole days that history covers (the home's days).
    sun(day, "sunset"|"sunrise"): that minute on that day. sun_ok(entity_id, action): may follow the sun.
    """
    found: list[dict[str, Any]] = []
    for entity_id, rows in changes.items():
        by_action: dict[str, dict[str, dict[date, list[int]]]] = defaultdict(lambda: {"person": defaultdict(list), "auto": defaultdict(list)})
        last = None
        for c in rows:
            if c.state == last:
                continue
            last = c.state
            action = STATE_ACTION.get(c.state)
            if action is None:
                continue
            d, m = day_and_minute(c.at)
            by_action[action]["auto" if c.by == "auto" else "person"][d].append(m)
        for action, sides in by_action.items():
            person, auto = sides["person"], sides["auto"]
            taken: list[str] = []
            for which in ("all", "weekdays", "weekends"):
                # Weekdays and weekends only when the whole week did not already fit.
                if which != "all" and "all" in taken:
                    break
                set_days = [d for d in days if in_days(d, which)]
                share, least = NEED[which]
                if len(set_days) < least:
                    continue
                per_day = {d: person[d] for d in set_days if person.get(d)}
                if len(per_day) < least:
                    continue
                centre, matched = _best_window(per_day)
                if len(matched) < least or len(matched) / len(set_days) < share:
                    continue
                if which == "all":
                    # "Every day" only when weekends do it too; otherwise weekdays and weekends are learned apart.
                    wd = [d for d in set_days if d.weekday() < 5]
                    we = [d for d in set_days if d.weekday() >= 5]
                    if (wd and sum(d in matched for d in wd) / len(wd) < 0.6) or (we and sum(d in matched for d in we) / len(we) < 0.5):
                        continue
                auto_near = sum(1 for d in set_days if any(abs(m - centre) <= WINDOW for m in auto.get(d, [])))
                if auto_near >= AUTO_DAYS:
                    continue
                item: dict[str, Any] = {
                    "kind": "time",
                    "entity_id": entity_id,
                    "action": action,
                    "days": which,
                    "minute": centre,
                    "seen": len(matched),
                    "of": len(set_days),
                    "strip": [d in matched for d in set_days[-14:]],
                }
                spread = _spread(list(matched.values()))
                if sun and sun_ok and sun_ok(entity_id, action) and spread >= 8:
                    for event in ("sunset", "sunrise"):
                        offsets = []
                        for d, m in matched.items():
                            s = sun(d, event)
                            if s is not None:
                                offsets.append(m - s)
                        if len(offsets) == len(matched) and _spread(offsets) <= spread * 0.6:
                            off = statistics.median(offsets)
                            if abs(off) <= 60:
                                item["sun"] = {"event": event, "offset": _round5(off)}
                                break
                found.append(item)
                taken.append(which)
    return found


def merge_time_habits(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Things with the same action at about the same time become one idea."""
    groups: list[dict[str, Any]] = []
    for f in sorted(found, key=lambda x: (x["action"], x["days"], str(x.get("sun", {}).get("event", "")), x["minute"])):
        key = (f["action"], f["days"], f.get("sun", {}).get("event"))
        g = groups[-1] if groups else None
        if g and g["_key"] == key and len(g["things"]) < MAX_THINGS:
            near = abs(f["sun"]["offset"] - g["sun"]["offset"]) <= MERGE if "sun" in f else abs(f["minute"] - g["_last"]) <= MERGE
            if near:
                g["things"].append(f["entity_id"])
                g["_minutes"].append(f["minute"])
                g["_last"] = f["minute"]
                g["seen"] = min(g["seen"], f["seen"])
                g["strip"] = [a and b for a, b in zip(g["strip"], f["strip"])] if len(g["strip"]) == len(f["strip"]) else g["strip"]
                if "sun" in f:
                    g["_offsets"].append(f["sun"]["offset"])
                continue
        groups.append(
            {
                "_key": key,
                "_last": f["minute"],
                "_minutes": [f["minute"]],
                "_offsets": [f["sun"]["offset"]] if "sun" in f else [],
                "kind": "time",
                "action": f["action"],
                "days": f["days"],
                "things": [f["entity_id"]],
                "seen": f["seen"],
                "of": f["of"],
                "strip": list(f["strip"]),
                **({"sun": dict(f["sun"])} if "sun" in f else {}),
            }
        )
    out = []
    for g in groups:
        minutes = g.pop("_minutes")
        # Off and lock: the latest of the group, so nobody is left in the dark early. On: the earliest.
        minute = max(minutes) if g["action"] in ("off", "lock") else min(minutes) if g["action"] == "on" else statistics.median(minutes)
        offsets = g.pop("_offsets")
        g.pop("_key")
        g.pop("_last")
        g["at"] = clock(_round5(minute))
        if offsets:
            g["sun"]["offset"] = _round5(statistics.median(offsets))
        out.append(g)
    return out


def find_after_habits(
    triggers: dict[str, list[tuple[float, bool]]],
    changes: dict[str, list[Change]],
) -> list[dict[str, Any]]:
    """"When the garage opens (after dark), the hall light comes on": a thing a person does right after another.

    triggers: each trigger's moments, (when, dark). changes: the things a person may change after it.
    """
    found: list[dict[str, Any]] = []
    for trig_id, moments in triggers.items():
        if len(moments) < AFTER_NEED[1]:
            continue
        for entity_id, rows in changes.items():
            if entity_id == trig_id:
                continue
            person = [c for c in rows if c.by != "auto"]
            for action in ("on", "off"):
                state = ACTIONS[action][1]
                times = [c.at for c in person if c.state == state]
                if len(times) < AFTER_NEED[1]:
                    continue
                follows = {True: 0, False: 0}
                total = {True: 0, False: 0}
                led = 0
                for at, dark in moments:
                    total[dark] += 1
                    if any(AFTER_MIN_S < t - at <= AFTER_MAX_S for t in times):
                        follows[dark] += 1
                for t in times:
                    if any(AFTER_MIN_S < t - at <= AFTER_MAX_S for at, _ in moments):
                        led += 1
                share, least = AFTER_NEED
                both = follows[True] + follows[False]
                all_n = total[True] + total[False]
                dark_only = None
                if both >= least and both / all_n >= share:
                    dark_only = False
                elif follows[True] >= least and total[True] and follows[True] / total[True] >= share and (not total[False] or follows[False] / total[False] < 0.3):
                    dark_only = True
                if dark_only is None or led / len(times) < 0.4:
                    continue
                seen = follows[True] if dark_only else both
                of = total[True] if dark_only else all_n
                found.append({"kind": "after", "trigger": trig_id, "dark": dark_only, "action": action, "entity_id": entity_id, "seen": seen, "of": of})
    # One idea per trigger, action and darkness: the hall and the porch light together.
    groups: dict[tuple[str, bool, str], dict[str, Any]] = {}
    for f in found:
        key = (f["trigger"], f["dark"], f["action"])
        g = groups.get(key)
        if g is None:
            groups[key] = {"kind": "after", "trigger": f["trigger"], "dark": f["dark"], "action": f["action"], "things": [f["entity_id"]], "seen": f["seen"], "of": f["of"]}
        elif len(g["things"]) < MAX_THINGS:
            g["things"].append(f["entity_id"])
            g["seen"] = min(g["seen"], f["seen"])
    return list(groups.values())


def idea_id(idea: dict[str, Any]) -> str:
    sig = "|".join([idea["kind"], idea["action"], idea.get("days", ""), idea.get("trigger", ""), str(idea.get("dark", "")), ",".join(sorted(idea["things"]))])
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


def thing_keys(idea: dict[str, Any]) -> list[str]:
    """One key per thing, so a "No thanks" also covers the same light in a later, bigger idea."""
    base = "|".join([idea["kind"], idea["action"], idea.get("days", "all") if idea["kind"] == "time" else idea.get("trigger", "")])
    return [f"{base}|{t}" for t in idea["things"]]


class Habits:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.ideas: list[dict[str, Any]] = []
        self.habits: list[dict[str, Any]] = []
        # thing key -> 0 (never) or until when (later).
        self.declined: dict[str, float] = {}
        self.learned_at = 0.0
        self.days_known = 0
        self.last_run: dict[str, Any] | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._unsubs: list[Any] = []
        self._after_unsub: Any = None
        self._ours: set[str] = set()
        self._waiting: set[str] = set()
        self._learning = False

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.habits = list(data.get("habits", []))
        self.ideas = list(data.get("ideas", []))
        self.declined = {k: float(v) for k, v in data.get("declined", {}).items()}
        self.learned_at = float(data.get("learned_at", 0))
        self.days_known = int(data.get("days_known", 0))

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"habits": self.habits, "ideas": self.ideas, "declined": self.declined, "learned_at": self.learned_at, "days_known": self.days_known},
            2,
        )

    # What the screens see.

    def _thing(self, entity_id: str) -> dict[str, str]:
        out = {"id": entity_id, "name": self._name(entity_id)}
        area = self._area(entity_id)
        if area:
            out["area"] = area
        return out

    def _show(self, x: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in x.items() if not k.startswith("_")}
        out["things"] = [self._thing(t) for t in x["things"]]
        if x.get("trigger"):
            out["trigger"] = {**self._thing(x["trigger"]), "what": self._trigger_what(x["trigger"])}
        return out

    def state(self) -> dict[str, Any]:
        last = None
        if self.last_run and time.time() - self.last_run["at"] < UNDO_S:
            last = {k: v for k, v in self.last_run.items() if k != "before"}
            last["names"] = [self._name(e) for e in self.last_run["before"]]
        return {
            "ideas": [self._show(i) for i in self.ideas[:MAX_IDEAS]],
            "habits": [self._show(h) for h in self.habits],
            "last": last,
            "learned_at": self.learned_at,
            "days_known": self.days_known,
            "need_days": MIN_DAYS,
            "free_left": max(0, 1 - len(self.habits)) if not self._pro() else None,
        }

    def _tell(self) -> None:
        s = self.state()
        for listener in list(self._listeners):
            listener(s)

    def _pro(self) -> bool:
        manager = self.hass.data.get(PRO)
        if manager is None:
            return True
        try:
            return manager.state().get("plan") in ("pro", "trial")
        except Exception:  # noqa: BLE001 - a license problem never stops a habit
            return True

    # Names, the way people say them.

    def _name(self, entity_id: str) -> str:
        s = self.hass.states.get(entity_id)
        entry = er.async_get(self.hass).async_get(entity_id)
        name = s.attributes.get("friendly_name") if s is not None else None
        if not name and entry is not None:
            name = entry.name or entry.original_name
        return str(name or entity_id.split(".", 1)[-1].replace("_", " ").capitalize())

    def _area_id(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        area_id = entry.area_id if entry else None
        if not area_id and entry and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        return area_id

    def _area(self, entity_id: str) -> str | None:
        area_id = self._area_id(entity_id)
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return area.name if area else None

    def _trigger_what(self, entity_id: str) -> str:
        domain = entity_id.split(".")[0]
        if domain == "person":
            return "home"
        if domain == "cover":
            return "garage"
        return "open"

    # Which things may become habits.

    def _candidates(self) -> tuple[list[str], list[str]]:
        reg = er.async_get(self.hass)
        things, triggers = [], []
        for s in self.hass.states.async_all():
            eid = s.entity_id
            domain = eid.split(".")[0]
            entry = reg.async_get(eid)
            if entry is not None and (entry.hidden_by or entry.disabled_by or entry.entity_category or entry.platform == "hadash"):
                continue
            if SKIP_ID.search(eid):
                continue
            dc = s.attributes.get("device_class")
            if domain in ("light", "fan"):
                things.append(eid)
            elif domain == "switch" and dc in (None, "outlet", "switch"):
                things.append(eid)
            elif domain == "cover":
                if dc in SHADE_CLASSES:
                    things.append(eid)
                elif dc in ("garage", "gate"):
                    triggers.append(eid)
            elif domain == "lock":
                things.append(eid)
            elif domain == "binary_sensor" and dc in DOOR_CLASSES:
                triggers.append(eid)
            elif domain == "person":
                triggers.append(eid)
        return things[:MAX_ENTITIES], triggers[:40]

    def _allowed(self, entity_id: str, action: str) -> bool:
        domain = entity_id.split(".")[0]
        if domain == "lock":
            return action == "lock"
        if domain == "cover":
            return action in ("open", "close")
        return action in ("on", "off")

    # Learning.

    async def _read(self, ids: list[str], start: datetime) -> dict[str, list[Change]]:
        """Each thing's changes since start, with who made them. Home Assistant's own database, on this machine."""
        from homeassistant.components.recorder import get_instance

        start_ts = start.timestamp()

        def with_context() -> dict[str, list[Change]]:
            from sqlalchemy import text

            from homeassistant.components.recorder.util import session_scope

            out: dict[str, list[Change]] = defaultdict(list)
            names = {f"e{i}": e for i, e in enumerate(ids)}
            q = text(
                "SELECT sm.entity_id, s.state, s.last_updated_ts, s.context_user_id_bin, s.context_parent_id_bin "
                "FROM states s JOIN states_meta sm ON s.metadata_id = sm.metadata_id "
                f"WHERE sm.entity_id IN ({', '.join(':' + k for k in names)}) AND s.last_updated_ts >= :start "
                "AND (s.last_changed_ts IS NULL OR s.last_changed_ts = s.last_updated_ts) "
                "ORDER BY s.last_updated_ts"
            )
            with session_scope(hass=self.hass, read_only=True) as session:
                for eid, state, ts, user, parent in session.execute(q, {**names, "start": start_ts}):
                    if ts is None or state in (None, "unavailable", "unknown"):
                        continue
                    out[eid].append(Change(float(ts), str(state), "auto" if parent else "person" if user else "other"))
            return out

        def plain() -> dict[str, list[Change]]:
            from homeassistant.components.recorder import history

            out: dict[str, list[Change]] = defaultdict(list)
            got = history.get_significant_states(self.hass, start, None, ids, include_start_time_state=False, significant_changes_only=False, minimal_response=False, no_attributes=True)
            for eid, states in got.items():
                for s in states:
                    if isinstance(s, State) and s.state not in ("unavailable", "unknown"):
                        out[eid].append(Change(s.last_changed.timestamp(), s.state, "other"))
            return out

        if not ids:
            return {}
        try:
            instance = get_instance(self.hass)
        except (KeyError, RuntimeError):
            return {}
        try:
            return await instance.async_add_executor_job(with_context)
        except Exception as err:  # noqa: BLE001 - an older or another database: no "who", the times still help
            _LOGGER.debug("Habits: reading who changed what failed (%s), using plain history", err)
        try:
            return await instance.async_add_executor_job(plain)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Habits: history failed: %s", err)
            return {}

    def _sun_minute(self, d: date, event: str) -> int | None:
        from homeassistant.helpers.sun import get_astral_event_date

        try:
            at = get_astral_event_date(self.hass, event, d)
        except Exception:  # noqa: BLE001
            return None
        if at is None:
            return None
        t = dt_util.as_local(at)
        return t.hour * 60 + t.minute

    def _dark(self, ts: float) -> bool:
        t = _local(ts)
        rise, fall = self._sun_minute(t.date(), "sunrise"), self._sun_minute(t.date(), "sunset")
        if rise is None or fall is None:
            return t.hour >= 18 or t.hour < 7
        m = t.hour * 60 + t.minute
        return m < rise or m > fall

    async def learn(self) -> None:
        if self._learning:
            return
        self._learning = True
        try:
            await self._learn()
        finally:
            self._learning = False

    async def _learn(self) -> None:
        things, triggers = self._candidates()
        now = dt_util.now()
        start = now - timedelta(days=DAYS + 1)
        changes = await self._read(things + triggers, start)
        first = min((c.at for rows in changes.values() for c in rows), default=None)
        today, _m = day_and_minute(now.timestamp())
        first_day = day_and_minute(first)[0] + timedelta(days=1) if first is not None else today
        days = [first_day + timedelta(days=i) for i in range((today - first_day).days)][-DAYS:]
        self.days_known = len(days)
        self.learned_at = time.time()
        ideas: list[dict[str, Any]] = []
        if len(days) >= MIN_DAYS:
            first_ts = dt_util.as_utc(datetime.combine(days[0], datetime.min.time(), tzinfo=dt_util.get_default_time_zone())).timestamp() + DAY_START * 60
            end_ts = first_ts + len(days) * 86400
            thing_changes = {e: [c for c in changes.get(e, []) if first_ts <= c.at < end_ts] for e in things}
            thing_changes = {e: v for e, v in thing_changes.items() if v}

            def sun_ok(entity_id: str, action: str) -> bool:
                return entity_id.split(".")[0] in ("light", "switch", "cover")

            found = [f for f in find_time_habits(thing_changes, days, self._sun_minute, sun_ok) if self._allowed(f["entity_id"], f["action"])]
            ideas += merge_time_habits(found)
            trig_moments: dict[str, list[tuple[float, bool]]] = {}
            for t in triggers:
                rows, last = [], None
                want = "home" if t.startswith("person.") else "open" if t.startswith("cover.") else "on"
                for c in changes.get(t, []):
                    if c.state != last and c.state == want and first_ts <= c.at < end_ts:
                        rows.append((c.at, self._dark(c.at)))
                    last = c.state
                if rows:
                    trig_moments[t] = rows
            lights = {e: v for e, v in thing_changes.items() if e.split(".")[0] in ("light", "switch")}
            ideas += find_after_habits(trig_moments, lights)
        self.ideas = self._fresh(ideas)
        self._save()
        self._tell()

    def _fresh(self, ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Leave out what was declined or is already a habit, and put the surest, biggest first."""
        now = time.time()
        self.declined = {k: v for k, v in self.declined.items() if v == 0 or v > now}
        taken = {k for h in self.habits for k in thing_keys(h)}
        out = []
        for idea in ideas:
            keys = thing_keys(idea)
            keep = [t for t, k in zip(idea["things"], keys) if k not in self.declined and k not in taken]
            if not keep:
                continue
            idea = {**idea, "things": keep}
            idea["id"] = idea_id(idea)
            out.append(idea)
        out.sort(key=lambda i: (-(i["seen"] / max(1, i["of"])), -len(i["things"]), i["kind"] != "time"))
        return out

    # Answers from the wall.

    def accept(self, idea_key: str) -> dict[str, Any]:
        idea = next((i for i in self.ideas if i["id"] == idea_key), None)
        if idea is None:
            return {"ok": False}
        if not self._pro() and len(self.habits) >= 1:
            return {"ok": False, "pro": True}
        habit = {**idea, "since": time.time(), "fixes": 0, "paused": False}
        self.habits.append(habit)
        self.ideas = [i for i in self.ideas if i["id"] != idea_key]
        self._save()
        self._watch_after()
        self._tell()
        return {"ok": True, "id": habit["id"]}

    def decline(self, idea_key: str, later: bool) -> None:
        idea = next((i for i in self.ideas if i["id"] == idea_key), None)
        if idea is None:
            return
        until = time.time() + 7 * 86400 if later else 0
        for k in thing_keys(idea):
            self.declined[k] = until
        self.ideas = [i for i in self.ideas if i["id"] != idea_key]
        self._save()
        self._tell()

    def stop(self, habit_id: str) -> None:
        """Stop a habit: it goes back to being an idea the next night, unless "No thanks" is said then."""
        self.habits = [h for h in self.habits if h["id"] != habit_id]
        self._save()
        self._watch_after()
        self._tell()

    def pause(self, habit_id: str, paused: bool) -> None:
        for h in self.habits:
            if h["id"] == habit_id:
                h["paused"] = paused
                h["fixes"] = 0
                h.pop("paused_by", None)
        self._save()
        self._tell()

    def forget(self) -> None:
        """The Studio's "Forget the No thanks": declined ideas may come back."""
        self.declined = {}
        self._save()
        self.hass.async_create_task(self.learn())

    async def undo(self, habit_id: str | None) -> bool:
        run = self.last_run
        if not run or time.time() - run["at"] > UNDO_S or (habit_id and run["id"] != habit_id):
            return False
        self.last_run = None
        for eid, was in run["before"].items():
            await self._set(eid, was)
        self._fixed(run["id"])
        self._tell()
        return True

    async def _set(self, entity_id: str, was: dict[str, Any]) -> None:
        domain = entity_id.split(".")[0]
        state = was.get("state")
        data: dict[str, Any] = {"entity_id": entity_id}
        if domain == "cover":
            service = "open_cover" if state == "open" else "close_cover"
            if was.get("position") is not None:
                service, data["position"] = "set_cover_position", was["position"]
        elif domain == "lock":
            service = "unlock" if state == "unlocked" else "lock"
        else:
            service = "turn_on" if state == "on" else "turn_off"
            if domain == "light" and state == "on" and was.get("brightness"):
                data["brightness"] = was["brightness"]
        ctx = Context()
        self._ours.add(ctx.id)
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True, context=ctx)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Habits: %s.%s %s failed: %s", domain, service, entity_id, err)

    def _fixed(self, habit_id: str) -> None:
        """Someone changed it back: twice in a row pauses the habit."""
        for h in self.habits:
            if h["id"] == habit_id:
                h["fixes"] = h.get("fixes", 0) + 1
                if h["fixes"] >= FIXES_TO_PAUSE:
                    h["paused"] = True
                    h["paused_by"] = "fixes"
        self._save()

    # Running.

    def start(self) -> None:
        @callback
        def minute(now: datetime) -> None:
            self.hass.async_create_task(self._minute(now))

        self._unsubs.append(async_track_time_change(self.hass, minute, second=0))

        @callback
        def nightly(_now: datetime) -> None:
            self.hass.async_create_task(self.learn())

        self._unsubs.append(async_track_time_change(self.hass, nightly, hour=LEARN_AT[0], minute=LEARN_AT[1], second=0))
        @callback
        def soon(_now: datetime) -> None:
            self.hass.async_create_task(self.learn())

        if time.time() - self.learned_at > 20 * 3600:
            self._unsubs.append(async_call_later(self.hass, START_LEARN_S, soon))
        self._watch_after()

        @callback
        def on_stop(_event: Event) -> None:
            self.shutdown()

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

    def shutdown(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
        if self._after_unsub:
            self._after_unsub()
            self._after_unsub = None

    def due(self, h: dict[str, Any], now: datetime) -> bool:
        """A time habit's minute has come."""
        if h["kind"] != "time" or h.get("paused"):
            return False
        d, m = day_and_minute(now.timestamp())
        if not in_days(d, h["days"]) or h.get("ran") == d.isoformat():
            return False
        if "sun" in h:
            s = self._sun_minute(d, h["sun"]["event"])
            if s is None:
                return False
            target = s + h["sun"]["offset"]
        else:
            hh, mm = (int(x) for x in h["at"].split(":"))
            target = hh * 60 + mm
            if target < DAY_START:
                target += 1440
        return m == target

    async def _minute(self, now: datetime) -> None:
        for h in list(self.habits):
            if self.due(h, now):
                h["ran"] = day_and_minute(now.timestamp())[0].isoformat()
                self._save()
                await self.run(h)

    def _someone_home(self) -> bool:
        people = self.hass.states.async_entity_ids("person")
        return not people or any((s := self.hass.states.get(p)) is not None and s.state == "home" for p in people)

    def _moving_near(self, entity_id: str, within_s: float) -> bool:
        """A motion, occupancy or presence sensor in the same room saw someone this recently (or still does)."""
        area_id = self._area_id(entity_id)
        if not area_id:
            return False
        now = time.time()
        for s in self.hass.states.async_all("binary_sensor"):
            if s.attributes.get("device_class") not in ("motion", "occupancy", "presence"):
                continue
            if self._area_id(s.entity_id) != area_id:
                continue
            if s.state == "on" or (s.state == "off" and now - s.last_changed.timestamp() < within_s):
                return True
        return False

    async def run(self, h: dict[str, Any], waited: float = 0) -> None:
        """Do the habit, carefully, and keep what was before for Undo."""
        if h.get("paused") or h not in self.habits:
            return
        action = h["action"]
        if action == "on" and not self._someone_home():
            return
        service, leaves = ACTIONS[action]
        todo = []
        for eid in h["things"]:
            s = self.hass.states.get(eid)
            if s is None or s.state in ("unavailable", "unknown") or s.state == leaves:
                continue
            todo.append(eid)
        if not todo:
            return
        # Off and shades: not while someone is still in the room. Wait, then try again, up to half an hour.
        busy = [e for e in todo if (action == "off" and self._moving_near(e, QUIET_ROOM_S)) or (e.startswith("cover.") and self._moving_near(e, QUIET_SHADE_S))]
        if busy:
            if h["id"] in self._waiting:
                return
            if waited >= WAIT_ROOM_S:
                # Still busy after half an hour: the rest of the house, not that room.
                todo = [e for e in todo if e not in busy]
            else:
                self._waiting.add(h["id"])

                @callback
                def again(_now: Any) -> None:
                    self._waiting.discard(h["id"])
                    self.hass.async_create_task(self.run(h, waited + 5 * 60))

                async_call_later(self.hass, 5 * 60, again)
                return
        if not todo:
            return
        before: dict[str, dict[str, Any]] = {}
        for eid in todo:
            s = self.hass.states.get(eid)
            if s is not None:
                before[eid] = {"state": s.state, "brightness": s.attributes.get("brightness"), "position": s.attributes.get("current_position")}
        ctx = Context()
        self._ours.add(ctx.id)
        by_domain: dict[str, list[str]] = defaultdict(list)
        for eid in todo:
            by_domain[eid.split(".")[0]].append(eid)
        for domain, ids in by_domain.items():
            data: dict[str, Any] = {"entity_id": ids}
            if domain == "light" and action == "off":
                # Softly, so it never feels like a power cut.
                data["transition"] = 3
            try:
                await self.hass.services.async_call(domain, service, data, blocking=False, context=ctx)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Habits: %s.%s failed: %s", domain, service, err)
        self.last_run = {"id": h["id"], "at": time.time(), "action": action, "before": before}
        h["last"] = time.time()
        self._save()
        self._watch_fix(h, before)
        self._tell()

    def _watch_fix(self, h: dict[str, Any], before: dict[str, dict[str, Any]]) -> None:
        """For 10 minutes: if a person puts it back the way it was, the habit was wrong today."""
        ids = set(before)
        done = {"x": False}

        @callback
        def changed(event: Event) -> None:
            new: State | None = event.data.get("new_state")
            if done["x"] or new is None or new.entity_id not in ids:
                return
            if new.context.id in self._ours or new.context.parent_id:
                return
            if new.state == before[new.entity_id]["state"]:
                done["x"] = True
                self._fixed(h["id"])
                self._tell()
                stop()

        @callback
        def only_these(data: Any) -> bool:
            return data["entity_id"] in ids

        unsub = self.hass.bus.async_listen("state_changed", changed, event_filter=only_these)

        @callback
        def stop() -> None:
            nonlocal unsub
            if unsub:
                unsub()
                unsub = None

        @callback
        def ended(_now: Any) -> None:
            if not done["x"]:
                # A clean run: the count of fixes starts again.
                h["fixes"] = 0
                self._save()
            stop()

        async_call_later(self.hass, WATCH_FIX_S, ended)

    def _watch_after(self) -> None:
        if self._after_unsub:
            self._after_unsub()
            self._after_unsub = None
        triggers = {h["trigger"] for h in self.habits if h["kind"] == "after"}
        if not triggers:
            return

        @callback
        def changed(event: Event) -> None:
            new: State | None = event.data.get("new_state")
            old: State | None = event.data.get("old_state")
            if new is None or (old is not None and old.state == new.state):
                return
            for h in self.habits:
                if h["kind"] != "after" or h["trigger"] != new.entity_id or h.get("paused"):
                    continue
                want = {"home": "home", "garage": "open"}.get(self._trigger_what(h["trigger"]), "on")
                if new.state != want or (h.get("dark") and not self._dark(time.time())):
                    continue
                self.hass.async_create_task(self.run(h))

        @callback
        def only_triggers(data: Any) -> bool:
            return data["entity_id"] in triggers

        self._after_unsub = self.hass.bus.async_listen("state_changed", changed, event_filter=only_triggers)


async def async_setup(hass: HomeAssistant) -> None:
    habits = Habits(hass)
    await habits.async_load()
    hass.data[HABITS] = habits
    habits.start()
    for command in (ws_subscribe, ws_accept, ws_decline, ws_stop, ws_pause, ws_undo, ws_learn, ws_forget):
        websocket_api.async_register_command(hass, command)


# Like Home moments, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    habits: Habits = hass.data[HABITS]

    @callback
    def forward(state: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], state))

    habits._listeners.append(forward)

    @callback
    def unsub() -> None:
        if forward in habits._listeners:
            habits._listeners.remove(forward)

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    forward(habits.state())


ID = vol.All(str, vol.Length(min=1, max=40))


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/accept", vol.Required("habit"): ID})
@callback
def ws_accept(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], hass.data[HABITS].accept(msg["habit"]))


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/decline", vol.Required("habit"): ID, vol.Optional("later", default=False): bool})
@callback
def ws_decline(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[HABITS].decline(msg["habit"], msg["later"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/stop", vol.Required("habit"): ID})
@callback
def ws_stop(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[HABITS].stop(msg["habit"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/pause", vol.Required("habit"): ID, vol.Required("paused"): bool})
@callback
def ws_pause(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[HABITS].pause(msg["habit"], msg["paused"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/undo", vol.Optional("habit"): vol.Any(None, ID)})
@websocket_api.async_response
async def ws_undo(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"ok": await hass.data[HABITS].undo(msg.get("habit"))})


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/learn"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_learn(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    await hass.data[HABITS].learn()
    connection.send_result(msg["id"], hass.data[HABITS].state())


@websocket_api.websocket_command({vol.Required("type"): "hadash/habits/forget"})
@websocket_api.require_admin
@callback
def ws_forget(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    hass.data[HABITS].forget()
    connection.send_result(msg["id"])
