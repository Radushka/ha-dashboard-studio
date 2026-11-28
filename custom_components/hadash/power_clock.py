"""Cheaper power hours (free): when power costs less today, with no meter and no account.

Many homes pay by the clock. In Ontario the price follows a fixed clock set by the Ontario Energy Board, so Home
Assistant knows it without asking anyone:

1. Time-of-use ("on_tou", most homes): weekdays 7 PM to 7 AM are off-peak (cheap). In winter (November to April)
   7 to 11 AM and 5 to 7 PM are on-peak (dear), 11 AM to 5 PM mid-peak. In summer (May to October) 11 AM to 5 PM is
   on-peak, 7 to 11 AM and 5 to 7 PM mid-peak. Weekends and holidays are off-peak all day.
2. Ultra-low overnight ("on_ulo"): 11 PM to 7 AM every day is ultra-low (cheapest). Weekdays 4 to 9 PM are on-peak,
   the rest of the day mid-peak; weekends and holidays 7 AM to 11 PM are weekend off-peak (in between).
3. A price sensor with today's and tomorrow's prices (Nord Pool, and others that use its "raw_today" and
   "raw_tomorrow" lists): each hour is cheap, middle or dear by where it falls in that day's prices.

"auto" (the default) finds a price sensor first, else time-of-use for a home in Ontario (Canada, Toronto time).
Everything else has no clock, and nothing shows. The Studio can pick the plan.

The screens get three words, never prices: "low", "mid", "high", for the next 24 hours, with the time each one
starts. The Energy card draws them as a soft band; a notice says "Cheaper power in 40 minutes. Start the dishwasher
then." (notices.py).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_VERSION

POWER_CLOCK = "hadash_power_clock"
STORAGE_KEY = "hadash.power_clock"
PLANS = ("auto", "on_tou", "on_ulo", "off")
ONTARIO_ZONES = {"America/Toronto", "America/Thunder_Bay", "America/Nipigon", "America/Rainy_River", "America/Atikokan"}
HOURS = 24


def _easter(year: int) -> date:
    """Easter Sunday (Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_monday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(7 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _observed(d: date) -> date:
    """A holiday on a weekend is kept on the Monday after (Christmas and Boxing Day move together)."""
    if d.weekday() == 5:
        return d + timedelta(days=2)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def ontario_holidays(year: int) -> set[date]:
    """The holidays that are off-peak all day in Ontario (the Ontario Energy Board's list)."""
    may25 = date(year, 5, 25)
    victoria = may25 - timedelta(days=may25.weekday())
    christmas = date(year, 12, 25)
    boxing = date(year, 12, 26)
    if christmas.weekday() == 5:
        christmas, boxing = date(year, 12, 27), date(year, 12, 28)
    elif christmas.weekday() == 6:
        christmas, boxing = date(year, 12, 26), date(year, 12, 27)
    elif christmas.weekday() == 4:
        boxing = date(year, 12, 28)
    return {
        _observed(date(year, 1, 1)),
        _nth_monday(year, 2, 3),  # Family Day
        _easter(year) - timedelta(days=2),  # Good Friday
        victoria,
        _observed(date(year, 7, 1)),
        _nth_monday(year, 8, 1),  # Civic Holiday
        _nth_monday(year, 9, 1),  # Labour Day
        _nth_monday(year, 10, 2),  # Thanksgiving
        christmas,
        boxing,
    }


def _day_off(d: date) -> bool:
    return d.weekday() >= 5 or d in ontario_holidays(d.year)


def ontario_level(plan: str, at: datetime) -> str:
    """The level of one moment on an Ontario plan: "low", "mid" or "high"."""
    h = at.hour
    if plan == "on_ulo":
        if h >= 23 or h < 7:
            return "low"
        if _day_off(at.date()):
            return "mid"
        return "high" if 16 <= h < 21 else "mid"
    if h >= 19 or h < 7 or _day_off(at.date()):
        return "low"
    winter = at.month >= 11 or at.month <= 4
    if winter:
        return "high" if (7 <= h < 11 or 17 <= h < 19) else "mid"
    return "high" if 11 <= h < 17 else "mid"


def _merge(slots: list[tuple[datetime, str]], end: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for start, level in slots:
        if out and out[-1]["level"] == level:
            continue
        if out:
            out[-1]["end"] = start.isoformat()
        out.append({"start": start.isoformat(), "level": level})
    if out:
        out[-1]["end"] = end.isoformat()
    return out


def price_levels(prices: list[dict[str, Any]]) -> list[tuple[datetime, datetime, str]]:
    """Nord Pool style [{start, end, value}]: each slot cheap, middle or dear by thirds of its own day."""
    by_day: dict[date, list[tuple[datetime, datetime, float]]] = {}
    for p in prices:
        try:
            start = dt_util.as_local(dt_util.parse_datetime(str(p["start"])))  # type: ignore[arg-type]
            end = dt_util.as_local(dt_util.parse_datetime(str(p["end"])))  # type: ignore[arg-type]
            value = float(p["value"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        by_day.setdefault(start.date(), []).append((start, end, value))
    out: list[tuple[datetime, datetime, str]] = []
    for slots in by_day.values():
        values = sorted(v for _s, _e, v in slots)
        third = max(1, -(-len(values) // 3))
        low = values[third - 1]
        high = values[len(values) - third]
        for s, e, v in slots:
            out.append((s, e, "low" if v <= low else "high" if v >= high else "mid"))
    return sorted(out)


class PowerClock:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.settings: dict[str, Any] = {"plan": "auto"}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.settings = {**self.settings, **data.get("settings", {})}

    def set_plan(self, plan: str) -> None:
        self.settings = {**self.settings, "plan": plan}
        self._store.async_delay_save(lambda: {"settings": self.settings}, 1)

    def _price_sensor(self) -> str | None:
        for s in self.hass.states.async_all("sensor"):
            if isinstance(s.attributes.get("raw_today"), list) and s.attributes["raw_today"]:
                return s.entity_id
        return None

    def plan(self) -> tuple[str, str | None]:
        """(the plan in use, its price sensor)."""
        chosen = self.settings.get("plan", "auto")
        if chosen != "auto":
            return chosen, None
        sensor = self._price_sensor()
        if sensor:
            return "prices", sensor
        if (self.hass.config.country or "") == "CA" and str(self.hass.config.time_zone) in ONTARIO_ZONES:
            return "on_tou", None
        return "off", None

    def state(self, now: datetime | None = None) -> dict[str, Any]:
        """The next 24 hours as levels, and what they mean now."""
        now = dt_util.as_local(now or dt_util.now()).replace(second=0, microsecond=0)
        plan, sensor = self.plan()
        end = now + timedelta(hours=HOURS)
        bands: list[dict[str, Any]] = []
        if plan in ("on_tou", "on_ulo"):
            slots = [(now, ontario_level(plan, now))]
            t = now.replace(minute=0) + timedelta(hours=1)
            while t < end:
                slots.append((t, ontario_level(plan, t)))
                t += timedelta(hours=1)
            bands = _merge(slots, end)
        elif plan == "prices" and sensor:
            s = self.hass.states.get(sensor)
            raw = [*(s.attributes.get("raw_today") or []), *(s.attributes.get("raw_tomorrow") or [])] if s else []
            slots = [(max(st, now), lv) for st, en, lv in price_levels(raw) if en > now and st < end]
            bands = _merge(slots, min(end, max((en for _s, en, _l in price_levels(raw)), default=now)))
        if not bands:
            return {"plan": "off" if plan != "prices" else plan, "bands": []}
        current = bands[0]
        next_low = next((b["start"] for b in bands[1:] if b["level"] == "low"), None)
        return {"plan": plan, "now": current["level"], "until": current["end"], "next_low": next_low, "bands": bands}


async def async_setup(hass: HomeAssistant) -> None:
    clock = PowerClock(hass)
    await clock.async_load()
    hass.data[POWER_CLOCK] = clock
    websocket_api.async_register_command(hass, ws_clock)
    websocket_api.async_register_command(hass, ws_plan)


# Like Home moments, the clock works for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command({vol.Required("type"): "hadash/energy/clock"})
@callback
def ws_clock(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    clock: PowerClock = hass.data[POWER_CLOCK]
    connection.send_result(msg["id"], {**clock.state(), "chosen": clock.settings.get("plan", "auto")})


@websocket_api.websocket_command({vol.Required("type"): "hadash/energy/clock_plan", vol.Required("plan"): vol.In(PLANS)})
@websocket_api.require_admin
@callback
def ws_plan(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    clock: PowerClock = hass.data[POWER_CLOCK]
    clock.set_plan(msg["plan"])
    connection.send_result(msg["id"], {**clock.state(), "chosen": clock.settings.get("plan", "auto")})
