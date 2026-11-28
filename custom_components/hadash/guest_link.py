"""Guest phone link (free): the babysitter's phone becomes a small remote for the house, for a short time.

The wall shows a code in guest mode, and the studio can copy the same link to send ahead. The phone needs no
Home Assistant login and no app. What it can reach is decided here, never by the phone:

1. Only lights, scenes, fans, shades, music and the thermostat, in rooms, and never locks, the alarm, cameras,
   garage doors or switches. The owner can leave rooms out (theme.guest.phone.hide).
2. Only a few actions for each: on and off, brightness, a scene, play and pause, volume, and the thermostat
   at most 2 degrees from where it was when the link was made.
3. Only while guest mode is on. When guest mode ends, the link ends with it, and a new one is made next time.
4. Only until its end time (12 hours, 1 day or 3 days after it was made).
5. The key is 256 random bits. It sits after the "#" of the address, so it never goes to a server log or the
   Home Assistant Cloud: the page sends it in a header. A wrong key counts against that address, and 10 wrong
   keys in 10 minutes close the door to it for 10 minutes. Each link has a limit of commands per minute.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta
import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

LINKS = "hadash_guest_link"
STORAGE_KEY = "hadash.guest_link"
PATH = "/api/hadash/guest"
HEADER = "X-Hadash-Guest"
EVENT_OPENED = "hadash_guest_link_opened"

HOURS = (12, 24, 72)
DEFAULT_HOURS = 24
# Wrong keys: this many from one address in WRONG_WINDOW seconds closes the door to it for WRONG_WINDOW seconds.
WRONG_MAX = 10
WRONG_WINDOW = 600
# Commands and reads for one link: a page reads every few seconds, and a finger on a slider sends a few a second.
CALLS_MAX = 120
CALLS_WINDOW = 60
# The thermostat may move this far from where it was when the link was made.
CLIMATE_RANGE = 2.0
SHADES = {"shade", "blind", "curtain", "awning", "shutter", None}
KINDS = ("light", "scene", "fan", "cover", "media_player", "climate")

PAGE = Path(__file__).parent / "guest_page.html"


# ---- What a guest may reach ----


def guest_settings(hass: HomeAssistant) -> dict[str, Any]:
    layout = hass.data[DOMAIN].layout or {}
    theme = layout.get("theme") if isinstance(layout, dict) else None
    guest = theme.get("guest") if isinstance(theme, dict) else None
    return guest if isinstance(guest, dict) else {}


def phone_settings(hass: HomeAssistant) -> dict[str, Any]:
    phone = guest_settings(hass).get("phone")
    return phone if isinstance(phone, dict) else {}


def _short(name: str, room: str) -> str:
    """ "Kitchen ceiling" in the Kitchen is "Ceiling"."""
    if room and name.lower().startswith(room.lower() + " ") and len(name) > len(room) + 1:
        rest = name[len(room) + 1 :].strip()
        return rest[:1].upper() + rest[1:]
    return name


def guest_things(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The rooms and what a guest may use in each: [{room, things: [{id, kind, name}]}], in the house's order."""
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    hide = {str(a) for a in phone_settings(hass).get("hide") or []}
    rooms: dict[str, list[dict[str, Any]]] = {}
    for entry in entities.entities.values():
        if entry.domain not in KINDS or entry.disabled_by or entry.hidden_by or entry.entity_category or entry.platform == DOMAIN:
            continue
        state = hass.states.get(entry.entity_id)
        if state is None or state.state == "unavailable":
            continue
        if entry.domain == "cover" and (entry.device_class or entry.original_device_class) not in SHADES:
            continue
        area_id = entry.area_id
        if not area_id and entry.device_id:
            device = devices.async_get(entry.device_id)
            area_id = device.area_id if device else None
        if not area_id or area_id in hide or areas.async_get_area(area_id) is None:
            continue
        rooms.setdefault(area_id, []).append({"id": entry.entity_id, "kind": entry.domain})
    out = []
    order = {a.id: i for i, a in enumerate(areas.async_list_areas())}
    for area_id in sorted(rooms, key=lambda a: order.get(a, 999)):
        room = areas.async_get_area(area_id).name
        things = []
        for t in rooms[area_id]:
            state = hass.states.get(t["id"])
            things.append({**t, "name": _short(str(state.attributes.get("friendly_name") or t["id"]), room)})
        kinds = {k: i for i, k in enumerate(("scene", "light", "fan", "cover", "climate", "media_player"))}
        things.sort(key=lambda t: (kinds[t["kind"]], t["name"].lower()))
        out.append({"room": room, "things": things})
    return out


