"""Drive time for the wall: how long the drive takes right now, with traffic.

The Drive tile and the Smart card ask here when the family calendar has an event with a place, or for the usual
drive to work. Home Assistant asks Waze for the drive, the same free source as Home Assistant's own Waze travel time
integration, and OpenStreetMap to find an address: no account, no key, no new device. We use that integration's own
library (pywaze), installed the way Home Assistant installs it, so a change on Waze's side is fixed by a normal Home
Assistant update.

Each place is asked at most every 5 minutes, whatever the number of tablets, and the whole home asks at most
60 times an hour. A place's position is kept, so the address search runs once per place. The drive without traffic
(the usual time) changes little and is asked again only once a day.

A place can be:
1. "43.65,-79.38": a position.
2. A zone's entity id ("zone.work") or a zone's name ("Work").
3. Any address or place name: OpenStreetMap finds it near the home (Waze's own search if that fails).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import math
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import VERSION

_LOGGER = logging.getLogger(__name__)

DRIVE = "hadash_drive"
FRESH = 5 * 60
USUAL_FRESH = 24 * 3600
# After a failed lookup, the same place waits this long before the next try.
RETRY = 10 * 60
WARN_EVERY = 30 * 60
PER_HOUR = 60
PLACES_KEPT = 200
# Farther than this is a trip, not a drive: the wall says nothing.
FAR_KM = 400
WAIT = 30
OSM_URL = "https://nominatim.openstreetmap.org/search"
OSM_HEADERS = {"User-Agent": f"DashboardStudio/{VERSION} (Home Assistant drive time; +https://lukaps.com/dashboardstudio/)"}

COORDS = re.compile(r"^\s*([-+]?\d{1,2}(?:\.\d+)?)\s*,\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")
# An online meeting has a link or a meeting word for its place: there is nowhere to drive.
ONLINE = re.compile(r"(https?://|www\.|\bzoom\b|\bteams\b|meet\.google|\bgoogle meet\b|\bwebex\b|\bonline\b|\bvirtual\b|\bskype\b|\bfacetime\b|\bphone call\b|\bcall\b)", re.I)

# Waze has one server for North America and one for most of the rest of the world.
NORTH_AMERICA = {"US", "CA", "MX", "PR", "VI", "GU", "AS", "MP"}
AUSTRALIA = {"AU", "NZ"}


def region_of(country: str | None) -> str:
    """The Waze region for the home's country."""
    c = (country or "").upper()
    if c in NORTH_AMERICA:
        return "US"
    if c == "IL":
        return "IL"
    if c in AUSTRALIA:
        return "AU"
    return "EU"


def is_online(place: str) -> bool:
    """A meeting link or "Zoom" is not a place to drive to."""
    return bool(ONLINE.search(place))


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """The distance in a straight line, in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, h)))


def traffic_of(minutes: float, usual: float | None) -> str:
    """Traffic in words: "light", "some" or "heavy", from how much longer the drive is than without traffic."""
    if not usual or usual <= 0:
        return "light"
    extra = minutes - usual
    ratio = minutes / usual
    if ratio >= 1.4 and extra >= 6:
        return "heavy"
    if ratio >= 1.15 and extra >= 3:
        return "some"
    return "light"


def via_of(route: Any) -> str:
    """The road the drive mostly takes, from Waze's route name ("Gardiner Expy W; Lake Shore Blvd")."""
    name = str(getattr(route, "name", "") or "").strip()
    return name.split(";")[0].strip()[:60] if name else ""


