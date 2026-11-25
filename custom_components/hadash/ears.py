"""A tablet as a TV's ears (Pro): talk to the tablet next to the TV, and the TV listens, shows and answers.

A Fire TV has no microphone the app can use. In the studio, a TV screen picks a tablet near it ("Listen with").
While the TV shows the dashboard, the wake word heard by that tablet goes to the TV: the tablet's microphone sound
passes through Home Assistant to the TV's page, which runs the usual talk (its look, the answer card, the wall
commands, the voice) as if the microphone were its own. When the TV is off or shows something else, the tablet
answers itself, as before.

Over the same websocket as everything else:
1. "hadash/ears/tablet": a tablet says who it is. It hears when its TV is ready, and when to open or close the mic.
2. "hadash/ears/tv": a TV page says which tablet it listens with, and whether it is on the screen now.
3. "hadash/ears/wake": the tablet heard the wake word. If its TV is ready, the TV starts to listen.
4. "hadash/ears/audio": the tablet's sound (16 kHz, 16-bit mono, base64), passed to the TV as it comes.
5. "hadash/ears/open" and "hadash/ears/close": the TV wants the microphone (a follow-up), or is done with it.
6. "hadash/ears/state": what the TV does now (thinks, speaks, its words), so the tablet can show a calm sign of it.
7. "hadash/ears/stop": a tap on the tablet stops the talk on the TV.
8. "hadash/ears/tablets": the studio's list of tablets that can listen.

The same tablet is also the TV's eyes (hand gestures, custom_components/hadash/gestures.py): the tablet hears
"eyes" with the TV to watch for. While the TV shows the dashboard, the hand works the dashboard. While another app
is in front of a Fire TV that is awake (YouTube, Netflix), the hand works that app like the remote.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, PRO, SATELLITES, TVS

EARS = "hadash_ears"
# The most sound one message may carry (a quarter of a second is about 11 kB as base64).
MAX_CHUNK = 64_000

Send = Callable[[dict[str, Any]], None]


@dataclass
class TabletSide:
    name: str
    send: Send


@dataclass
class TvSide:
    screen: str
    name: str
    visible: bool
    send: Send


class Ears:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.tablets: dict[str, TabletSide] = {}
        # The TV pages, by the tablet each one listens with. Two TVs may pick the same tablet: the one on screen wins.
        self.tvs: dict[str, list[TvSide]] = {}
        # Tablets whose sound goes to a TV right now.
        self.open: dict[str, TvSide] = {}
        # Each TV page by its connection and tablet, so its later commands find it.
        self.by_connection: dict[tuple[int, str], TvSide] = {}
        # What each tablet was last told to watch for, so a Fire TV going on or off tells it only once.
        self.eyes_sent: dict[str, dict[str, Any] | None] = {}

    @property
    def pro(self) -> bool:
        return self.hass.data[PRO].state()["plan"] != "free"

    def tv_for(self, tablet: str) -> TvSide | None:
        """The TV this tablet listens for now: on a Pro plan, while that TV shows the dashboard."""
        if not self.pro:
            return None
        return next((tv for tv in reversed(self.tvs.get(tablet, [])) if tv.visible), None)

    def firetv_of(self, screen: str) -> str | None:
        """The Fire TV that shows this screen (set up with "Add a Fire TV"), by its address."""
        store = self.hass.data.get(TVS)
        tvs: dict[str, dict[str, Any]] = getattr(store, "tvs", {}) or {}
        return next((host for host, info in tvs.items() if screen and info.get("screen") == screen), None)

    def picked_by(self, tablet: str) -> list[tuple[str, str]]:
        """The screens (id, name) that picked this tablet under "Listen with" in the published layout. A TV in
        another app has no dashboard page that could say so, for example right after Home Assistant restarts."""
        layout = getattr(self.hass.data.get(DOMAIN), "layout", None) or {}
        found = []
        for screen in layout.get("screens", []) if isinstance(layout, dict) else []:
            voice = screen.get("voice") if isinstance(screen, dict) else None
            if isinstance(voice, dict) and voice.get("ears") == tablet and screen.get("id"):
                found.append((str(screen["id"]), str(screen.get("name") or screen["id"])))
        return found

    def awake(self, host: str) -> bool:
        """The Fire TV is on (its media player in Home Assistant), whatever app is in front."""
        for state in self.hass.states.async_all("media_player"):
            if state.attributes.get("firetv_host") == host:
                return state.state not in ("off", "standby", "unavailable", "unknown")
        return False

    def eyes_for(self, tablet: str) -> dict[str, Any] | None:
        """The TV this tablet watches hands for now: its dashboard on the screen, or another app on its Fire TV."""
        if not self.pro:
            return None
        sides = list(reversed(self.tvs.get(tablet, [])))
        tv = next((t for t in sides if t.visible), None)
        if tv is not None:
            return {"screen": tv.screen, "name": tv.name, "dashboard": True, "firetv": self.firetv_of(tv.screen)}
        for screen, name in [*((t.screen, t.name) for t in sides), *self.picked_by(tablet)]:
            host = self.firetv_of(screen)
            if host and self.awake(host):
                return {"screen": screen, "name": name, "dashboard": False, "firetv": host}
        return None

    @callback
    def changed(self, tablet: str) -> None:
        """Tells the tablet and its TVs what they need to know about each other."""
        side = self.tablets.get(tablet)
        tv = self.tv_for(tablet)
        if side is not None:
            side.send({"action": "tv", "tv": {"screen": tv.screen, "name": tv.name} if tv else None})
            eyes = self.eyes_for(tablet)
            if eyes != self.eyes_sent.get(tablet):
                self.eyes_sent[tablet] = eyes
                side.send({"action": "eyes", "tv": eyes})
        for each in self.tvs.get(tablet, []):
            each.send({"action": "tablet", "on": side is not None and self.pro, "name": side.name if side else ""})
        if tablet in self.open and self.open[tablet] is not tv:
            self.close(tablet)

    @callback
    def changed_all(self) -> None:
        for tablet in {*self.tablets, *self.tvs}:
            self.changed(tablet)

    @callback
    def close(self, tablet: str) -> None:
        tv = self.open.pop(tablet, None)
        side = self.tablets.get(tablet)
        if side is not None:
            side.send({"action": "close"})
        if tv is not None:
            tv.send({"action": "closed"})

    @callback
    def wake(self, tablet: str, level: float | None, word: str) -> TvSide | None:
        tv = self.tv_for(tablet)
        if tv is None or tablet not in self.tablets:
            return None
        self.open[tablet] = tv
        tv.send({"action": "wake", "level": level, "word": word})
        return tv

    @callback
    def audio(self, tablet: str, data: str) -> None:
        tv = self.open.get(tablet)
        if tv is not None:
            tv.send({"action": "audio", "data": data})

    @callback
    def ask_open(self, tablet: str, tv: TvSide) -> bool:
        side = self.tablets.get(tablet)
        if side is None or self.tv_for(tablet) is not tv:
            return False
        self.open[tablet] = tv
        side.send({"action": "open"})
        return True


@callback
def async_register(hass: HomeAssistant) -> None:
    ears = hass.data[EARS] = Ears(hass)
    hass.data[PRO].async_listen(lambda *_: ears.changed_all())
    # A new "Listen with" pick in the studio: the tablets learn what to watch at once.
    layouts = hass.data.get(DOMAIN)
    if layouts is not None and hasattr(layouts, "async_listen"):
        layouts.async_listen(lambda *_: ears.changed_all())

    @callback
    def fire_tv_changed(event: Event) -> None:
        # A Fire TV went on or off: its tablet starts or stops watching for hands while another app is in front.
        new, old = event.data.get("new_state"), event.data.get("old_state")
        if not ears.tablets or not any(s is not None and s.attributes.get("firetv_host") for s in (new, old)):
            return
        if (new.state if new else None) != (old.state if old else None):
            ears.changed_all()

    hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        fire_tv_changed,
        event_filter=callback(lambda data: str(data.get("entity_id", "")).startswith("media_player.")),
    )
    for command in (ws_tablet, ws_tv, ws_seen, ws_wake, ws_audio, ws_open, ws_close, ws_state, ws_stop, ws_tablets):
        websocket_api.async_register_command(hass, command)


TABLET_ID = vol.All(str, vol.Length(min=8, max=64))


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/ears/tablet", vol.Required("tablet"): TABLET_ID, vol.Optional("name", default=""): vol.All(str, vol.Length(max=60))}
)
@callback
def ws_tablet(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A tablet that may be a TV's ears. Works for non-admin logins, like the layout."""
    ears: Ears = hass.data[EARS]
    tablet = msg["tablet"]

    @callback
    def send(command: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], command))

    side = ears.tablets[tablet] = TabletSide(msg["name"].strip(), send)
    ears.eyes_sent.pop(tablet, None)

    @callback
    def gone() -> None:
        # A tablet that reconnected already has a new subscription; only the one that went away ends.
        if ears.tablets.get(tablet) is side:
            ears.tablets.pop(tablet)
            ears.close(tablet)
            ears.changed(tablet)

    connection.subscriptions[msg["id"]] = gone
    connection.send_result(msg["id"])
    ears.changed(tablet)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/ears/tv",
        vol.Required("tablet"): TABLET_ID,
        vol.Optional("screen", default=""): str,
        vol.Optional("name", default=""): vol.All(str, vol.Length(max=60)),
        vol.Optional("visible", default=True): bool,
    }
)
@callback
def ws_tv(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A TV page that listens with a tablet."""
    ears: Ears = hass.data[EARS]
    tablet = msg["tablet"]

    @callback
    def send(command: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], command))

    tv = TvSide(msg["screen"], msg["name"].strip() or msg["screen"] or "TV", msg["visible"], send)
    ears.tvs.setdefault(tablet, []).append(tv)
    key = (id(connection), tablet)
    ears.by_connection[key] = tv

    @callback
    def gone() -> None:
        if ears.by_connection.get(key) is tv:
            ears.by_connection.pop(key)
        _tv_gone(ears, tablet, tv)

    connection.subscriptions[msg["id"]] = gone
    connection.send_result(msg["id"])
    ears.changed(tablet)


@callback
def _tv_gone(ears: Ears, tablet: str, tv: TvSide) -> None:
    sides = ears.tvs.get(tablet, [])
    if tv in sides:
        sides.remove(tv)
    if not sides:
        ears.tvs.pop(tablet, None)
    ears.changed(tablet)


def _tv(connection: websocket_api.ActiveConnection, tablet: str) -> tuple[str, TvSide] | None:
    tv = connection.hass.data[EARS].by_connection.get((id(connection), tablet))
    return (tablet, tv) if tv is not None else None


@websocket_api.websocket_command({vol.Required("type"): "hadash/ears/seen", vol.Required("tablet"): TABLET_ID, vol.Required("visible"): bool})
@callback
def ws_seen(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The TV's dashboard came on the screen, or something else took the screen."""
    ears: Ears = hass.data[EARS]
    found = _tv(connection, msg["tablet"])
    if found is not None:
        tablet, tv = found
        tv.visible = msg["visible"]
        ears.changed(tablet)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/ears/wake",
        vol.Required("tablet"): TABLET_ID,
        vol.Optional("level"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=0, max=120))),
        vol.Optional("word", default=""): vol.All(str, vol.Length(max=40)),
    }
)
@callback
def ws_wake(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The tablet heard the wake word. Its TV listens, or (no TV ready) the tablet answers itself."""
    ears: Ears = hass.data[EARS]
    tv = ears.wake(msg["tablet"], msg.get("level"), msg["word"])
    connection.send_result(msg["id"], {"tv": tv.name if tv else None})


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/ears/audio", vol.Required("tablet"): TABLET_ID, vol.Required("data"): vol.All(str, vol.Length(max=MAX_CHUNK))}
)
@callback
def ws_audio(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    ears: Ears = hass.data[EARS]
    ears.audio(msg["tablet"], msg["data"])
    connection.send_result(msg["id"], {"open": msg["tablet"] in ears.open})


@websocket_api.websocket_command({vol.Required("type"): "hadash/ears/open", vol.Required("tablet"): TABLET_ID})
@callback
def ws_open(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The TV wants to listen again (a follow-up, or a tap on its microphone)."""
    ears: Ears = hass.data[EARS]
    found = _tv(connection, msg["tablet"])
    ok = found is not None and ears.ask_open(found[0], found[1])
    connection.send_result(msg["id"], {"ok": ok})


@websocket_api.websocket_command({vol.Required("type"): "hadash/ears/close", vol.Required("tablet"): TABLET_ID})
@callback
def ws_close(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The TV has heard enough: the tablet's microphone goes off."""
    ears: Ears = hass.data[EARS]
    found = _tv(connection, msg["tablet"])
    if found is not None and ears.open.get(found[0]) is found[1]:
        ears.close(found[0])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/ears/state",
        vol.Required("tablet"): TABLET_ID,
        vol.Required("phase"): vol.In(["idle", "listening", "thinking", "speaking"]),
        vol.Optional("heard", default=""): vol.All(str, vol.Length(max=400)),
        vol.Optional("answer", default=""): vol.All(str, vol.Length(max=600)),
    }
)
@callback
def ws_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """What the TV does with the talk now. The tablet that hears for it shows a calm sign of it."""
    ears: Ears = hass.data[EARS]
    found = _tv(connection, msg["tablet"])
    side = ears.tablets.get(msg["tablet"])
    if found is not None and side is not None and ears.tv_for(found[0]) is found[1]:
        side.send({"action": "state", "phase": msg["phase"], "heard": msg["heard"], "answer": msg["answer"], "name": found[1].name})
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/ears/stop", vol.Required("tablet"): TABLET_ID})
@callback
def ws_stop(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A tap on the tablet: the TV stops listening or speaking."""
    ears: Ears = hass.data[EARS]
    tablet = msg["tablet"]
    tv = ears.open.get(tablet) or ears.tv_for(tablet)
    if tablet in ears.tablets and tv is not None:
        tv.send({"action": "hush"})
        ears.close(tablet)
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/ears/tablets"})
@callback
def ws_tablets(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The tablets a TV can listen with: every wall tablet with a working microphone that Home Assistant knows."""
    ears: Ears = hass.data[EARS]
    satellites = hass.data.get(SATELLITES)
    found: dict[str, dict[str, Any]] = {}
    for t in (satellites.tablets.values() if satellites else []):
        if t.listen:
            found[t.id] = {"id": t.id, "name": t.name or t.model or "Wall tablet", "on": t.id in ears.tablets}
    for tid, side in ears.tablets.items():
        found.setdefault(tid, {"id": tid, "name": side.name or "Wall tablet", "on": True})
    tablets = sorted(found.values(), key=lambda t: (not t["on"], t["name"].casefold()))
    connection.send_result(msg["id"], {"tablets": tablets})
