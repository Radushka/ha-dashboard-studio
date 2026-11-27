"""Room to room (Pro): what you watch or hear comes with you to the next room.

What can come along:
1. A video in the watch view. It goes on at the same moment, three seconds back, so no word is lost.
2. A camera that fills a screen ("show me the front door").
3. Music on a speaker. The speaker of the new room joins the music (Sonos, and other speakers that Home Assistant
   can group). This one is only offered, never done alone.

How a room is known. A screen's room is, in this order: the room set in the studio for the screen (Living
dashboard or Follow me), the room of a tablet or TV that shows it (its device in Home Assistant), or a room named
in the screen's name ("Kitchen TV"). Someone is in a room when a screen there notices them (a touch, a face, motion
in front of its camera, the wake word), or when a motion, occupancy or presence sensor in that room turns on.

When it moves:
1. Something plays on a screen in room A, and a screen in room B notices someone.
2. Room A has been quiet for a while: no sign of anyone for QUIET_S seconds. Only a room that can tell counts:
   one with a sensor, or one whose screen saw faces or motion in the last hour.
3. One person is home (or nobody is tracked): the screen in B takes it by itself. The new screen whispers "From
   the kitchen"; the old one pauses and shows "Now in the living room" with Bring back.
   More people at home, a room that cannot tell, or a paused video: B only offers it, with one tap ("Continue here").
4. "Bring it here" or "follow me" said to a screen (the closest one answers the wake word) takes the newest thing
   that plays in another room at once. Bring back on the old screen does the same.

A TV takes things by itself only while it shows the dashboard. A video asked for by voice also wakes a Fire TV.
Nothing here needs setup: the studio has one choice (by itself, ask first, off) in the main layout ("roomToRoom").

Over the same websocket as everything else:
- "hadash/follow/report": a screen says what it plays now (or nothing) and whether its page is seen.
- "hadash/follow/seen": a screen noticed someone.
- "hadash/follow/here": bring the newest thing from another room to this screen.
- "hadash/follow/take": a tap on the "Continue here" card.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import secrets
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, PRO, SATELLITES, TVS
from .voice import VOICE, Voice, Wall

_LOGGER = logging.getLogger(__name__)

FOLLOW = "hadash_follow"
# A room counts as left after this long with no sign of anyone.
QUIET_S = 40.0
# A screen that saw faces or motion in front of its camera this recently can tell that its room is empty.
SENSES_FOR_S = 3600.0
# After a move, the same thing stays put this long (two people in two rooms must not toss it back and forth).
SETTLE_S = 30.0
# Someone noticed in room B: how long the old room may take to become quiet (a motion sensor stays on a while).
WAIT_FOR_QUIET_S = 120.0
RECHECK_S = 10.0
# Someone still counts as in room B this long after their last sign.
STILL_HERE_S = 45.0
# How long "Continue here" waits for a tap.
OFFER_S = 60.0
# A video goes on this much earlier than where it left off.
REWIND_S = 3.0
# A paused video, or a report that old, is not offered any more.
STALE_S = 15 * 60.0
SIGN_CLASSES = ("motion", "occupancy", "presence")
# MediaPlayerEntityFeature.GROUPING: the speaker can join another one.
GROUPING = 524288
# On these, the music goes on when the first speaker leaves its group.
LEADER_MAY_LEAVE = ("sonos",)
MODES = ("auto", "ask", "off")

TEXT = {
    "pro": {
        "en": "Room to room is part of Dashboard Studio Pro.",
        "fr": "D'une pièce à l'autre fait partie de Dashboard Studio Pro.",
        "de": "Von Raum zu Raum gehört zu Dashboard Studio Pro.",
        "es": "De habitación en habitación es parte de Dashboard Studio Pro.",
    },
    "nothing": {
        "en": "Nothing is playing in another room.",
        "fr": "Rien ne joue dans une autre pièce.",
        "de": "In keinem anderen Raum läuft gerade etwas.",
        "es": "No hay nada en otra habitación.",
    },
    "here": {
        "en": "Here it is, from {where}.",
        "fr": "Le voici, depuis {where}.",
        "de": "Hier ist es, aus {where}.",
        "es": "Aquí está, desde {where}.",
    },
    "waking": {
        "en": "Waking {where}. It shows in a moment.",
        "fr": "{where} se réveille. Cela s'affiche dans un instant.",
        "de": "{where} wird geweckt. Es erscheint gleich.",
        "es": "Despertando {where}. Aparece en un momento.",
    },
    "gone": {
        "en": "That screen is not on right now.",
        "fr": "Cet écran n'est pas allumé.",
        "de": "Dieser Bildschirm ist gerade nicht an.",
        "es": "Esa pantalla no está encendida.",
    },
    "music": {
        "en": "The music plays here now.",
        "fr": "La musique joue ici maintenant.",
        "de": "Die Musik läuft jetzt hier.",
        "es": "La música suena aquí ahora.",
    },
    "music_too": {
        "en": "The music plays here too.",
        "fr": "La musique joue aussi ici.",
        "de": "Die Musik läuft jetzt auch hier.",
        "es": "La música suena aquí también.",
    },
    "no_speaker": {
        "en": "There is no speaker here that can join the music.",
        "fr": "Aucune enceinte ici ne peut rejoindre la musique.",
        "de": "Hier gibt es keinen Lautsprecher, der die Musik übernehmen kann.",
        "es": "Aquí no hay ningún altavoz que pueda unirse a la música.",
    },
    "too_late": {
        "en": "It has stopped in the meantime.",
        "fr": "Cela s'est arrêté entre-temps.",
        "de": "Es wurde inzwischen beendet.",
        "es": "Se ha detenido mientras tanto.",
    },
}


def text(hass: HomeAssistant, key: str, **args: str) -> str:
    lang = (hass.config.language or "en").split("-")[0].lower()
    return TEXT[key].get(lang, TEXT[key]["en"]).format(**args)


@dataclass
class Report:
    """What one screen's page plays now."""

    screen: str
    connection: int
    item: dict[str, Any] | None
    visible: bool
    at: float = field(default_factory=time.monotonic)

    def position(self, now: float) -> float:
        """Where the video is now: it went on playing since the report."""
        if not self.item or self.item.get("kind") != "video":
            return 0.0
        at = float(self.item.get("at") or 0)
        return at + (now - self.at if self.item.get("playing") else 0)

    def playing(self) -> bool:
        return bool(self.item) and (self.item.get("kind") == "camera" or bool(self.item.get("playing")))


