"""Hand gestures (Pro): a camera next to a TV sees a hand, and the dashboard of that place answers.

The gesture tracker (tools/gestures, on the Mac for now) looks at the camera's video in memory and fires the event
hadash_gesture: {"where": "Kitchen", "gesture": "left"}. Home Assistant passes it on at once to the dashboards whose
screen has that name, and keeps nothing. A thumb up or down steps the volume here, on the TV of that place: the
"entity" the tracker names, or else the one TV in the area with that name.

When the tracker names the Fire TV next to the camera ("firetv": its address, set up with "Add a Fire TV") and
another app is in front there (Netflix, YouTube), the gestures work that app instead: a swipe is the remote's arrow,
one finger OK, a fist Back, a V play or pause, a thumb the TV's volume. Which app is in front is read when the hand
says "ready", and again after a key that may change it, so a gesture never waits for the TV to answer first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

GESTURES_KEY = "hadash_gestures"
EVENT = "hadash_gesture"
GESTURES = {"ready", "gone", "aim", "left", "right", "up", "down", "ok", "back", "playpause", "volume_up", "volume_down"}
VOLUME = {"volume_up": "volume_up", "volume_down": "volume_down"}
# Gesture -> the Fire TV remote's key (tvremote.KEYS).
FIRE_KEYS = {"left": "left", "right": "right", "up": "up", "down": "down", "ok": "ok", "back": "back",
             "playpause": "play_pause", "volume_up": "volume_up", "volume_down": "volume_down"}


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


class Gestures:
    def __init__(self) -> None:
        # Subscription key -> (screen name, send function).
        self.screens: dict[int, tuple[str, Callable[[dict[str, Any]], None]]] = {}
        self._next = 0

    def join(self, where: str, send: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        key = self._next
        self._next += 1
        self.screens[key] = (where, send)

        @callback
        def leave() -> None:
            self.screens.pop(key, None)

        return leave

    def send(self, where: str, message: dict[str, Any]) -> int:
        """Passes a gesture to the screens of that place (all screens when no place is named). Returns how many."""
        count = 0
        for name, send in list(self.screens.values()):
            if where and not _same(name, where):
                continue
            send(message)
            count += 1
        return count


def find_tv(hass: HomeAssistant, where: str) -> str | None:
    """The TV in the area with this name: a media player that says it is a TV, or has "TV" in its name."""
    if not where:
        return None
    areas = ar.async_get(hass)
    area = next((a for a in areas.async_list_areas() if _same(a.name, where)), None)
    if area is None:
        return None
    ents, devs = er.async_get(hass), dr.async_get(hass)
    found: list[tuple[int, str]] = []
    for entry in ents.entities.values():
        if entry.domain != "media_player" or entry.disabled_by:
            continue
        device = devs.async_get(entry.device_id) if entry.device_id else None
        if (entry.area_id or (device.area_id if device else None)) != area.id:
            continue
        state = hass.states.get(entry.entity_id)
        if state is None:
            continue
        name = f"{entry.entity_id} {state.name}".casefold()
        is_tv = state.attributes.get("device_class") == "tv" or "tv" in name.replace("_", " ").split()
        if not is_tv:
            continue
        # A TV that is on comes first.
        found.append((0 if state.state not in ("off", "standby", "unavailable") else 1, entry.entity_id))
    return min(found)[1] if found else None


async def async_setup(hass: HomeAssistant) -> None:
    gestures = Gestures()
    hass.data[GESTURES_KEY] = gestures
    websocket_api.async_register_command(hass, ws_subscribe)

    # Fire TV address -> the app in front ("" while unknown).
    front: dict[str, str] = {}

    async def read_front(host: str, wait: float = 0.0) -> None:
        from .tvremote import fire_tv_now

        if wait:
            await asyncio.sleep(wait)
        now = await fire_tv_now(hass, host)
        front[host] = str(now.get("app") or "") if now.get("reachable") and now.get("awake") else ""

    async def fire_key(host: str, key: str) -> None:
        from .tvremote import fire_tv_key

        await fire_tv_key(hass, host, key)

    @callback
    def in_other_app(host: str) -> bool:
        from .tvsetup import PACKAGE

        app = front.get(host, "")
        return bool(app) and app != PACKAGE

    @callback
    def on_gesture(event: Event) -> None:
        gesture = str(event.data.get("gesture", ""))
        if gesture not in GESTURES:
            return
        where = str(event.data.get("where", ""))[:80]
        message: dict[str, Any] = {"gesture": gesture}
        if gesture == "aim":
            # Air tap: where the hand is over the screen (0-1 across and down). Only the dashboard follows it.
            try:
                message["x"] = max(0.0, min(1.0, float(event.data.get("x", 0.5))))
                message["y"] = max(0.0, min(1.0, float(event.data.get("y", 0.5))))
                # Pointer style: the live hand's 21 points (x, y in hand sizes around its middle) for the screen to draw.
                hand = event.data.get("hand")
                if isinstance(hand, list) and len(hand) == 42:
                    message["hand"] = [max(-2.0, min(2.0, float(v))) for v in hand]
            except (TypeError, ValueError):
                return
            gestures.send(where, message)
            return
        if gesture == "ready":
            try:
                message["seconds"] = max(1.0, min(30.0, float(event.data.get("seconds", 5))))
            except (TypeError, ValueError):
                message["seconds"] = 5.0
            if event.data.get("style") in ("steps", "glow", "pointer"):
                message["style"] = event.data["style"]
        host = event.data.get("firetv")
        host = host if isinstance(host, str) and host else None
        if host:
            if gesture == "ready":
                hass.async_create_task(read_front(host))
            elif gesture in FIRE_KEYS and in_other_app(host):
                # Another app is in front of the TV: the gesture is the remote's key there.
                hass.async_create_task(fire_key(host, FIRE_KEYS[gesture]))
                if gesture in ("ok", "back"):
                    hass.async_create_task(read_front(host, 1.0))
                return
            elif gesture in VOLUME and not event.data.get("entity"):
                gestures.send(where, message)
                hass.async_create_task(fire_key(host, FIRE_KEYS[gesture]))
                return
        gestures.send(where, message)
        if gesture in VOLUME:
            entity = event.data.get("entity")
            entity = entity if isinstance(entity, str) and entity.startswith("media_player.") else find_tv(hass, where)
            if entity:
                hass.async_create_task(
                    hass.services.async_call("media_player", VOLUME[gesture], {"entity_id": entity}, blocking=False)
                )

    hass.bus.async_listen(EVENT, on_gesture)
    websocket_api.async_register_command(hass, ws_hands)


# Works for non-admin logins: wall screens often use a plain user. Sending a gesture (the event) needs an admin.


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/gesture/subscribe",
        vol.Optional("where", default=""): vol.All(str, vol.Length(max=80)),
    }
)
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    gestures: Gestures = hass.data[GESTURES_KEY]

    @callback
    def forward(data: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], data))

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = gestures.join(msg["where"], forward)


# ---- A wall tablet's own front camera (the wall app runs the hand model; Home Assistant runs our rules) ----
# The tablet sends each picture's hands as points (never the picture). Here they go through the same rules as the
# Mac tracker (gesture_logic.py), and the gestures go out as the same hadash_gesture event, to the screen named
# "target": the kitchen tablet can be the eyes of the kitchen TV.

STYLES = ("steps", "glow", "pointer")


class HandSession:
    """One tablet's camera, for one target screen."""

    def __init__(self, hass: HomeAssistant, target: str, style: str, firetv: str | None) -> None:
        from .gesture_logic import GestureReader, Settings

        self.hass, self.target, self.style, self.firetv = hass, target, style, firetv
        # A tablet on the wall sees the person a little farther and lower than the Mac's own camera.
        self.reader = GestureReader(Settings(airtap=style != "steps", pointer=style == "pointer", min_size=0.06, max_y=1.0))
        self.aim_sent: tuple[float, float] | None = None

    def fire(self, gesture: str, extra: dict[str, Any] | None = None) -> None:
        data: dict[str, Any] = {"where": self.target, "gesture": gesture, **(extra or {})}
        if self.firetv:
            data["firetv"] = self.firetv
        if gesture == "ready":
            data["seconds"] = self.reader.settings.listen
            data["style"] = self.style
        self.hass.bus.async_fire(EVENT, data)

    def look(self, now: float, hands: list) -> None:
        r = self.reader
        for g in r.look(now, hands):
            if g == "ok" and r.tap_at:
                # Air tap: first put the glow back where the hand was before it closed (not for the drawn hand, which
                # already stands still there), then tap.
                if r.shape is None:
                    self.fire("aim", {"x": round(r.tap_at[0], 3), "y": round(r.tap_at[1], 3)})
                r.tap_at = None
            self.fire(g)
        if r.aim and (self.aim_sent is None or max(abs(r.aim[0] - self.aim_sent[0]), abs(r.aim[1] - self.aim_sent[1])) > 0.0015):
            self.aim_sent = r.aim
            extra: dict[str, Any] = {"x": round(r.aim[0], 3), "y": round(r.aim[1], 3)}
            if r.shape:
                extra["hand"] = [round(v, 2) for pt in r.shape for v in pt]
            self.fire("aim", extra)
        elif not r.aim:
            self.aim_sent = None


