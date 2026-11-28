"""Sunrise wake-up (free): before a wake-up alarm, the lights of the tablet's room grow from a faint warm glow to
daylight, the way the sky does, and the tablet's screen brightens with them (renderer widgets/Dawn.svelte).

Home Assistant does the lights, so they rise even when the tablet sleeps or reloads. Each minute it looks at the
wake-up alarms (alarms.py). In the 20 minutes before one, it takes the lights of the alarm tablet's room that are
off, and every minute sets them a little brighter and a little less orange: 1% at 2200 K at the start, 80% at
3500 K at the alarm. A light someone turns off or changes during the sunrise is left alone from then on. At the
alarm the lights stay as they are. It never touches a light that was already on.

The room: the area of the tablet's device in Home Assistant, else of the screen the tablet shows, else a room whose
name the alarm's screen name holds. It is one switch in the Studio (Home moments), on by default.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SUNRISE = "hadash_sunrise"
MINUTES = 20
START_PCT, END_PCT = 1, 80
START_K, END_K = 2200, 3500


def next_ring(alarm: dict[str, Any], now: datetime) -> datetime | None:
    """The next time [alarm] rings after [now] (both in Home Assistant's time zone), like the tablet's nextRing."""
    if not alarm.get("on", True):
        return None
    try:
        h, m = (int(x) for x in str(alarm.get("time", "")).split(":"))
    except ValueError:
        return None
    days = alarm.get("days") or []
    for add in range(8):
        day = (now + timedelta(days=add)).replace(hour=h, minute=m, second=0, microsecond=0)
        if day <= now:
            continue
        if days:
            # The tablet counts days from Sunday (0); Python from Monday.
            if (day.weekday() + 1) % 7 in days:
                return day
        elif alarm.get("date"):
            if day.date().isoformat() == alarm["date"]:
                return day
        else:
            return day
    return None


def step(f: float) -> tuple[int, int]:
    """Brightness (%) and colour (K) at [f] of the way (0 to 1). Slow at first, like the first light."""
    f = min(1.0, max(0.0, f))
    return round(START_PCT + (END_PCT - START_PCT) * f * f), round(START_K + (END_K - START_K) * f)


class Sunrise:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # alarm id + ring time -> {light: the brightness (0-255) last set}.
        self.rising: dict[str, dict[str, int]] = {}
        self._unsub: Any = None

    @property
    def on(self) -> bool:
        from .alarms import ALARMS

        store = self.hass.data.get(ALARMS)
        return bool(store is None or store.settings.get("sunrise", True))

    def start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(minutes=1))

        @callback
        def stop(_event: Event) -> None:
            if self._unsub:
                self._unsub()
                self._unsub = None

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)

    async def _tick(self, _now: Any) -> None:
        await self.run(dt_util.now())

    def area_of(self, alarm: dict[str, Any]) -> str | None:
        tablet = str(alarm.get("tablet") or "")
        if tablet:
            device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, tablet)})
            if device and device.area_id:
                return device.area_id
        where = str(alarm.get("where") or "")
        if not where:
            return None
        # The screen the alarm was set on, by its name: the room the Studio gives it.
        from .follow import FOLLOW

        follow = self.hass.data.get(FOLLOW)
        layout = getattr(self.hass.data.get(DOMAIN), "layout", None) or {}
        for sc in layout.get("screens") or []:
            if isinstance(sc, dict) and str(sc.get("name") or "") == where and follow and (area := follow.area_of(str(sc.get("id")))):
                return area
        from .satellite import room_of

        return room_of(self.hass, where)

    def lights_in(self, area: str) -> list[str]:
        ents = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        out: list[str] = []
        for s in self.hass.states.async_all("light"):
            entry = ents.async_get(s.entity_id)
            if entry is None or entry.disabled_by or entry.hidden_by:
                continue
            where = entry.area_id
            if not where and entry.device_id and (d := devices.async_get(entry.device_id)):
                where = d.area_id
            if where == area:
                out.append(s.entity_id)
        return out

    async def run(self, now: datetime) -> None:
        """One minute of every sunrise. Open for tests."""
        from .alarms import ALARMS

        store = self.hass.data.get(ALARMS)
        if store is None:
            return
        live: set[str] = set()
        for alarm in store.alarms if self.on else []:
            ring = next_ring(alarm, now - timedelta(seconds=30))
            if ring is None or ring - now > timedelta(minutes=MINUTES) or ring <= now:
                continue
            key = f"{alarm['id']}@{ring.isoformat()}"
            live.add(key)
            lights = self.rising.get(key)
            if lights is None:
                area = self.area_of(alarm)
                # Only lights that are off now: a light someone uses is never taken.
                lights = {e: 0 for e in (self.lights_in(area) if area else []) if (s := self.hass.states.get(e)) and s.state == "off"}
                self.rising[key] = lights
                if lights:
                    _LOGGER.info("Sunrise for the %s alarm: %s", alarm.get("time"), ", ".join(lights))
            f = 1 - (ring - now).total_seconds() / (MINUTES * 60)
            pct, kelvin = step(f)
            for entity_id, last in list(lights.items()):
                s = self.hass.states.get(entity_id)
                # Someone turned it off, or changed it: it is theirs now.
                if s is None or (last and (s.state != "on" or abs(int(s.attributes.get("brightness") or 0) - last) > 30)):
                    lights.pop(entity_id, None)
                    continue
                data: dict[str, Any] = {"entity_id": entity_id, "brightness_pct": pct, "transition": 55}
                if "color_temp" in (s.attributes.get("supported_color_modes") or []):
                    data["color_temp_kelvin"] = kelvin
                try:
                    await self.hass.services.async_call("light", "turn_on", data, blocking=False)
                except Exception:  # noqa: BLE001 - one light that fails does not stop the others
                    _LOGGER.warning("Sunrise: could not set %s", entity_id)
                    continue
                lights[entity_id] = max(1, round(pct * 255 / 100))
        for key in list(self.rising):
            if key not in live:
                self.rising.pop(key, None)


async def async_setup(hass: HomeAssistant) -> None:
    sunrise = Sunrise(hass)
    hass.data[SUNRISE] = sunrise
    sunrise.start()
