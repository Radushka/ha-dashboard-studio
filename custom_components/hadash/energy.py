"""Energy without a meter (free): what each device used this week, guessed from its on and off times.

Most homes have no energy meter in Home Assistant, but Home Assistant knows when each light, TV, heater or plug
was on. So this part guesses:

1. Each device gets a typical power by its kind: an LED light 9 W (less when dimmed), a TV 100 W, a space heater
   1500 W, a hot tub 3000 W, and so on. Kinds come from the domain, the device class, the integration and plain
   words in the name ("heater", "dehumidifier", "tree"). A device we cannot place is left out, never guessed wild.
2. A device with its own energy sensor (a smart plug that measures) is measured, not guessed: its real kWh count,
   and its switch is not guessed again.
3. The on times of the last 7 days (Home Assistant's history) times the power give kWh per device. Lights add up
   per room ("Kitchen lights").
4. Away and night: the hours a device was on while nobody was home (person entities), and lights on between
   1 and 5 in the morning.
5. One plain tip each week, chosen on the first look of the week: the biggest thing that could be saved (left on
   while away, on all night, on all the time, or the biggest user). The same tip is not said two weeks in a row
   when another one fits. A calm week gets a kind word instead.
6. Money: the price from Home Assistant's Energy settings when there is one, else a typical price for the
   home's country, else no money at all (kWh only).

Everything is an estimate and the screens say so ("about"). The tablets ask with hadash/energy/guess. The guess is
made again every 6 hours, in the recorder's own thread, so it never slows Home Assistant down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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

ENERGY = "hadash_energy"
STORAGE_KEY = "hadash.energy"
WEEK = timedelta(days=7)
EVERY = timedelta(hours=6)
FIRST_LOOK_S = 90
NIGHT_FROM, NIGHT_TO = 1, 5
KEEP_DAYS = 45
TOP = 6

# Typical power (W) when on. Kept low rather than high: a guess should never scare anyone.
LIGHT_W = 9
TV_W = 100
SPEAKER_W = 6
COOL_W, HEAT_W, FAN_ONLY_W = 2500, 500, 300
# Whole words only: "outdoor" is not a door, and "tv" in "living_tv" is a TV.
L, R = r"(?<![a-zà-ÿ])", r"(?![a-zà-ÿ])"


def _w(*words: str) -> re.Pattern[str]:
    return re.compile(L + "(?:" + "|".join(words) + ")", re.I)


WORDS: list[tuple[re.Pattern[str], str, float]] = [
    (_w(r"hot.?tub", r"spa" + R, "jacuzzi", "whirlpool", r"pool.?heat", "sauna"), "spa", 3000),
    (_w("dehumid", "déshumid", "deshumid", "entfeucht"), "dryair", 300),
    (_w("humidif", "befeucht"), "air", 40),
    (_w("heater", r"space.?heat", "radiator", "baseboard", r"floor.?heat", "chauffage", "radiateur", "heizung", "heizlüfter", "calefactor", "estufa"), "heat", 1500),
    (_w("kettle", "bouilloire", "wasserkocher", "hervidor"), "kettle", 1500),
    (_w("coffee", "espresso", "café", "cafe" + R, "kaffee", "cafetera"), "coffee", 1000),
    (_w("pump", "pompe", "pumpe", "bomba", "pool" + R), "pump", 800),
    (_w("aquarium", r"fish.?tank", "terrarium"), "aquarium", 60),
    (_w("monitor", "computer", "pc" + R, "desktop", "printer", "gaming", "xbox", "playstation", "ps5"), "computer", 80),
    (_w("tv" + R, "television", "télé", "tele" + R, "fernseher"), "tv", TV_W),
    (_w("tree", "christmas", "xmas", "noel", "noël", "weihnacht", "navidad", "string", "fairy", "garland", "guirlande", "lichterkette"), "tree", 30),
    (_w("fan" + R, "ventilat", "ventilo", "lüfter"), "fan", 40),
    (_w("lamp", "lampe", "lámpara", "light", "lumi", "licht", "luz"), "lamp", LIGHT_W),
]
# Never guessed: they run all the time by design, or their on time says nothing about their use.
NEVER = _w(
    "fridge", "freezer", "frigo", "refrigerat", "kühl", "congel", "nevera", "router", "modem", "network", "nas" + R, "server",
    "charger", "chargeur", "wallbox", "ev" + R, "car" + R, "washer", "dryer", "dishwasher", "alarm", "siren", "camera",
    "lock" + R, "valve", "sprinkler", "irrigation", "garage", "gate" + R, "door", "motion", "privacy", "mute", "led" + R,
    "indicator", "status", "auto", "mode" + R, "update", "guest", "detect", "notif", "record", "do.?not.?disturb",
)
TV_PLATFORMS = {"braviatv", "webostv", "samsungtv", "philips_js", "vizio", "panasonic_viera", "lg_netcast", "hisense_tv", "tcl", "roku_tv", "androidtv_remote", "bravia"}
SPEAKER_PLATFORMS = {"sonos", "cast", "heos", "bluesound", "yamaha_musiccast", "linkplay", "bose", "denonavr", "squeezebox", "music_assistant"}
SKIP_PLATFORMS = {"hadash", "browser_mod", "mobile_app", "group", "template", "universal", "fully_kiosk", "alexa_media", "dlna_dmr", "kodi", "plex", "jellyfin", "emby", "spotify", "apple_tv", "androidtv", "firetv"}
ON_TV = {"on", "playing", "paused", "idle", "buffering"}
AWAY_KINDS_SKIP = {"aquarium", "climate", "spa", "pump"}
ALWAYS_KINDS = {"heat", "dryair", "air", "fan", "plug", "lamp", "tree", "computer", "spa", "pump", "coffee"}
# A typical price per kWh by country, in the country's own money: used only when the Energy settings have none and
# Home Assistant's currency is that country's.
TYPICAL_PRICE: dict[str, tuple[str, float]] = {
    "CA": ("CAD", 0.15), "US": ("USD", 0.17), "GB": ("GBP", 0.25), "IE": ("EUR", 0.35), "DE": ("EUR", 0.38),
    "FR": ("EUR", 0.25), "ES": ("EUR", 0.20), "IT": ("EUR", 0.30), "NL": ("EUR", 0.30), "BE": ("EUR", 0.35),
    "AT": ("EUR", 0.30), "PT": ("EUR", 0.22), "LU": ("EUR", 0.25), "FI": ("EUR", 0.15), "CH": ("CHF", 0.32),
    "SE": ("SEK", 2.0), "NO": ("NOK", 1.5), "DK": ("DKK", 2.5), "PL": ("PLN", 1.0), "AU": ("AUD", 0.32),
    "NZ": ("NZD", 0.33), "MD": ("MDL", 3.0), "RO": ("RON", 1.3),
}


@dataclass
class Profile:
    """What a device is and its typical power when on."""

    kind: str
    watts: float


def profile(entity_id: str, name: str, attrs: dict[str, Any], platform: str | None = None) -> Profile | None:
    """The kind and the typical power of one entity, or None when we should not guess it."""
    domain, key = entity_id.split(".", 1)
    words = f"{key} {name}"
    if platform in SKIP_PLATFORMS:
        return None
    if domain == "light":
        # Groups (a Hue room, a light group) would count their lights twice.
        if attrs.get("entity_id") or attrs.get("is_hue_group") or attrs.get("lights"):
            return None
        return Profile("lights", LIGHT_W)
    if domain == "media_player":
        if attrs.get("device_class") == "tv" or platform in TV_PLATFORMS:
            return Profile("tv", TV_W)
        if attrs.get("device_class") == "speaker" or platform in SPEAKER_PLATFORMS:
            # A group of speakers is the same speakers again.
            if len(attrs.get("group_members") or []) > 1 and attrs.get("group_members", [entity_id])[0] != entity_id:
                return None
            return Profile("speaker", SPEAKER_W)
        return None
    if domain == "climate":
        return Profile("climate", 0)
    if domain == "fan":
        return Profile("fan", 40)
    if domain == "humidifier":
        return Profile("dryair", 300) if attrs.get("device_class") == "dehumidifier" else Profile("air", 40)
    if domain == "switch":
        if NEVER.search(words):
            return None
        for pattern, kind, watts in WORDS:
            if pattern.search(words):
                return Profile(kind, watts)
        if attrs.get("device_class") == "outlet":
            return Profile("plug", 50)
    return None


def watts_now(p: Profile, state: str, attrs: dict[str, Any]) -> float:
    """The guessed power of a device in this state."""
    if state in ("unavailable", "unknown", "off", "standby"):
        return 0.0
    if p.kind == "lights":
        if state != "on":
            return 0.0
        b = attrs.get("brightness")
        return p.watts * max(0.2, b / 255) if isinstance(b, (int, float)) else p.watts
    if p.kind == "tv":
        return p.watts if state in ON_TV else 0.0
    if p.kind == "speaker":
        return p.watts if state == "playing" else 0.0
    if p.kind == "climate":
        action = attrs.get("hvac_action")
        return {"cooling": COOL_W, "heating": HEAT_W, "fan": FAN_ONLY_W, "drying": FAN_ONLY_W}.get(action, 0.0)
    return p.watts if state == "on" else 0.0


def overlap(t0: float, t1: float, spans: list[tuple[float, float]]) -> float:
    """Seconds of [t0, t1] inside any of these spans (sorted, not overlapping)."""
    total = 0.0
    for a, b in spans:
        if b <= t0:
            continue
        if a >= t1:
            break
        total += min(b, t1) - max(a, t0)
    return total


def nights(start: datetime, end: datetime) -> list[tuple[float, float]]:
    """1 to 5 in the morning, local time, for each night in the window."""
    out = []
    day = dt_util.as_local(start).replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        a = day.replace(hour=NIGHT_FROM).timestamp()
        b = day.replace(hour=NIGHT_TO).timestamp()
        if b > start.timestamp() and a < end.timestamp():
            out.append((max(a, start.timestamp()), min(b, end.timestamp())))
        day = day + timedelta(days=1)
    return out


def away_spans(people: list[list[tuple[float, str]]], start: float, end: float) -> list[tuple[float, float]]:
    """When nobody was home: each person is a list of (time, state). Empty when there are no people."""
    if not people:
        return []
    times = sorted({start, end, *(t for p in people for t, _ in p if start < t < end)})

    def at(p: list[tuple[float, str]], when: float) -> str | None:
        found = None
        for t, s in p:
            if t <= when:
                found = s
            else:
                break
        return found

    spans: list[tuple[float, float]] = []
    for a, b in zip(times, times[1:]):
        states = [at(p, a) for p in people]
        # Unknown people (no history yet) count as home, so a new person never makes the house "empty".
        if all(s is not None and s not in ("home", "unknown", "unavailable") for s in states):
            if spans and spans[-1][1] == a:
                spans[-1] = (spans[-1][0], b)
            else:
                spans.append((a, b))
    return spans


@dataclass
class Use:
    """What one device (or one room of lights) used in the window."""

    key: str
    kind: str
    ids: list[str]
    area: str | None = None
    kwh: float = 0.0
    on_s: float = 0.0
    away_kwh: float = 0.0
    away_s: float = 0.0
    night_kwh: float = 0.0
    night_count: int = 0
    measured: bool = False
    days: dict[str, float] = field(default_factory=dict)


def add_rows(use: Use, p: Profile, rows: list[tuple[float, str, dict[str, Any]]], start: float, end: float,
             away: list[tuple[float, float]], night: list[tuple[float, float]]) -> None:
    """Adds one entity's history (time, state, attributes) to its use."""
    lit_nights: dict[int, float] = {}
    for i, (t, state, attrs) in enumerate(rows):
        t0 = max(t, start)
        t1 = min(rows[i + 1][0] if i + 1 < len(rows) else end, end)
        if t1 <= t0:
            continue
        w = watts_now(p, state, attrs)
        if w <= 0:
            continue
        use.kwh += w * (t1 - t0) / 3_600_000
        use.on_s += t1 - t0
        a = overlap(t0, t1, away)
        use.away_s += a
        use.away_kwh += w * a / 3_600_000
        for n, span in enumerate(night):
            s = overlap(t0, t1, [span])
            if s:
                lit_nights[n] = lit_nights.get(n, 0) + s
                use.night_kwh += w * s / 3_600_000
        # Per local day, split at midnight.
        a0 = t0
        while a0 < t1:
            local = dt_util.as_local(dt_util.utc_from_timestamp(a0))
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            a1 = min(t1, midnight.timestamp())
            day = local.date().isoformat()
            use.days[day] = use.days.get(day, 0) + w * (a1 - a0) / 3_600_000
            a0 = a1
    use.night_count = max(use.night_count, sum(1 for s in lit_nights.values() if s >= 30 * 60))