class Drive:
    """The drive times, shared by every tablet."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._places: OrderedDict[str, tuple[float, float] | None] = OrderedDict()
        self._times: dict[str, dict[str, Any]] = {}
        self._usual: dict[str, tuple[float, float]] = {}
        self._failed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._asked: list[float] = []
        self._calc: Any = None
        self._osm_lock = asyncio.Lock()
        self._osm_at = 0.0
        self._warned_at = 0.0

    @property
    def home(self) -> tuple[float, float]:
        return (float(self.hass.config.latitude), float(self.hass.config.longitude))

    async def _calculator(self) -> Any:
        """Waze's route calculator, from the library Home Assistant's Waze integration uses."""
        if self._calc is not None:
            return self._calc
        try:
            from pywaze import route_calculator  # noqa: PLC0415
        except ImportError:
            # Install it the way Home Assistant installs it for its own Waze integration (same pinned version).
            from homeassistant.requirements import async_get_integration_with_requirements  # noqa: PLC0415

            await async_get_integration_with_requirements(self.hass, "waze_travel_time")
            from pywaze import route_calculator  # noqa: PLC0415
        region = region_of(self.hass.config.country)
        if hasattr(route_calculator, "AsyncSession"):
            # Newer versions make their own browser-like connection; Waze refuses plain ones.
            self._calc = route_calculator.WazeRouteCalculator(region=region)
        else:
            from homeassistant.helpers.httpx_client import get_async_client  # noqa: PLC0415

            self._calc = route_calculator.WazeRouteCalculator(region=region, client=get_async_client(self.hass))
        return self._calc

    def _spend(self) -> bool:
        """One ask from the home's hourly allowance; False when it is used up."""
        now = time.monotonic()
        self._asked = [t for t in self._asked if now - t < 3600]
        if len(self._asked) >= PER_HOUR:
            return False
        self._asked.append(now)
        return True

    def _zone(self, place: str) -> tuple[float, float] | None:
        """A zone by its entity id or its name."""
        want = place.strip().casefold()
        for st in self.hass.states.async_all("zone"):
            if st.entity_id == place.strip() or str(st.attributes.get("friendly_name", st.name)).casefold() == want:
                lat, lon = st.attributes.get("latitude"), st.attributes.get("longitude")
                if lat is not None and lon is not None:
                    return (float(lat), float(lon))
        return None

    async def where(self, place: str) -> tuple[float, float] | None:
        """The position of a place, or None when nothing was found."""
        if m := COORDS.match(place):
            return (float(m[1]), float(m[2]))
        if zone := self._zone(place):
            return zone
        key = place.strip().casefold()
        if key in self._places:
            self._places.move_to_end(key)
            return self._places[key]
        spot = await self._search(place)
        self._places[key] = spot
        while len(self._places) > PLACES_KEPT:
            self._places.popitem(last=False)
        return spot

    async def _search(self, place: str) -> tuple[float, float] | None:
        """An address or a place name: OpenStreetMap first, then Waze's own search.

        Waze's address search started refusing requests in November 2026 (HTTP 400), while its routing works,
        so a free open search finds the place. A failure of both raises; a place nobody knows returns None.
        """
        errors: list[Exception] = []
        for ask in (self._osm, self._waze_search):
            try:
                if spot := await ask(place):
                    return spot
            except Exception as err:  # noqa: BLE001
                errors.append(err)
        # "Venue, 123 Street, City": a search may know the street address but not the venue's name.
        if "," in place and len(errors) < 2:
            rest = place.split(",", 1)[1].strip()
            if rest and any(c.isdigit() for c in rest):
                return await self._search(rest)
        if len(errors) == 2:
            raise errors[0]
        return None

    async def _osm(self, place: str) -> tuple[float, float] | None:
        """OpenStreetMap's free search (Nominatim), near the home: at most one ask a second, as its rules ask."""
        lat, lon = self.home
        params = {"q": place, "format": "jsonv2", "limit": "1", "viewbox": f"{lon - 1.5},{lat + 1.5},{lon + 1.5},{lat - 1.5}"}
        if country := (self.hass.config.country or "").lower():
            params["countrycodes"] = country
        async with self._osm_lock:
            wait = self._osm_at + 1.1 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._osm_at = time.monotonic()
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(WAIT), session.get(OSM_URL, params=params, headers=OSM_HEADERS) as resp:
                resp.raise_for_status()
                found = await resp.json()
        if not found:
            return None
        return (float(found[0]["lat"]), float(found[0]["lon"]))

    async def _waze_search(self, place: str) -> tuple[float, float] | None:
        calc = await self._calculator()
        lat, lon = self.home
        async with asyncio.timeout(WAIT):
            try:
                try:
                    found = await calc.address_to_coords(place, base_coords={"lat": lat, "lon": lon})
                except TypeError:
                    # Older versions search near their region's own centre.
                    found = await calc.address_to_coords(place)
            except Exception as err:
                # "Cannot get coords for ...": Waze answered, and knows no such place. Anything else is a failure.
                if "cannot get coords" not in str(err).lower() and not isinstance(err, LookupError):
                    raise
                found = None
        return (float(found["lat"]), float(found["lon"])) if found and "lat" in found else None
    async def _route(self, to: tuple[float, float], real_time: bool) -> tuple[float, float, str]:
        """Minutes, km and the main road, now."""
        calc = await self._calculator()
        start = "{},{}".format(*self.home)
        end = "{},{}".format(*to)
        async with asyncio.timeout(WAIT):
            routes = await calc.calc_routes(start, end, real_time=real_time)
        if not routes:
            raise LookupError("no route")
        best = min(routes, key=lambda r: r.duration)
        return float(best.duration), float(best.distance), via_of(best)

    async def time_to(self, place: str) -> dict[str, Any]:
        """How long the drive takes now. {"minutes", "usual", "traffic", "km", "via", "at"} or {"error"}."""
        place = place.strip()
        if not place:
            return {"error": "no_place"}
        if is_online(place):
            return {"error": "online"}
        key = place.casefold()
        now = time.time()
        if (hit := self._times.get(key)) and now - hit["at"] < FRESH:
            return hit
        if now - self._failed.get(key, 0) < RETRY:
            return self._times.get(key) or {"error": "failed"}
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if (hit := self._times.get(key)) and time.time() - hit["at"] < FRESH:
                return hit
            if not self._spend():
                return self._times.get(key) or {"error": "busy"}
            try:
                spot = await self.where(place)
                if spot is None:
                    self._failed[key] = time.time()
                    return {"error": "not_found"}
                if km_between(self.home, spot) > FAR_KM:
                    return {"error": "far"}
                minutes, km, via = await self._route(spot, True)
                usual = self._usual.get(key)
                if not usual or time.time() - usual[1] > USUAL_FRESH:
                    try:
                        usual = ((await self._route(spot, False))[0], time.time())
                        self._usual[key] = usual
                    except Exception:  # noqa: BLE001
                        usual = usual or None
            except Exception as err:  # noqa: BLE001
                self._failed[key] = time.time()
                why = f"{type(err).__name__}: {err}"[:200]
                if time.time() - self._warned_at > WARN_EVERY:
                    # A wall that shows no drive time leaves a reason in the log, at most every 30 minutes.
                    _LOGGER.warning("Drive time: no answer for a place (%s). The wall tries again in 10 minutes.", why)
                    self._warned_at = time.time()
                else:
                    _LOGGER.debug("Drive time failed for a place", exc_info=True)
                return self._times.get(key) or {"error": "failed", "why": why}
            self._failed.pop(key, None)
            usual_min = usual[0] if usual else None
            out = {
                "minutes": max(1, round(minutes)),
                "usual": max(1, round(usual_min)) if usual_min else None,
                "traffic": traffic_of(minutes, usual_min),
                "km": round(km, 1),
                "via": via,
                "at": time.time(),
            }
            self._times[key] = out
            return out


async def async_setup(hass: HomeAssistant) -> None:
    hass.data[DRIVE] = Drive(hass)
    websocket_api.async_register_command(hass, ws_time)


# Wall tablets often sign in as a plain user, so this is not for admins only. It only ever asks Waze, at most
# 60 times an hour for the whole home, so a signed-in user cannot make Home Assistant fetch anything else.


@websocket_api.websocket_command({vol.Required("type"): "hadash/drive/time", vol.Required("to"): vol.All(str, vol.Length(max=300))})
@websocket_api.async_response
async def ws_time(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    drive: Drive = hass.data[DRIVE]
    try:
        out = await drive.time_to(msg["to"])
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Drive time failed", exc_info=True)
        out = {"error": "failed"}
    connection.send_result(msg["id"], out)
