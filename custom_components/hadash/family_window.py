"""Family window (Pro): live video between your wall and a family home's wall, over the Care link.

Care for family homes (care.py) already links two homes with Dashboard Studio: the shared home (for example your
parents') gave your home a care code, and your Home Assistant reaches it over its own address. The family window
uses that same link, so it needs no account, no phone number, no app and no new device: a wall tablet's own front
camera and microphone.

How the two homes talk:
1. Your Home Assistant (the watching side) keeps one quiet websocket open to the shared home:
   /api/hadash/care/<link>/window, opened with the care key. A home that cannot reach you (your parents' home
   does not know your address) can still ring you over it. Nothing flows on it until someone calls.
2. A call: the caller's tablet sends small pictures (about 6 a second, 320 wide, JPEG) and its sound (16 kHz mu-law
   pieces, the same as Live talk) to its own Home Assistant, which passes them over the link to the other Home
   Assistant, which passes them to the tablet that answered. No WebRTC: it works on plain http tablets at home,
   through any router, with no outside server.

Dignity first, the same as Care:
1. The tablet that is called never sends its picture or sound before someone there answers. The caller is seen
   ringing, like a face at the window, without sound.
2. A family member's wall can always say "Not now". No answer in 40 seconds is a missed window, and the caller's
   wall says so.
3. A paused shared home (Private until tomorrow) does not ring.
4. The link's key opens only this window and the care summary: never a camera of the house, never a room.

Messages between the two Home Assistants (JSON text on the link):
  {"t": "hi", "name": "Radu"}                 the watching side says who it is
  {"t": "ring", "call": id, "name": "Radu"}   a call starts
  {"t": "answer", "call": id}                 the other side answered
  {"t": "end", "call": id, "why": "..."}      ended, declined, missed, busy, lost
  {"t": "v", "call": id, "d": b64}             one picture (JPEG, upright)
  {"t": "a", "call": id, "d": b64}             one tenth of a second of sound
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any, Callable

from aiohttp import ClientError, WSMsgType, web
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.start import async_at_started

from .care import CARE, HEADER_FROM, HEADER_KEY, Care

_LOGGER = logging.getLogger(__name__)

WINDOW = "hadash_family_window"
VIEW_URL = "/api/hadash/care/{link_id}/window"

RING_S = 40
LOST_S = 20
ENDED_SHOW_S = 6
RING_GAP_S = 15
RETRY_S = (10, 30, 60, 120, 300)
MAX_PICTURE = 90_000
MAX_SOUND = 6_000
PICTURES_PER_S = 12
SOUNDS_PER_S = 15
WHY = {"ended", "declined", "missed", "busy", "lost", "private"}


class Peer:
    """One family link: a websocket to a family home (or from it), and its name."""

    def __init__(self, key: str, name: str, send: Callable[[dict[str, Any]], Any]) -> None:
        self.key = key
        self.name = name
        self.send = send
        self.rings: list[float] = []


class Call:
    def __init__(self, call_id: str, peer: str, name: str, incoming: bool, screen: str | None) -> None:
        self.id = call_id
        self.peer = peer
        self.name = name
        self.incoming = incoming
        # The tablet that has the call: the caller's, or the one that answered.
        self.screen = screen
        self.state = "ringing"
        self.why: str | None = None
        self.started = time.time()
        self.heard = time.time()
        self.pictures: list[float] = []
        self.sounds: list[float] = []


class FamilyWindow:
    def __init__(self, hass: HomeAssistant, care: Care) -> None:
        self.hass = hass
        self.care = care
        self.peers: dict[str, Peer] = {}
        self.call: Call | None = None
        self.ended: dict[str, Any] | None = None
        # Each tablet that listens: (screen, send).
        self._tablets: dict[int, tuple[str, Callable[[dict[str, Any]], None]]] = {}
        self._n = 0
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = False
        self._timer: asyncio.TimerHandle | None = None
        self._later: asyncio.TimerHandle | None = None

    # ---- the tablets in this home ----

    def add_tablet(self, screen: str, send: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._n += 1
        n = self._n
        self._tablets[n] = (screen, send)

        def remove() -> None:
            self._tablets.pop(n, None)
            # The tablet that had the call went away (closed, reloaded): the call ends after a moment of silence.

        return remove

    def state(self) -> dict[str, Any]:
        people = []
        for h in self.care.homes:
            people.append({"peer": f"h:{h['id']}", "name": h["name"], "online": f"h:{h['id']}" in self.peers})
        for link in self.care.links:
            if link.get("seen"):
                people.append({"peer": f"l:{link['id']}", "name": link.get("name") or "", "online": f"l:{link['id']}" in self.peers})
        c = self.call
        call = None
        if c:
            call = {"id": c.id, "peer": c.peer, "name": c.name, "incoming": c.incoming, "state": c.state, "tablet": c.screen, "since": c.started}
        elif self.ended and time.time() - self.ended["at"] < ENDED_SHOW_S:
            call = self.ended
        return {"people": people, "call": call, "pro": self.care._pro()}

    def _tell(self) -> None:
        s = {"type": "state", **self.state()}
        for _screen, send in list(self._tablets.values()):
            send(s)

    def _to_call_tablet(self, msg: dict[str, Any], ringing_all: bool = False) -> None:
        c = self.call
        if not c:
            return
        for screen, send in list(self._tablets.values()):
            if screen == c.screen or (ringing_all and c.state == "ringing" and c.incoming):
                send(msg)

    # ---- calls ----

    def start_call(self, peer: str, screen: str, me: str) -> dict[str, Any]:
        # Watching a family home is Pro. The shared home may always call back over the link the family keeps open.
        if peer.startswith("h:") and not self.care._pro():
            return {"ok": False, "error": "pro"}
        p = self.peers.get(peer)
        if p is None:
            return {"ok": False, "error": "offline"}
        if self.call:
            return {"ok": False, "error": "busy"}
        c = Call(secrets.token_hex(8), peer, p.name, False, screen)
        self.call = c
        self.ended = None
        self._send(p, {"t": "ring", "call": c.id, "name": me[:40]})
        self._arm()
        self._tell()
        return {"ok": True, "call": c.id}

    def answer(self, call_id: str, screen: str) -> bool:
        c = self.call
        if not c or c.id != call_id or not c.incoming or c.state != "ringing":
            return False
        c.state = "open"
        c.screen = screen
        c.heard = time.time()
        if p := self.peers.get(c.peer):
            self._send(p, {"t": "answer", "call": c.id})
        self._tell()
        return True

    def end(self, call_id: str, why: str = "ended", tell_peer: bool = True) -> None:
        c = self.call
        if not c or c.id != call_id:
            return
        if tell_peer and (p := self.peers.get(c.peer)):
            self._send(p, {"t": "end", "call": c.id, "why": why})
        self.call = None
        self.ended = {"id": c.id, "peer": c.peer, "name": c.name, "incoming": c.incoming, "state": "ended", "why": why, "tablet": c.screen, "at": time.time()}
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._tell()
        # The ended line goes away by itself.
        if self._later:
            self._later.cancel()
        self._later = self.hass.loop.call_later(ENDED_SHOW_S + 0.5, self._tell)

    def media(self, call_id: str, screen: str, kind: str, data: str) -> None:
        """A picture or sound from this home's tablet, for the family home."""
        c = self.call
        if not c or c.id != call_id or c.screen != screen:
            return
        # The called side sends nothing before it answers.
        if c.incoming and c.state != "open":
            return
        if not self._fair(c, kind, data):
            return
        if p := self.peers.get(c.peer):
            self._send(p, {"t": kind, "call": c.id, "d": data})

    def _fair(self, c: Call, kind: str, data: str) -> bool:
        now = time.time()
        if kind == "v":
            c.pictures = [x for x in c.pictures if now - x < 1]
            if len(c.pictures) >= PICTURES_PER_S or len(data) > MAX_PICTURE:
                return False
            c.pictures.append(now)
            return True
        c.sounds = [x for x in c.sounds if now - x < 1]
        if len(c.sounds) >= SOUNDS_PER_S or len(data) > MAX_SOUND:
            return False
        c.sounds.append(now)
        return True

    def _arm(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = self.hass.loop.call_later(1, self._check)

    @callback
    def _check(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = None
        c = self.call
        if not c:
            return
        now = time.time()
        if c.state == "ringing" and now - c.started > RING_S:
            self.end(c.id, "missed")
            return
        if c.state == "open" and now - c.heard > LOST_S:
            self.end(c.id, "lost")
            return
        if c.peer not in self.peers:
            self.end(c.id, "lost", tell_peer=False)
            return
        self._arm()

    # ---- from the family home ----

    def from_peer(self, peer: Peer, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        call_id = str(msg.get("call") or "")[:32]
        c = self.call
        if kind == "hi":
            name = str(msg.get("name") or "")[:40]
            if name:
                peer.name = name
            self._tell()
            return
        if kind == "ring":
            now = time.time()
            peer.rings = [x for x in peer.rings if now - x < RING_GAP_S]
            if peer.rings:
                return
            peer.rings.append(now)
            if c is not None:
                self._send(peer, {"t": "end", "call": call_id, "why": "busy"})
                return
            if peer.key.startswith("l:") and self.care.paused_until > now:
                self._send(peer, {"t": "end", "call": call_id, "why": "private"})
                return
            name = str(msg.get("name") or "")[:40] or peer.name
            self.call = Call(call_id, peer.key, name, True, None)
            self.ended = None
            self._arm()
            self._tell()
            return
        if not c or c.id != call_id or c.peer != peer.key:
            return
        if kind == "answer" and not c.incoming and c.state == "ringing":
            c.state = "open"
            c.heard = time.time()
            self._tell()
        elif kind == "end":
            why = str(msg.get("why") or "ended")
            self.end(c.id, why if why in WHY else "ended", tell_peer=False)
        elif kind in ("v", "a") and isinstance(msg.get("d"), str):
            c.heard = time.time()
            # While it rings here, every tablet sees who is at the window; sound only after someone answers.
            if c.state == "ringing" and c.incoming:
                if kind == "v" and len(msg["d"]) <= MAX_PICTURE:
                    self._to_call_tablet({"type": "v", "call": c.id, "d": msg["d"]}, ringing_all=True)
                return
            if c.state == "open" and len(msg["d"]) <= (MAX_PICTURE if kind == "v" else MAX_SOUND):
                self._to_call_tablet({"type": kind, "call": c.id, "d": msg["d"]})

    def _send(self, peer: Peer, msg: dict[str, Any]) -> None:
        try:
            result = peer.send(msg)
            if asyncio.iscoroutine(result):
                task = self.hass.async_create_background_task(result, "hadash family window send")
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except Exception:  # noqa: BLE001 - a link that closed; it comes back by itself
            _LOGGER.debug("Family window: send failed", exc_info=True)

    def joined(self, peer: Peer) -> None:
        self.peers[peer.key] = peer
        self._tell()

    def left(self, peer: Peer) -> None:
        if self.peers.get(peer.key) is peer:
            del self.peers[peer.key]
        if self.call and self.call.peer == peer.key:
            self.end(self.call.id, "lost", tell_peer=False)
        self._tell()

    # ---- the watching side keeps one link open to each family home ----

    def keep_links(self) -> None:
        """Opens a link to each family home that has none (Pro), and closes the ones for removed homes."""
        if not self.care.homes and not self._tasks:
            return
        wanted = {h["id"]: h for h in self.care.homes} if not self._stop and self.care._pro() else {}
        for home_id, task in list(self._tasks.items()):
            if home_id not in wanted:
                task.cancel()
                del self._tasks[home_id]
        for home_id, home in wanted.items():
            if home_id not in self._tasks or self._tasks[home_id].done():
                self._tasks[home_id] = self.hass.async_create_background_task(self._link(home_id), f"hadash family window {home_id}")

    async def _link(self, home_id: str) -> None:
        tries = 0
        session = async_get_clientsession(self.hass)
        while not self._stop:
            home = next((h for h in self.care.homes if h["id"] == home_id), None)
            if home is None:
                return
            url = f"{home['url']}/api/hadash/care/{home['id']}/window"
            headers = {HEADER_KEY: home["key"], HEADER_FROM: home.get("me") or self.hass.config.location_name}
            try:
                async with session.ws_connect(url, headers=headers, heartbeat=30, timeout=15, max_msg_size=256_000) as ws:
                    tries = 0
                    peer = Peer(f"h:{home_id}", home["name"], ws.send_json)
                    await ws.send_json({"t": "hi", "name": home.get("me") or ""})
                    self.joined(peer)
                    try:
                        async for m in ws:
                            if m.type == WSMsgType.TEXT:
                                self._got(peer, m.data)
                            elif m.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                                break
                    finally:
                        self.left(peer)
            except asyncio.CancelledError:
                raise
            except (ClientError, TimeoutError, OSError, ValueError, RuntimeError):
                _LOGGER.debug("Family window: no link to %s yet", home.get("name"), exc_info=True)
            await asyncio.sleep(RETRY_S[min(tries, len(RETRY_S) - 1)])
            tries += 1

    def _got(self, peer: Peer, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if isinstance(msg, dict):
            self.from_peer(peer, msg)

    def stop(self) -> None:
        self._stop = True
        for handle in (self._timer, self._later):
            if handle:
                handle.cancel()
        self._timer = self._later = None
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()


class WindowView(HomeAssistantView):
    """The shared home's side of the link: a websocket, opened only with the care key."""

    url = VIEW_URL
    name = "api:hadash:care:window"
    requires_auth = False
    cors_allowed = False

    def __init__(self, window: FamilyWindow) -> None:
        self.window = window

    async def get(self, request: web.Request, link_id: str) -> web.StreamResponse:
        care = self.window.care
        remote = request.remote or "?"
        if care.blocked(remote):
            return web.json_response({"error": "wait"}, status=429)
        link = care.check(link_id, request.headers.get(HEADER_KEY), remote)
        if link is None:
            return web.json_response({"error": "no"}, status=403)
        care.seen_by(link, request.headers.get(HEADER_FROM))
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=256_000)
        await ws.prepare(request)
        peer = Peer(f"l:{link_id}", link.get("name") or "", ws.send_json)
        # The same family home opening a new link (its Home Assistant restarted) takes the place of the old one.
        self.window.joined(peer)
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    # A link removed in the Studio stops at once.
                    if not any(x["id"] == link_id for x in care.links):
                        break
                    self.window._got(peer, m.data)
                elif m.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.window.left(peer)
        return ws


async def async_setup(hass: HomeAssistant) -> None:
    care: Care = hass.data[CARE]
    window = FamilyWindow(hass, care)
    hass.data[WINDOW] = window
    hass.http.register_view(WindowView(window))
    for command in (ws_subscribe, ws_call, ws_answer, ws_end, ws_send):
        websocket_api.async_register_command(hass, command)

    # Links follow the family homes: a home added or removed, or Pro turned on or off.
    @callback
    def follow(_state: dict[str, Any]) -> None:
        window.keep_links()
        window._tell()

    care._listeners.append(follow)
    # The links open once Home Assistant has started (the license is read by then).
    async_at_started(hass, lambda _hass: window.keep_links())

    @callback
    def on_stop(_event: Event) -> None:
        window.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)


@websocket_api.websocket_command({vol.Required("type"): "hadash/window/subscribe", vol.Optional("tablet", default=""): vol.All(str, vol.Length(max=80))})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    window: FamilyWindow = hass.data[WINDOW]
    screen = msg["tablet"] or f"tablet-{msg['id']}-{id(connection)}"

    @callback
    def send(event: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    connection.subscriptions[msg["id"]] = window.add_tablet(screen, send)
    connection.send_result(msg["id"], {"tablet": screen})
    send({"type": "state", **window.state()})


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/window/call", vol.Required("peer"): vol.All(str, vol.Length(max=80)), vol.Required("tablet"): vol.All(str, vol.Length(max=80))}
)
@callback
def ws_call(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    window: FamilyWindow = hass.data[WINDOW]
    peer = msg["peer"]
    me = ""
    if peer.startswith("h:"):
        home = next((h for h in window.care.homes if f"h:{h['id']}" == peer), None)
        me = (home or {}).get("me") or ""
    if not me:
        me = hass.config.location_name or ""
    connection.send_result(msg["id"], window.start_call(peer, msg["tablet"], me))


@websocket_api.websocket_command({vol.Required("type"): "hadash/window/answer", vol.Required("call"): str, vol.Required("tablet"): vol.All(str, vol.Length(max=80))})
@callback
def ws_answer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], {"ok": hass.data[WINDOW].answer(msg["call"], msg["tablet"])})


@websocket_api.websocket_command({vol.Required("type"): "hadash/window/end", vol.Required("call"): str, vol.Optional("why", default="ended"): vol.In(sorted(WHY))})
@callback
def ws_end(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    window: FamilyWindow = hass.data[WINDOW]
    window.end(msg["call"], msg["why"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/window/send",
        vol.Required("call"): str,
        vol.Required("tablet"): vol.All(str, vol.Length(max=80)),
        vol.Optional("v"): vol.All(str, vol.Length(max=MAX_PICTURE)),
        vol.Optional("a"): vol.All(str, vol.Length(max=MAX_SOUND)),
    }
)
@callback
def ws_send(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    window: FamilyWindow = hass.data[WINDOW]
    if "v" in msg:
        window.media(msg["call"], msg["tablet"], "v", msg["v"])
    if "a" in msg:
        window.media(msg["call"], msg["tablet"], "a", msg["a"])
    connection.send_result(msg["id"])