def tip_for(uses: list[Use], total: float, recent: list[str]) -> dict[str, Any]:
    """The one tip of the week: what saves the most, not the same as the last two weeks when another fits."""
    options: list[tuple[float, dict[str, Any]]] = []
    for u in uses:
        base = {"ids": u.ids, "area": u.area, "group": u.kind, "measured": u.measured}
        if u.kind not in AWAY_KINDS_SKIP and u.away_kwh >= 0.3 and u.away_s >= 3 * 3600:
            options.append((u.away_kwh, {"kind": "away", "key": f"away:{u.key}", "hours": round(u.away_s / 3600), "kwh": u.away_kwh, **base}))
        if u.kind == "lights" and u.night_count >= 3 and u.night_kwh >= 0.2:
            options.append((u.night_kwh, {"kind": "night", "key": f"night:{u.key}", "nights": u.night_count, "kwh": u.night_kwh, **base}))
        if u.kind in ALWAYS_KINDS and u.on_s >= 7 * 18 * 3600 and u.kwh >= 1:
            options.append((u.kwh * 0.3, {"kind": "always", "key": f"always:{u.key}", "hours": round(u.on_s / 3600 / 7), "kwh": u.kwh * 0.3, **base}))
    if uses and total > 0:
        big = max(uses, key=lambda u: u.kwh)
        if big.kwh >= 2 and big.kwh / total >= 0.25:
            options.append((big.kwh * 0.1, {"kind": "top", "key": f"top:{big.key}", "share": round(big.kwh / total, 2), "kwh": big.kwh * 0.1,
                                            "ids": big.ids, "area": big.area, "group": big.kind, "measured": big.measured}))
    if not options:
        return {"kind": "calm", "key": "calm"}
    # Last weeks' device again (in any tip) only when nothing else comes close.
    said = {k.split(":", 1)[-1] for k in recent if k}
    best = max(options, key=lambda o: o[0] * (0.4 if o[1]["key"].split(":", 1)[-1] in said else 1))
    return best[1]