_POINTS = vol.All([vol.Coerce(float)], vol.Length(min=42, max=42))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/gesture/hands",
        vol.Required("target"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Optional("style", default="glow"): vol.In(STYLES),
        vol.Optional("firetv"): vol.All(str, vol.Length(max=80)),
        vol.Optional("t"): vol.Coerce(float),  # the picture's time on the tablet, in ms (steadier than arrival)
        vol.Required("w"): vol.All(vol.Coerce(int), vol.Range(min=16, max=4096)),
        vol.Required("h"): vol.All(vol.Coerce(int), vol.Range(min=16, max=4096)),
        vol.Required("hands"): vol.All(
            [{vol.Required("shape"): vol.All(str, vol.Length(max=40)), vol.Required("score"): vol.Coerce(float),
              vol.Required("pts"): _POINTS}],
            vol.Length(max=2),
        ),
    }
)
@callback
def ws_hands(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """One picture's hands from a wall tablet's camera. Answers whether the rules are listening, so the tablet can
    look often while someone talks with a hand, and rest its camera the rest of the time."""
    import time

    from .gesture_logic import hand_of

    sessions: dict[tuple[int, str], HandSession] = hass.data.setdefault("hadash_hand_sessions", {})
    key = (id(connection), msg["target"].casefold())
    session = sessions.get(key)
    if session is None or session.style != msg["style"] or session.firetv != msg.get("firetv"):
        session = sessions[key] = HandSession(hass, msg["target"], msg["style"], msg.get("firetv") or None)

        @callback
        def forget() -> None:
            sessions.pop(key, None)

        # The tablet's connection closes: its session goes too.
        connection.subscriptions[f"hadash_hands_{key[1]}"] = forget
    w, h = msg["w"], msg["h"]
    hands = []
    for item in msg["hands"]:
        pts = item["pts"]
        points = [(min(1.5, max(-0.5, pts[i])), min(1.5, max(-0.5, pts[i + 1]))) for i in range(0, 42, 2)]
        hands.append(hand_of(points, item["shape"], item["score"], w, h))
    session.look(msg["t"] / 1000 if "t" in msg else time.monotonic(), hands)
    connection.send_result(msg["id"], {"listening": session.reader.listening})