def _now_of(hass: HomeAssistant, thing: dict[str, Any]) -> dict[str, Any]:
    """What the phone shows for one thing right now."""
    s = hass.states.get(thing["id"])
    if s is None:
        return {**thing, "state": "unavailable"}
    a = s.attributes
    out: dict[str, Any] = {**thing, "state": s.state}
    if thing["kind"] == "light" and s.state == "on" and a.get("brightness") is not None:
        out["level"] = round(int(a["brightness"]) / 2.55)
    elif thing["kind"] == "media_player":
        if a.get("volume_level") is not None:
            out["volume"] = round(float(a["volume_level"]) * 100)
        title = a.get("media_title")
        if title and s.state in ("playing", "paused"):
            out["title"] = str(title)[:80]
            if a.get("media_artist"):
                out["artist"] = str(a["media_artist"])[:60]
    elif thing["kind"] == "climate":
        out["now"] = a.get("current_temperature")
        out["target"] = a.get("temperature")
        out["unit"] = hass.config.units.temperature_unit
    elif thing["kind"] == "cover" and a.get("current_position") is not None:
        out["level"] = a["current_position"]
    return out


# ---- The link ----


class GuestLinks:
    """One link at a time, kept in Home Assistant's storage so a restart keeps it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, 1, STORAGE_KEY)
        self.link: dict[str, Any] | None = None
        self.on = False
        self._wrong: dict[str, deque[float]] = {}
        self._calls: deque[float] = deque()
        self._listeners: list[Any] = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self.link = data.get("link") if isinstance(data, dict) else None

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"link": self.link}, 1)
        for cb in list(self._listeners):
            cb()

    @callback
    def async_listen(self, cb: Any) -> Any:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    # The guest switch tells this when guest mode starts and ends.
    @callback
    def guest_changed(self, on: bool) -> None:
        was = self.on
        self.on = on
        if was and not on and self.link:
            _LOGGER.info("Guest mode ended: the guest phone link ended with it")
            self.end()
        elif was != on:
            self._save()

    def current(self) -> dict[str, Any] | None:
        """The link while it is good, else None (and an old one is let go)."""
        link = self.link
        if link and dt_util.parse_datetime(link["ends"]) <= dt_util.utcnow():
            self.end()
            return None
        return link

    def make(self, hours: int | None = None) -> dict[str, Any]:
        """A new link. The old one stops working at once."""
        hours = hours if hours in HOURS else phone_settings(self.hass).get("hours")
        hours = hours if hours in HOURS else DEFAULT_HOURS
        now = dt_util.utcnow()
        self.link = {
            "key": secrets.token_urlsafe(32),
            "made": now.isoformat(),
            "ends": (now + timedelta(hours=hours)).isoformat(),
            "opened": None,
            # Where each thermostat was, so a guest moves it only a little.
            "climate": {
                s.entity_id: s.attributes.get("temperature") for s in self.hass.states.async_all("climate") if isinstance(s.attributes.get("temperature"), (int, float))
            },
        }
        self._calls.clear()
        self._save()
        return self.link

    def end(self) -> None:
        if self.link is not None:
            self.link = None
            self._save()

    def url(self, link: dict[str, Any]) -> str | None:
        return f"{base_url(self.hass)}{PATH}#{link['key']}" if base_url(self.hass) else None

    def summary(self, link: dict[str, Any] | None) -> dict[str, Any]:
        """For a signed-in screen: the address, when it ends, and when the guest first opened it."""
        if not link:
            return {"url": None, "on": self.on, "off": bool(phone_settings(self.hass).get("off"))}
        return {"url": self.url(link), "ends": link["ends"], "opened": link.get("opened"), "on": self.on, "away": bool(away_url(self.hass)), "off": bool(phone_settings(self.hass).get("off"))}

    # ---- The door ----

    def blocked(self, ip: str) -> bool:
        wrong = self._wrong.get(ip)
        if not wrong:
            return False
        cut = time.monotonic() - WRONG_WINDOW
        while wrong and wrong[0] < cut:
            wrong.popleft()
        return len(wrong) >= WRONG_MAX

    def check(self, key: str | None, ip: str) -> dict[str, Any] | None:
        """The link when the key is right. A wrong key is counted against the address."""
        link = self.current()
        if key and link and hmac.compare_digest(key.encode(), str(link["key"]).encode()):
            return link
        if len(self._wrong) > 1000:
            self._wrong.clear()
        self._wrong.setdefault(ip, deque()).append(time.monotonic())
        return None

    def busy(self) -> bool:
        now = time.monotonic()
        while self._calls and self._calls[0] < now - CALLS_WINDOW:
            self._calls.popleft()
        if len(self._calls) >= CALLS_MAX:
            return True
        self._calls.append(now)
        return False

    def opened(self, link: dict[str, Any]) -> None:
        if not link.get("opened"):
            link["opened"] = dt_util.utcnow().isoformat()
            self._save()
            self.hass.bus.async_fire(EVENT_OPENED, {"ends": link["ends"]})
            _LOGGER.info("The guest opened the phone link")


def away_url(hass: HomeAssistant) -> str | None:
    try:
        return get_url(hass, allow_internal=False, allow_ip=False, require_ssl=True, require_standard_port=True, allow_cloud=True)
    except NoURLAvailableError:
        return None


def base_url(hass: HomeAssistant) -> str | None:
    """Away from home first (Home Assistant Cloud or the house's own https address), else the home address:
    a guest on the home Wi-Fi reaches that too."""
    found = away_url(hass)
    if found:
        return found
    try:
        return get_url(hass, allow_external=False, allow_cloud=False)
    except NoURLAvailableError:
        return None


# ---- What a guest may do ----


def _number(value: Any, low: float, high: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("not a number") from err
    if n != n:  # NaN
        raise vol.Invalid("not a number")
    return max(low, min(high, n))


def plan(hass: HomeAssistant, link: dict[str, Any], entity_id: str, action: str, value: Any) -> tuple[str, str, dict[str, Any]] | None:
    """The one service call a guest's tap may make, or None when it is not allowed."""
    allowed = {t["id"]: t["kind"] for room in guest_things(hass) for t in room["things"]}
    kind = allowed.get(entity_id)
    if kind is None:
        return None
    target = {"entity_id": entity_id}
    if kind == "light":
        if action == "toggle":
            return ("light", "toggle", target)
        if action == "level":
            return ("light", "turn_on", {**target, "brightness_pct": round(_number(value, 1, 100))})
        if action == "off":
            return ("light", "turn_off", target)
    elif kind == "scene" and action == "on":
        return ("scene", "turn_on", target)
    elif kind == "fan" and action == "toggle":
        return ("fan", "toggle", target)
    elif kind == "cover" and action in ("open", "close", "stop"):
        return ("cover", f"{action}_cover", target)
    elif kind == "media_player":
        if action in ("play_pause", "next"):
            return ("media_player", "media_play_pause" if action == "play_pause" else "media_next_track", target)
        if action == "volume":
            return ("media_player", "volume_set", {**target, "volume_level": round(_number(value, 0, 100)) / 100})
    elif kind == "climate" and action == "target":
        state = hass.states.get(entity_id)
        base = (link.get("climate") or {}).get(entity_id)
        if state is None or not isinstance(base, (int, float)):
            return None
        low = max(float(state.attributes.get("min_temp", base - CLIMATE_RANGE)), base - CLIMATE_RANGE)
        high = min(float(state.attributes.get("max_temp", base + CLIMATE_RANGE)), base + CLIMATE_RANGE)
        return ("climate", "set_temperature", {**target, "temperature": round(_number(value, low, high) * 2) / 2})
    return None


def page_data(hass: HomeAssistant, link: dict[str, Any], links: GuestLinks) -> dict[str, Any]:
    g = guest_settings(hass)
    wifi = g.get("wifi") if isinstance(g.get("wifi"), dict) else None
    calls = [
        {"name": str(c.get("name") or "")[:40], "phone": str(c.get("phone") or "")[:30]}
        for c in (g.get("calls") or [])
        if isinstance(c, dict) and c.get("phone")
    ][:4]
    out: dict[str, Any] = {
        "home": hass.config.location_name,
        "ends": link["ends"],
        "on": links.on,
        "language": hass.config.language,
    }
    if not links.on:
        # Before guest mode starts, the page only says that it waits: nothing of the house yet.
        return out
    rooms = []
    for room in guest_things(hass):
        rooms.append({"room": room["room"], "things": [_now_of(hass, t) for t in room["things"]]})
    shown = {t["id"] for room in rooms for t in room["things"]}
    climate_range = {eid: [base - CLIMATE_RANGE, base + CLIMATE_RANGE] for eid, base in (link.get("climate") or {}).items() if eid in shown}
    out.update({"rooms": rooms, "note": str(g.get("note") or "")[:2000], "calls": calls, "range": climate_range})
    if wifi and wifi.get("ssid"):
        out["wifi"] = {"ssid": wifi.get("ssid"), "password": wifi.get("password") if wifi.get("security") != "nopass" else None}
    return out


# ---- The page and its two calls ----

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
}
_PAGE_HEADERS = {
    **_HEADERS,
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def _ip(request: web.Request) -> str:
    return str(request.remote or "?")


class GuestPageView(HomeAssistantView):
    """/api/hadash/guest: the page itself. It holds nothing of the house; it asks with the key from its address."""

    url = PATH
    name = "hadash:guest"
    requires_auth = False

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass = request.app["hass"]
        text = await hass.async_add_executor_job(PAGE.read_text)
        return web.Response(text=text, content_type="text/html", headers=_PAGE_HEADERS)


class GuestStateView(HomeAssistantView):
    """/api/hadash/guest/state (GET) and /api/hadash/guest/do (POST), with the key in the X-Hadash-Guest header."""

    url = f"{PATH}/state"
    extra_urls = [f"{PATH}/do"]
    name = "hadash:guest:state"
    requires_auth = False

    def _door(self, request: web.Request) -> tuple[GuestLinks, dict[str, Any] | None, web.Response | None]:
        hass = request.app["hass"]
        links: GuestLinks = hass.data[LINKS]
        ip = _ip(request)
        if links.blocked(ip):
            return links, None, web.json_response({"error": "wait"}, status=429, headers=_HEADERS)
        link = links.check(request.headers.get(HEADER), ip)
        if link is None:
            return links, None, web.json_response({"error": "ended"}, status=401, headers=_HEADERS)
        if links.busy():
            return links, None, web.json_response({"error": "wait"}, status=429, headers=_HEADERS)
        return links, link, None

    async def get(self, request: web.Request) -> web.Response:
        if not request.path.endswith("/state"):
            raise web.HTTPMethodNotAllowed("GET", ["POST"])
        links, link, refused = self._door(request)
        if refused is not None:
            return refused
        hass = request.app["hass"]
        if links.on:
            links.opened(link)
        return web.json_response(page_data(hass, link, links), headers=_HEADERS)

    async def post(self, request: web.Request) -> web.Response:
        if not request.path.endswith("/do"):
            raise web.HTTPMethodNotAllowed("POST", ["GET"])
        links, link, refused = self._door(request)
        if refused is not None:
            return refused
        if not links.on:
            return web.json_response({"error": "off"}, status=403, headers=_HEADERS)
        if (request.content_length or 0) > 2000:
            return web.json_response({"error": "bad"}, status=400, headers=_HEADERS)
        try:
            body = await request.json()
            call = plan(request.app["hass"], link, str(body.get("id") or ""), str(body.get("action") or ""), body.get("value"))
        except (ValueError, AttributeError, vol.Invalid):
            call = None
        if call is None:
            return web.json_response({"error": "not allowed"}, status=403, headers=_HEADERS)
        hass = request.app["hass"]
        domain, service, data = call
        try:
            await hass.services.async_call(domain, service, data, blocking=True)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Guest phone: %s.%s did not work", domain, service, exc_info=True)
            return web.json_response({"error": "failed"}, status=502, headers=_HEADERS)
        return web.json_response({"ok": True}, headers=_HEADERS)


# ---- For the signed-in screens: the wall's code and the studio ----


@websocket_api.websocket_command({vol.Required("type"): "hadash/guest/link", vol.Optional("new"): bool, vol.Optional("hours"): vol.In(HOURS)})
@callback
def ws_guest_link(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """{url, ends, opened, on}: the guest link, made when there is none. Any signed-in user: a wall tablet in
    guest mode shows it. A new link (new: true) ends the old one, so only an admin may ask for that."""
    links: GuestLinks = hass.data[LINKS]
    if phone_settings(hass).get("off"):
        connection.send_result(msg["id"], links.summary(None))
        return
    link = links.current()
    if msg.get("new") or "hours" in msg:
        if not connection.user or not connection.user.is_admin:
            connection.send_error(msg["id"], "unauthorized", "Only an admin can make a new guest link.")
            return
        link = links.make(msg.get("hours"))
    elif link is None:
        # A wall shows the code only in guest mode; the studio may make one ahead, to send it.
        if not links.on and not (connection.user and connection.user.is_admin):
            connection.send_result(msg["id"], links.summary(None))
            return
        link = links.make()
    connection.send_result(msg["id"], links.summary(link))


@websocket_api.websocket_command({vol.Required("type"): "hadash/guest/link/end"})
@callback
def ws_guest_link_end(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Ends the guest link now. Anyone signed in may: ending is always safe."""
    hass.data[LINKS].end()
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({vol.Required("type"): "hadash/guest/link/subscribe"})
@callback
def ws_guest_link_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Tells a screen when the link changes (made, opened, ended), so the studio shows "Opened at 6:40 PM"."""
    links: GuestLinks = hass.data[LINKS]

    @callback
    def changed() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], links.summary(links.link)))

    connection.subscriptions[msg["id"]] = links.async_listen(changed)
    connection.send_result(msg["id"])


async def async_setup(hass: HomeAssistant) -> None:
    links = GuestLinks(hass)
    await links.async_load()
    hass.data[LINKS] = links
    # Leaving rooms out or turning the phone off in the studio takes effect at once.
    hass.data[DOMAIN].async_listen(lambda _data: links.end() if phone_settings(hass).get("off") else None)
    hass.http.register_view(GuestPageView())
    hass.http.register_view(GuestStateView())
    websocket_api.async_register_command(hass, ws_guest_link)
    websocket_api.async_register_command(hass, ws_guest_link_end)
    websocket_api.async_register_command(hass, ws_guest_link_subscribe)