def iso_week(when: datetime) -> str:
    y, w, _ = dt_util.as_local(when).isocalendar()
    return f"{y}-W{w:02d}"


class EnergyGuess:
    """Guesses the week's energy use per device, and keeps the week's tip and the past days' totals."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = {"days": {}, "tips": {}}
        self.state: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._stops: list[Any] = []

    async def async_load(self) -> None:
        self.data = (await self._store.async_load()) or {"days": {}, "tips": {}}
        self.data.setdefault("days", {})
        self.data.setdefault("tips", {})

    def start(self) -> None:
        async def refresh(_now: Any = None) -> None:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - a guess must never break anything
                _LOGGER.debug("Energy guess failed", exc_info=True)

        self._stops = [async_call_later(self.hass, FIRST_LOOK_S, refresh), async_track_time_interval(self.hass, refresh, EVERY)]

        @callback
        def stop(_event: Event) -> None:
            for s in self._stops:
                s()
            self._stops = []

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)

    async def get(self) -> dict[str, Any]:
        if self.state is None or time.time() - self.state.get("at", 0) > EVERY.total_seconds():
            await self.refresh()
        return self.state or {"ready": False}

    # The money

    async def price(self) -> tuple[float | None, str | None, str | None]:
        """(price per kWh, currency, where it came from: "energy" or "typical")."""
        currency = self.hass.config.currency
        try:
            from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415

            prefs = (await async_get_manager(self.hass)).data or {}
        except Exception:  # noqa: BLE001
            prefs = {}
        for source in prefs.get("energy_sources", []):
            if source.get("type") != "grid":
                continue
            # Since HA 2026.11 on the grid itself; before, on each flow_from meter.
            for item in [source, *source.get("flow_from", [])]:
                if isinstance(item.get("number_energy_price"), (int, float)) and item["number_energy_price"] > 0:
                    return float(item["number_energy_price"]), currency, "energy"
                if item.get("entity_energy_price"):
                    s = self.hass.states.get(item["entity_energy_price"])
                    try:
                        v = float(s.state) if s else 0
                    except ValueError:
                        v = 0
                    if v > 0:
                        return v, currency, "energy"
        typical = TYPICAL_PRICE.get(self.hass.config.country or "")
        if typical and typical[0] == currency:
            return typical[1], currency, "typical"
        return None, currency, None

    # The guess

    def _candidates(self) -> tuple[dict[str, tuple[Profile, str | None]], dict[str, tuple[str, str | None]]]:
        """Guessed entities {id: (profile, area)} and measured energy sensors {id: (device, area)}."""
        ents = er.async_get(self.hass)
        devs = dr.async_get(self.hass)
        areas = ar.async_get(self.hass)

        def area_of(entry: er.RegistryEntry | None) -> str | None:
            if entry is None:
                return None
            area_id = entry.area_id or (devs.async_get(entry.device_id).area_id if entry.device_id and devs.async_get(entry.device_id) else None)
            a = areas.async_get_area(area_id) if area_id else None
            return a.name if a else None

        whole = re.compile(r"home|house|total|grid|main|whole|maison|haus|casa|import|export|solar|battery|consumption_total", re.I)
        measured: dict[str, tuple[str, str | None]] = {}
        measured_devices: set[str] = set()
        for s in self.hass.states.async_all("sensor"):
            if s.attributes.get("device_class") != "energy" or s.attributes.get("state_class") not in ("total", "total_increasing"):
                continue
            if s.attributes.get("unit_of_measurement") not in ("kWh", "Wh"):
                continue
            entry = ents.async_get(s.entity_id)
            if entry is None or not entry.device_id or entry.platform in SKIP_PLATFORMS or whole.search(s.entity_id):
                continue
            # One energy sensor per device: the first one (a plug that measures has one).
            if entry.device_id in measured_devices:
                continue
            measured_devices.add(entry.device_id)
            measured[s.entity_id] = (entry.device_id, area_of(entry))
        guessed: dict[str, tuple[Profile, str | None]] = {}
        for s in self.hass.states.async_all(("light", "media_player", "climate", "fan", "humidifier", "switch")):
            entry = ents.async_get(s.entity_id)
            if entry is not None and (entry.entity_category is not None or entry.hidden_by or entry.disabled_by):
                continue
            if entry is not None and entry.device_id in measured_devices:
                continue
            p = profile(s.entity_id, s.name, dict(s.attributes), entry.platform if entry else None)
            if p:
                guessed[s.entity_id] = (p, area_of(entry))
        return guessed, measured

    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            try:
                from homeassistant.components.recorder import get_instance, history, statistics  # noqa: PLC0415
            except ImportError:
                self.state = {"ready": False, "reason": "recorder", "at": time.time()}
                return self.state
            end = dt_util.utcnow()
            start = end - WEEK
            guessed, measured = self._candidates()
            people = [s.entity_id for s in self.hass.states.async_all("person")]
            ids = [*guessed, *people]
            recorder = get_instance(self.hass)
            rows: dict[str, list[Any]] = {}
            if ids:
                rows = await recorder.async_add_executor_job(
                    lambda: history.get_significant_states(self.hass, start, end, ids, significant_changes_only=True)
                )
            stats: dict[str, list[Any]] = {}
            if measured:
                stats = await recorder.async_add_executor_job(
                    lambda: statistics.statistics_during_period(self.hass, start, end, set(measured), "hour", None, {"change"})
                )
            price, currency, price_from = await self.price()
            self.state = self._build(start, end, guessed, measured, people, rows, stats, price, currency, price_from)
            await self._store.async_save(self.data)
            return self.state

    def _build(self, start: datetime, end: datetime, guessed: dict[str, tuple[Profile, str | None]],
               measured: dict[str, tuple[str, str | None]], people: list[str], rows: dict[str, list[Any]],
               stats: dict[str, list[Any]], price: float | None, currency: str | None, price_from: str | None) -> dict[str, Any]:
        t0, t1 = start.timestamp(), end.timestamp()

        def history_of(eid: str) -> list[tuple[float, str, dict[str, Any]]]:
            out = []
            for st in rows.get(eid, []):
                if isinstance(st, State):
                    out.append((st.last_changed.timestamp(), st.state, dict(st.attributes)))
                elif isinstance(st, dict):
                    lc = st.get("last_changed")
                    when = dt_util.parse_datetime(lc).timestamp() if isinstance(lc, str) and dt_util.parse_datetime(lc) else t0
                    out.append((when, st.get("state", ""), st.get("attributes", {})))
            return sorted(out, key=lambda r: r[0])

        hist = {eid: h for eid in guessed if (h := history_of(eid))}
        first = min((h[0][0] for h in hist.values()), default=t1)
        away = away_spans([[(t, s) for t, s, _ in history_of(p)] for p in people], t0, t1)
        night = nights(start, end)
        uses: dict[str, Use] = {}
        for eid, (p, area) in guessed.items():
            h = hist.get(eid)
            if not h:
                continue
            key = f"lights:{area or ''}" if p.kind == "lights" else eid
            use = uses.get(key) or Use(key=key, kind=p.kind, ids=[], area=area)
            uses[key] = use
            use.ids.append(eid)
            add_rows(use, p, h, t0, t1, away, night)
        for eid, (_device, area) in measured.items():
            points = stats.get(eid, [])
            kwh = 0.0
            use = Use(key=eid, kind="measured", ids=[eid], area=area, measured=True)
            unit = (self.hass.states.get(eid).attributes.get("unit_of_measurement") if self.hass.states.get(eid) else "kWh")
            scale = 0.001 if unit == "Wh" else 1
            for pt in points:
                change = (pt.get("change") or 0) * scale
                if change <= 0:
                    continue
                kwh += change
                when = pt.get("start")
                if isinstance(when, (int, float)):
                    when_s = when / 1000 if when > 1e11 else when
                    day = dt_util.as_local(dt_util.utc_from_timestamp(when_s)).date().isoformat()
                    use.days[day] = use.days.get(day, 0) + change
                    a = overlap(when_s, when_s + 3600, away)
                    use.away_kwh += change * a / 3600
                    use.away_s += a
                    use.on_s += 3600 if change > 0.005 else 0
            if kwh > 0.05:
                use.kwh = kwh
                uses[eid] = use
        found = [u for u in uses.values() if u.kwh > 0.01]
        found.sort(key=lambda u: -u.kwh)
        total = sum(u.kwh for u in found)
        days_seen = max(0.0, min(7.0, (t1 - first) / 86400))

        # The past days' totals (complete days only), for "less than last week".
        today = dt_util.as_local(end).date().isoformat()
        start_day = dt_util.as_local(start).date().isoformat()
        per_day: dict[str, float] = {}
        for u in found:
            for d, v in u.days.items():
                per_day[d] = per_day.get(d, 0) + v
        if days_seen >= 6.9:
            for d, v in per_day.items():
                if d not in (today, start_day):
                    self.data["days"][d] = round(v, 3)
        keep = sorted(self.data["days"])[-KEEP_DAYS:]
        self.data["days"] = {d: self.data["days"][d] for d in keep}
        trend = None
        this_days = [(dt_util.as_local(end) - timedelta(days=i)).date().isoformat() for i in range(1, 8)]
        last_days = [(dt_util.as_local(end) - timedelta(days=i)).date().isoformat() for i in range(8, 15)]
        if all(d in self.data["days"] for d in this_days + last_days):
            a = sum(self.data["days"][d] for d in this_days)
            b = sum(self.data["days"][d] for d in last_days)
            if b > 0:
                trend = "less" if a < b * 0.9 else "more" if a > b * 1.1 else "same"

        # The week's tip: chosen once, on the first look of the week with enough history.
        week = iso_week(end)
        tips = self.data["tips"]
        if days_seen >= 3 and week not in tips:
            recent = [tips[w].get("key") for w in sorted(tips)[-2:]]
            tips[week] = tip_for(found, total, recent)
            self.data["tips"] = {w: tips[w] for w in sorted(tips)[-8:]}
        tip = dict(self.data["tips"].get(week) or {})
        if tip and price:
            # Weekly kWh saved, said per month.
            tip["month_cost"] = round((tip.get("kwh") or 0) * 52 / 12 * price, 2)
        if tip.get("kwh") is not None:
            tip["month_kwh"] = round(tip["kwh"] * 52 / 12, 1)

        groups = [
            {"kind": u.kind, "area": u.area, "ids": u.ids[:12], "kwh": round(u.kwh, 2), "share": round(u.kwh / total, 3) if total else 0,
             "measured": u.measured, "hours": round(u.on_s / 3600, 1)}
            for u in found[:TOP]
        ]
        measured_kwh = sum(u.kwh for u in found if u.measured)
        return {
            "ready": days_seen >= 1 and total > 0,
            "at": time.time(),
            "days": round(days_seen, 1),
            "kwh": round(total, 2),
            "cost": round(total * price, 2) if price else None,
            "currency": currency,
            "price_from": price_from,
            "measured_share": round(measured_kwh / total, 2) if total else 0,
            "groups": groups,
            "rest_kwh": round(sum(u.kwh for u in found[TOP:]), 2),
            "devices": sum(len(u.ids) for u in found),
            "trend": trend,
            "tip": tip or None,
            "week": week,
            "people": bool(people),
        }


async def async_setup(hass: HomeAssistant) -> None:
    guess = EnergyGuess(hass)
    await guess.async_load()
    hass.data[ENERGY] = guess
    guess.start()
    websocket_api.async_register_command(hass, ws_guess)
    # Cheaper power hours: the price clock (free, power_clock.py).
    from .power_clock import async_setup as async_setup_clock

    await async_setup_clock(hass)


# Like Home moments, this works for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/energy/guess", vol.Optional("fresh"): bool})
@websocket_api.async_response
async def ws_guess(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    guess: EnergyGuess = hass.data[ENERGY]
    try:
        state = await (guess.refresh() if msg.get("fresh") else guess.get())
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Energy guess failed", exc_info=True)
        state = {"ready": False}
    connection.send_result(msg["id"], state)