@dataclass
class Candidate:
    """Something that could come to the screen where someone is: a report of another screen, or music."""

    kind: str
    title: str
    from_area: str
    report: Report | None = None
    speaker: str = ""


class Follow:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.reports: dict[tuple[int, str], Report] = {}
        # The last sign of someone at each screen, and the last one from its camera (it can tell an empty room).
        self.seen_at: dict[str, float] = {}
        self.sensed_at: dict[str, float] = {}
        self.moved_at: dict[str, float] = {}
        # "Continue here" cards: id -> (screen, candidate, cancel).
        self.offers: dict[str, tuple[str, Candidate, Any]] = {}
        # Screens where someone was noticed while the old room was not quiet yet: when it started.
        self.waiting: dict[str, float] = {}
        # The "look again" timer of each waiting screen.
        self._rechecks: dict[str, Any] = {}
        self._stop: Any = None

    # ---- Starting ----

    @callback
    def async_start(self) -> None:
        self._stop = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stopping)

    @callback
    def _stopping(self, _event: Event) -> None:
        """Home Assistant stops: no timer is left behind."""
        for cancel in self._rechecks.values():
            cancel()
        self._rechecks.clear()
        self.waiting.clear()
        for oid in list(self.offers):
            self.end_offer(oid)

    @callback
    def _state_changed(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is None or new.domain != "binary_sensor" or new.state != "on":
            return
        old = event.data.get("old_state")
        if old is not None and old.state == "on":
            return
        if new.attributes.get("device_class") not in SIGN_CLASSES or not self._anything_plays():
            return
        area = self.entity_area(new.entity_id)
        if not area:
            return
        for screen in {r.screen for r in self._live_reports()}:
            if self.area_of(screen) == area:
                self.noticed(screen, sensor=True)

    # ---- Settings and plan ----

    @property
    def pro(self) -> bool:
        return self.hass.data[PRO].state()["plan"] != "free"

    def mode(self) -> str:
        layout = self.hass.data[DOMAIN].layout or {}
        mode = layout.get("roomToRoom") if isinstance(layout, dict) else None
        return mode if mode in MODES else "auto"

    # ---- Rooms ----

    def entity_area(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None:
            return None
        if entry.area_id:
            return entry.area_id
        if entry.device_id and (device := dr.async_get(self.hass).async_get(entry.device_id)):
            return device.area_id
        return None

    def _area_id(self, value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        areas = ar.async_get(self.hass)
        if areas.async_get_area(value):
            return value
        found = areas.async_get_area_by_name(value)
        return found.id if found else None

    def area_of(self, screen: str) -> str | None:
        """The room a screen stands in (an area id), or None when nothing says."""
        layout = self.hass.data[DOMAIN].layout or {}
        settings: dict[str, Any] = layout if screen == "" else {}
        name = str(layout.get("title") or "")
        for s in layout.get("screens") or []:
            if isinstance(s, dict) and s.get("id") == screen:
                inner = s.get("layout") if isinstance(s.get("layout"), dict) else {}
                settings = {**s, **inner}
                name = str(s.get("name") or screen)
        for key in ("living", "followMe"):
            part = settings.get(key)
            if isinstance(part, dict) and (area := self._area_id(part.get("area"))):
                return area
        # The tablets that show it: their devices' rooms in Home Assistant.
        voice: Voice = self.hass.data[VOICE]
        connections = {w.connection for w in voice.walls.values() if w.screen == screen}
        satellites = self.hass.data.get(SATELLITES)
        devices = dr.async_get(self.hass)
        for tablet in getattr(satellites, "tablets", {}).values():
            if tablet.connection and tablet.connection in connections:
                device = devices.async_get_device(identifiers={(DOMAIN, tablet.id)})
                if device and device.area_id:
                    return device.area_id
        # A Fire TV set up with "Add a Fire TV": the room of its media player.
        tvs: dict[str, dict[str, Any]] = getattr(self.hass.data.get(TVS), "tvs", {}) or {}
        hosts = {h for h, tv in tvs.items() if tv.get("screen") == screen}
        if hosts:
            for state in self.hass.states.async_all("media_player"):
                if state.attributes.get("firetv_host") in hosts and (area := self.entity_area(state.entity_id)):
                    return area
        from .satellite import room_of

        return room_of(self.hass, name) if screen or name else None

    def area_name(self, area: str | None, fallback: str = "") -> str:
        found = ar.async_get(self.hass).async_get_area(area) if area else None
        return found.name if found else fallback

    def _sensors(self, area: str) -> list[Any]:
        return [
            s
            for s in self.hass.states.async_all("binary_sensor")
            if s.attributes.get("device_class") in SIGN_CLASSES and self.entity_area(s.entity_id) == area
        ]

    def last_sign(self, area: str, now: float) -> tuple[float | None, bool]:
        """When someone was last noticed in the room (monotonic), and whether the room can tell at all."""
        times: list[float] = []
        can_tell = False
        wall_now = time.time()
        for s in self._sensors(area):
            can_tell = True
            if s.state == "on":
                times.append(now)
            elif s.state == "off":
                times.append(now - max(0.0, wall_now - s.last_changed.timestamp()))
        for screen in self._screens_in(area):
            if screen in self.seen_at:
                times.append(self.seen_at[screen])
            if now - self.sensed_at.get(screen, -1e9) < SENSES_FOR_S:
                can_tell = True
        return (max(times) if times else None), can_tell

    def _screens_in(self, area: str) -> set[str]:
        voice: Voice = self.hass.data[VOICE]
        screens = {w.screen for w in voice.walls.values()} | {r.screen for r in self.reports.values()}
        return {s for s in screens if self.area_of(s) == area}

    def quiet(self, area: str, now: float) -> bool | None:
        """True: nobody there for a while. False: someone was there just now. None: the room cannot tell."""
        last, can_tell = self.last_sign(area, now)
        if not can_tell:
            return None
        return last is None or now - last >= QUIET_S

    def people_home(self) -> int:
        from .presence import is_home

        people = self.hass.states.async_all("person")
        if not people:
            return 1
        return sum(1 for p in people if is_home(self.hass, p))

    # ---- What plays ----

    def _walls(self, screen: str, connection: int | None = None) -> list[Wall]:
        voice: Voice = self.hass.data[VOICE]
        return [w for w in voice.walls.values() if w.screen == screen and (connection is None or w.connection == connection)]

    def _live_reports(self) -> list[Report]:
        """The reports of pages that are still connected; the others are forgotten."""
        voice: Voice = self.hass.data[VOICE]
        alive = {(w.connection, w.screen) for w in voice.walls.values()}
        for key in [k for k in self.reports if k not in alive]:
            del self.reports[key]
        return list(self.reports.values())

    def _anything_plays(self) -> bool:
        return any(r.item for r in self.reports.values()) or bool(self._music_players())

    def _visible(self, screen: str) -> bool:
        """A TV takes things by itself only while it shows the dashboard. A tablet always may (it wakes)."""
        reports = [r for r in self._live_reports() if r.screen == screen]
        walls = self._walls(screen)
        if not walls:
            return False
        if any(w.tv for w in walls):
            return any(r.visible for r in reports)
        return True

    def _busy(self, screen: str) -> bool:
        return any(r.item for r in self._live_reports() if r.screen == screen)

    def _music_players(self) -> list[Any]:
        return [
            s
            for s in self.hass.states.async_all("media_player")
            if s.state == "playing" and s.attributes.get("device_class") != "tv" and int(s.attributes.get("supported_features") or 0) & GROUPING
        ]

    def _platform(self, entity_id: str) -> str:
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry.platform if entry else ""

    def speaker_for(self, area: str, playing: Any) -> str | None:
        """A speaker in this room that can join the music that plays elsewhere."""
        platform = self._platform(playing.entity_id)
        members = set(playing.attributes.get("group_members") or [playing.entity_id])
        for s in self.hass.states.async_all("media_player"):
            if s.entity_id in members or s.attributes.get("device_class") == "tv" or s.state == "unavailable":
                continue
            if not int(s.attributes.get("supported_features") or 0) & GROUPING or self._platform(s.entity_id) != platform:
                continue
            if self.entity_area(s.entity_id) == area:
                return s.entity_id
        return None

    def candidates(self, screen: str, now: float) -> list[Candidate]:
        """What plays in other rooms and could come here, the newest first. Music comes after the screens."""
        here = self.area_of(screen)
        found: list[Candidate] = []
        for r in sorted(self._live_reports(), key=lambda r: r.at, reverse=True):
            if r.screen == screen or not r.item or now - r.at > STALE_S:
                continue
            area = self.area_of(r.screen)
            if here and area == here:
                continue
            title = str(r.item.get("title") or "")
            found.append(Candidate(str(r.item.get("kind")), title, area or "", report=r))
        if here:
            for s in self._music_players():
                area = self.entity_area(s.entity_id)
                if area and area != here and self.speaker_for(here, s):
                    title = str(s.attributes.get("media_title") or s.attributes.get("friendly_name") or "")
                    found.append(Candidate("music", title, area, speaker=s.entity_id))
        return found

    # ---- Someone noticed ----

    @callback
    def noticed(self, screen: str, sensor: bool = False) -> None:
        now = time.monotonic()
        if not sensor:
            self.seen_at[screen] = now
        if not self.pro or self.mode() == "off" or self._busy(screen) or not self._visible(screen):
            return
        here = self.area_of(screen)
        if not here:
            return
        for c in self.candidates(screen, now):
            if c.report and now - self.moved_at.get(self._key(c), -1e9) < SETTLE_S:
                continue
            quiet = self.quiet(c.from_area, now) if c.from_area else None
            if quiet is False:
                # The old room still shows someone (a motion sensor stays on a while): look again in a moment.
                self._wait(screen, now)
                return
            sure = quiet is True and c.kind != "music" and c.report is not None and c.report.playing()
            if sure and self.mode() == "auto" and self.people_home() <= 1:
                self.hass.async_create_task(self.move(c, screen, "auto"))
            else:
                self.offer(screen, c)
            self.waiting.pop(screen, None)
            return

    def _wait(self, screen: str, now: float) -> None:
        first = self.waiting.setdefault(screen, now)
        if (old := self._rechecks.pop(screen, None)) is not None:
            old()
        if now - first > WAIT_FOR_QUIET_S:
            self.waiting.pop(screen, None)
            return

        @callback
        def again(_now: Any) -> None:
            self._rechecks.pop(screen, None)
            if screen not in self.waiting:
                return
            area = self.area_of(screen)
            last, _ = self.last_sign(area, time.monotonic()) if area else (None, False)
            if last is None or time.monotonic() - last > STILL_HERE_S:
                self.waiting.pop(screen, None)
                return
            self.noticed(screen, sensor=True)

        self._rechecks[screen] = async_call_later(self.hass, RECHECK_S, again)

    @staticmethod
    def _key(c: Candidate) -> str:
        if c.report and c.report.item:
            return f"{c.report.item.get('kind')}:{c.report.item.get('id') or c.report.item.get('entity')}"
        return f"music:{c.speaker}"

    # ---- "Continue here" ----

    @callback
    def offer(self, screen: str, c: Candidate) -> None:
        if any(s == screen and self._key(o) == self._key(c) for s, o, _ in self.offers.values()):
            return
        for oid in [oid for oid, (s, _o, _c) in self.offers.items() if s == screen]:
            self.end_offer(oid)
        oid = secrets.token_hex(6)
        item = c.report.item if c.report and c.report.item else {}

        @callback
        def expire(_now: Any) -> None:
            self.end_offer(oid)

        self.offers[oid] = (screen, c, async_call_later(self.hass, OFFER_S, expire))
        message = {
            "action": "follow_offer",
            "offer": oid,
            "kind": c.kind,
            "title": c.title[:200],
            "from": self.area_name(c.from_area, c.report.screen if c.report else ""),
            "seconds": OFFER_S,
        }
        if c.kind == "video" and item.get("id"):
            message["id"] = item["id"]
        for wall in self._walls(screen):
            wall.send(message)

    @callback
    def end_offer(self, oid: str) -> tuple[str, Candidate] | None:
        found = self.offers.pop(oid, None)
        if found is None:
            return None
        screen, c, cancel = found
        cancel()
        for wall in self._walls(screen):
            wall.send({"action": "follow_offer_end", "offer": oid})
        return screen, c

    async def take(self, oid: str) -> dict[str, Any]:
        found = self.end_offer(oid)
        if found is None:
            return {"ok": False, "say": text(self.hass, "too_late")}
        screen, c = found
        return await self.move(c, screen, "tap")

    async def here(self, screen: str, kind: str = "") -> dict[str, Any]:
        """Bring it here (voice, or Bring back): the newest thing from another room, at once."""
        if not self.pro:
            return {"ok": False, "say": text(self.hass, "pro")}
        found = [c for c in self.candidates(screen, time.monotonic()) if not kind or c.kind == kind]
        if not found:
            if kind == "music":
                return {"ok": False, "say": text(self.hass, "no_speaker")}
            return {"ok": False, "say": text(self.hass, "nothing")}
        for oid in [oid for oid, (s, _o, _c) in self.offers.items() if s == screen]:
            self.end_offer(oid)
        return await self.move(found[0], screen, "voice")

    # ---- Moving ----

    async def move(self, c: Candidate, screen: str, how: str) -> dict[str, Any]:
        if c.kind == "music":
            return await self._move_music(c, screen)
        r = c.report
        assert r is not None and r.item is not None
        now = time.monotonic()
        self.moved_at[self._key(c)] = now
        from_name = self.area_name(c.from_area, r.screen)
        here_area = self.area_of(screen)
        to_name = self.area_name(here_area, screen)
        item = r.item
        if item.get("kind") == "video":
            command: dict[str, Any] = {
                "action": "video",
                "id": item.get("id"),
                "title": item.get("title") or "",
                "author": item.get("author") or "",
                "start": int(max(0.0, r.position(now) - REWIND_S)),
                "came": from_name,
            }
        else:
            command = {"action": "follow_camera", "entity": item.get("entity"), "came": from_name}
        # The old screen lets go first: a Eufy HomeBase carries one video at a time.
        for wall in self._walls(r.screen, r.connection):
            wall.send({"action": "follow_gone", "to": to_name, "kind": item.get("kind")})
        self.reports.pop((r.connection, r.screen), None)
        if item.get("kind") == "camera":
            await asyncio.sleep(0.6)
        from .send import SEND, Sender

        sender: Sender = self.hass.data[SEND]
        walls = self._walls(screen)
        hosts = [h for h, tv in (getattr(self.hass.data.get(TVS), "tvs", {}) or {}).items() if tv.get("screen") == screen]
        if how == "voice" and command["action"] == "video":
            for host in hosts:
                self.hass.async_create_background_task(sender._wake_tv(host), f"hadash wake {host}")
        if walls:
            await sender._deliver_command(self.hass.data[VOICE], walls, command)
            return {"ok": True, "say": text(self.hass, "here", where=from_name)}
        if hosts and how == "voice":
            sender.pending[screen] = (command, time.monotonic())
            return {"ok": True, "say": text(self.hass, "waking", where=to_name)}
        return {"ok": False, "say": text(self.hass, "gone")}

    async def _move_music(self, c: Candidate, screen: str) -> dict[str, Any]:
        playing = self.hass.states.get(c.speaker)
        area = self.area_of(screen)
        if playing is None or playing.state != "playing" or not area:
            return {"ok": False, "say": text(self.hass, "too_late")}
        target = self.speaker_for(area, playing)
        if not target:
            return {"ok": False, "say": text(self.hass, "no_speaker")}
        members = list(playing.attributes.get("group_members") or [playing.entity_id])
        leader = members[0] if members else playing.entity_id
        self.moved_at[self._key(c)] = time.monotonic()
        await self.hass.services.async_call("media_player", "join", {"entity_id": leader, "group_members": [target]}, blocking=True)
        # The old room lets go when nobody is there any more, and the music can go on without it.
        left = self.quiet(c.from_area, time.monotonic()) is True
        if left and (c.speaker != leader or self._platform(c.speaker) in LEADER_MAY_LEAVE):
            await asyncio.sleep(1.5)
            await self.hass.services.async_call("media_player", "unjoin", {"entity_id": c.speaker}, blocking=True)
            return {"ok": True, "say": text(self.hass, "music")}
        return {"ok": True, "say": text(self.hass, "music_too")}


def _follow(hass: HomeAssistant) -> Follow:
    return hass.data[FOLLOW]


@callback
def async_setup(hass: HomeAssistant) -> None:
    hass.data[FOLLOW] = Follow(hass)
    hass.data[FOLLOW].async_start()
    for command in (ws_report, ws_seen, ws_here, ws_take):
        websocket_api.async_register_command(hass, command)


ITEM = vol.Any(
    None,
    vol.Schema(
        {
            vol.Required("kind"): vol.In(("video", "camera")),
            vol.Optional("id"): vol.All(str, vol.Match(r"^[\w-]{6,20}$")),
            vol.Optional("entity"): vol.All(str, vol.Match(r"^camera\.[\w]+$")),
            vol.Optional("title", default=""): vol.All(str, vol.Length(max=200)),
            vol.Optional("author", default=""): vol.All(str, vol.Length(max=80)),
            vol.Optional("at", default=0): vol.All(vol.Coerce(float), vol.Range(min=0, max=86400)),
            vol.Optional("playing", default=False): bool,
        }
    ),
)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/follow/report",
        vol.Optional("screen", default=""): str,
        vol.Optional("item"): ITEM,
        vol.Optional("visible", default=True): bool,
    }
)
@callback
def ws_report(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    item = msg.get("item")
    if item and not (item.get("id") if item["kind"] == "video" else item.get("entity")):
        item = None
    follow = _follow(hass)
    follow.reports[(id(connection), msg["screen"])] = Report(msg["screen"], id(connection), item, msg["visible"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/follow/seen",
        vol.Optional("screen", default=""): str,
        vol.Optional("how", default="touch"): vol.All(str, vol.Length(max=12)),
    }
)
@callback
def ws_seen(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    follow = _follow(hass)
    if msg["how"] in ("camera", "face"):
        follow.sensed_at[msg["screen"]] = time.monotonic()
    follow.noticed(msg["screen"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/follow/here",
        vol.Optional("screen", default=""): str,
        vol.Optional("kind", default=""): vol.In(("", "video", "camera", "music")),
    }
)
@websocket_api.async_response
async def ws_here(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], await _follow(hass).here(msg["screen"], msg["kind"]))


@websocket_api.websocket_command({vol.Required("type"): "hadash/follow/take", vol.Required("offer"): str})
@websocket_api.async_response
async def ws_take(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], await _follow(hass).take(msg["offer"]))
