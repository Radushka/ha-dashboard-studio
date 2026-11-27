"""Eufy live: one owner in Home Assistant for every Eufy camera's live video, shared by every screen.

What we learned at the front door (26 Nov 2026):
1. Each screen woke the camera by itself. Two screens at once gave "already running" errors, and a screen that
   closed could stop the video another screen still watched.
2. The Eufy video lands in the go2rtc add-on under the camera's serial number, but the screens asked for it by
   the entity name. That only works once Home Assistant already calls the camera "streaming", so a screen that asked
   a moment too early got "does not support streaming" or an old still picture.
3. Cameras behind one HomeBase share one link, and that link carries one video at a time. A start for the doorbell
   ended the front door video in the same second. The screens' "fresh picture" wakes did exactly that, every few
   minutes, so a live front door video kept dropping.
4. A stop sent while a start is still on its way is lost, and the video then runs on with nobody watching.

What the owner does instead:
1. A screen says "I am watching" (hadash/eufy/live). The first one wakes the camera; the others join that video.
   Every screen plays it straight from go2rtc by its stream name (the serial number).
2. One video per HomeBase. A screen someone watches always wins over a fresh picture: a picture wake waits for its
   turn ("busy") and is skipped when the link stays taken. When two people watch two cameras of one HomeBase, the
   newest tap wins, and the other screen is told "busy" until the link is free.
3. While someone watches, a video that ends by itself is started again quietly, with growing pauses. Screens hear
   "waking", "live", "retrying", "busy", "resting" or "sleeping", so they can show calm words instead of a grey tile.
4. When the last screen leaves, the camera sleeps after a short wait, so a tile that opens full screen does not
   wake it twice. Also a video the owner did not start is stopped then, so it never blocks its HomeBase.
5. A sweeper stops leftover videos: a Eufy camera that streams with nobody watching it in go2rtc for a minute.
6. Wired cameras wake early: a ring or a person at the door starts the video before anyone taps, so it is ready
   when someone looks. Battery cameras are never woken early.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from collections.abc import Callable
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .eufy import EUFY, _camera, _not_running

_LOGGER = logging.getLogger(__name__)

KEY = "hadash_eufy_live"
ALREADY = "already_running"
# The last screen left: the camera sleeps after this wait, unless a screen comes back.
GRACE_S = 8.0
# A wake that gives no video in this time counts as failed.
WAKE_S = 20.0
# Pauses between tries after a failed wake; after the last one the owner rests and tries once a minute.
RETRY_S = (2.0, 4.0, 8.0, 15.0)
REST_S = 60.0
# A picture wake waits this long for a busy HomeBase, then gives up (the picture is simply not refreshed).
PICTURE_WAIT_S = 20.0
# An early wake (ring or person) holds the video this long when no screen joins.
EARLY_S = 25.0
# An early wake happens at most once in this time for each camera.
EARLY_GAP_S = 60.0
# Sensors of the camera's own device that start an early wake.
EARLY_WORDS = ("ring", "person", "doorbell", "motion_human")
# The sweeper: how often it looks, how long a video may stream unwatched, and the least time between two stops.
SWEEP_S = 30.0
UNWATCHED_S = 60.0
SWEEP_GAP_S = 120.0
# How long the HomeBase of each camera is remembered before it is asked again.
LINKS_S = 6 * 3600.0
GO2RTC = "http://localhost:1984/"


def _device(hass: HomeAssistant, entity_id: str) -> dr.DeviceEntry | None:
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    return dr.async_get(hass).async_get(entry.device_id)


def serial_of(hass: HomeAssistant, entity_id: str) -> str | None:
    """The camera's serial number. The eufy_security integration names the go2rtc stream after it.
    The device registry has no serial number for Eufy cameras: the serial is the device identifier."""
    device = _device(hass, entity_id)
    if device is None:
        return None
    for domain, ident in device.identifiers:
        if domain == EUFY and ident:
            return str(ident)
    return device.serial_number or None


def _device_entities(hass: HomeAssistant, entity_id: str) -> list[str]:
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.device_id is None:
        return []
    return [e.entity_id for e in er.async_entries_for_device(er.async_get(hass), entry.device_id)]


def has_battery(hass: HomeAssistant, entity_id: str) -> bool:
    return any(e.startswith("sensor.") and "battery" in e for e in _device_entities(hass, entity_id))


def way_of(hass: HomeAssistant, entity_id: str) -> str:
    """RTSP when the camera's own RTSP switch is on, else P2P (Eufy's own link, as the Eufy app uses)."""
    for e in _device_entities(hass, entity_id):
        if e.startswith("switch.") and "rtsp" in e:
            state = hass.states.get(e)
            if state is not None and state.state == "on":
                return "rtsp"
    return "p2p"


def eufy_cameras(hass: HomeAssistant) -> list[str]:
    return [e.entity_id for e in er.async_get(hass).entities.values() if e.platform == EUFY and e.domain == "camera"]


class Links:
    """Which cameras share one HomeBase. Asked from the Eufy add-on itself (read only): each device's
    stationSerialNumber. A camera with its own link (a wall light, a standalone camera) is its own station.
    When the add-on cannot be asked, every camera that is not a station itself counts as behind one HomeBase:
    that is careful, never wrong in the bad way (two videos fighting)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.station: dict[str, str] = {}
        self.at = 0.0
        self.lock = asyncio.Lock()

    def _addon(self) -> str | None:
        for entry in self.hass.config_entries.async_entries(EUFY):
            host, port = entry.data.get("host"), entry.data.get("port", 3000)
            if host:
                return f"ws://{host}:{port}"
        return None

    async def refresh(self) -> None:
        async with self.lock:
            if time.monotonic() - self.at < LINKS_S and self.station:
                return
            url = self._addon()
            if url is None:
                return
            try:
                self.station = await asyncio.wait_for(self._ask(url), 15)
            except Exception as err:  # noqa: BLE001 - without the answer the careful grouping is used
                _LOGGER.debug("Eufy add-on: could not read the HomeBase of each camera: %s", err)
                self.at = time.monotonic() - LINKS_S + 300  # ask again in 5 minutes
                return
            self.at = time.monotonic()
            _LOGGER.debug("Eufy add-on: HomeBase of each camera: %s", self.station)

    async def _ask(self, url: str) -> dict[str, str]:
        session = async_get_clientsession(self.hass)
        found: dict[str, str] = {}
        async with session.ws_connect(url, heartbeat=10) as ws:
            n = 0

            async def ask(command: dict[str, Any]) -> dict[str, Any]:
                nonlocal n
                n += 1
                mid = f"hadash-{n}"
                await ws.send_str(json.dumps({"messageId": mid, **command}))
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = json.loads(msg.data)
                    if data.get("type") == "result" and data.get("messageId") == mid:
                        return data
                raise ConnectionError("the add-on closed the link")

            await ask({"command": "set_api_schema", "schemaVersion": 21})
            state = (await ask({"command": "start_listening"})).get("result", {}).get("state", {})
            stations = set(state.get("stations") or [])
            for serial in state.get("devices") or []:
                if serial in stations:
                    found[serial] = serial
                    continue
                answer = await ask({"command": "device.get_properties", "serialNumber": serial})
                props = answer.get("result", {}).get("properties", {}) if answer.get("success") else {}
                found[serial] = props.get("stationSerialNumber") or "homebase"
        return found

    def of(self, hass: HomeAssistant, entity_id: str) -> str:
        serial = serial_of(hass, entity_id) or entity_id
        if serial in self.station:
            return self.station[serial]
        # Not known (yet): the careful guess. Models T84A1 (wall light) and T8400 (indoor) have their own link.
        return serial if serial[:5] in ("T84A1", "T8400") else "homebase"


class Viewer:
    def __init__(self, send: Callable[[dict[str, Any]], None], picture: bool) -> None:
        self.send = send
        self.picture = picture
        self.at = time.monotonic()


class Live:
    """The live video of one camera, and the screens that watch it."""

    def __init__(self, owner: Lives, entity_id: str) -> None:
        self.owner = owner
        self.hass = owner.hass
        self.entity_id = entity_id
        self.viewers: dict[int, Viewer] = {}
        self.step = "sleeping"
        self.why = ""
        self.task: asyncio.Task | None = None
        self.held_until = 0.0
        self.next_viewer = 0
        self.early_at = 0.0
        self.woke_at = 0.0

    # The screens

    def join(self, send: Callable[[dict[str, Any]], None], picture: bool = False) -> Callable[[], None]:
        self.next_viewer += 1
        viewer = self.next_viewer
        self.viewers[viewer] = Viewer(send, picture)
        send(self.message())
        self._ensure_running()

        @callback
        def leave() -> None:
            self.viewers.pop(viewer, None)

        return leave

    def hold(self, seconds: float) -> None:
        """Keep the video for a while without a screen (an early wake)."""
        self.held_until = max(self.held_until, time.monotonic() + seconds)
        self._ensure_running()

    def wanted(self) -> bool:
        return bool(self.viewers) or time.monotonic() < self.held_until

    def watched(self) -> bool:
        """Someone looks at the video (not only a fresh picture)."""
        return any(not v.picture for v in self.viewers.values()) or time.monotonic() < self.held_until

    def rank(self) -> tuple[int, float]:
        """Who gets the HomeBase: someone watching beats a picture wake; then the newest tap wins."""
        watching = [v.at for v in self.viewers.values() if not v.picture]
        if watching:
            return (2, max(watching))
        if time.monotonic() < self.held_until:
            return (1, self.held_until)
        return (0, max((v.at for v in self.viewers.values()), default=0.0))

    def message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"step": self.step, "way": way_of(self.hass, self.entity_id)}
        src = serial_of(self.hass, self.entity_id)
        if src:
            msg["src"] = src
        if self.why:
            msg["why"] = self.why
        return msg

    def _say(self, step: str, why: str = "") -> None:
        if step == self.step and why == self.why:
            return
        self.step, self.why = step, why
        msg = self.message()
        for viewer in list(self.viewers.values()):
            viewer.send(msg)

    # The camera

    def streaming(self) -> bool:
        camera = _camera(self.hass, self.entity_id)
        live = getattr(camera, "is_streaming", None)
        if isinstance(live, bool):
            return live
        state = self.hass.states.get(self.entity_id)
        return state is not None and state.state == "streaming"

    async def _call(self, service: str) -> None:
        await self.hass.services.async_call(EUFY, service, target={"entity_id": self.entity_id}, blocking=True)

    async def _wake(self, way: str) -> bool:
        """Start the video. True once the camera streams. Another start a moment earlier counts as success."""
        self.woke_at = time.monotonic()
        try:
            await asyncio.wait_for(self._call(f"start_{way}_livestream"), WAKE_S)
        except asyncio.TimeoutError:
            self.why = "no answer from the camera"
        except Exception as err:  # noqa: BLE001 - the camera's answer decides what happens next
            if ALREADY not in str(err) and getattr(err, "error_code", None) != f"device_livestream_{ALREADY}":
                self.why = str(err)[:120]
                _LOGGER.debug("%s: wake failed: %s", self.entity_id, err)
                return False
        until = time.monotonic() + WAKE_S
        while time.monotonic() < until:
            if self.streaming():
                return True
            if not self.wanted() or self.owner.blocked(self):
                return False
            await asyncio.sleep(0.5)
        self.why = self.why or "the camera did not send video"
        return False

    async def sleep(self, way: str) -> None:
        """Put the camera back to sleep. A start still on its way is waited for first: a stop sent before the
        video arrives is lost, and the video then runs with nobody watching. A video that already ended gets no
        stop, so the log stays quiet."""
        until = self.woke_at + WAKE_S
        while not self.streaming() and time.monotonic() < until and not self.wanted():
            await asyncio.sleep(0.5)
        if self.wanted() or not self.streaming():
            return
        try:
            await self._call(f"stop_{way}_livestream")
        except Exception as err:  # noqa: BLE001
            if not _not_running(err):
                _LOGGER.debug("%s: stop failed: %s", self.entity_id, err)

    def _ensure_running(self) -> None:
        if self.task is None or self.task.done():
            self.task = self.hass.async_create_background_task(self._run(), f"hadash eufy live {self.entity_id}")

    async def _run(self) -> None:
        await self.owner.links.refresh()
        way = way_of(self.hass, self.entity_id)
        tries = 0
        busy_since: float | None = None
        cancelled = False
        try:
            while True:
                if not self.wanted():
                    # The last screen left: wait a moment, a full screen view may open right after the tile.
                    left = time.monotonic()
                    while not self.wanted() and time.monotonic() - left < GRACE_S:
                        await asyncio.sleep(0.5)
                    if not self.wanted():
                        break
                if self.owner.blocked(self):
                    # Another camera of the same HomeBase is watched: wait for the link. A picture gives up.
                    busy_since = busy_since or time.monotonic()
                    self._say("busy")
                    if not self.watched() and time.monotonic() - busy_since > PICTURE_WAIT_S:
                        for key, viewer in list(self.viewers.items()):
                            if viewer.picture:
                                viewer.send({**self.message(), "step": "busy", "gave_up": True})
                                self.viewers.pop(key, None)
                    await asyncio.sleep(1.0)
                    continue
                busy_since = None
                if self.streaming():
                    tries = 0
                    self._say("live")
                    await asyncio.sleep(1.0)
                    continue
                # Not streaming while someone wants it: first wake, or the video ended by itself.
                self._say("waking" if self.step in ("sleeping", "waking", "busy") else "retrying")
                if await self._wake(way):
                    tries = 0
                    self.why = ""
                    self._say("live")
                    continue
                if not self.wanted() or self.owner.blocked(self):
                    continue
                tries += 1
                if tries <= len(RETRY_S):
                    self._say("retrying", self.why)
                    await self._pause(RETRY_S[tries - 1])
                else:
                    # Many tries in a row: the camera or its link is down. Rest, and try again once a minute.
                    self._say("resting", self.why)
                    await self._pause(REST_S)
        except asyncio.CancelledError:
            # Home Assistant stops: nothing more is sent, so the shutdown never waits for a camera.
            cancelled = True
            raise
        finally:
            if not cancelled and not self.owner.blocked(self):
                await self.sleep(way)
            self.why = ""
            self._say("sleeping")
            # A screen came back while the camera was put to sleep: wake it again for that screen.
            if not cancelled and self.wanted() and self.hass.is_running:
                self.task = None
                self._ensure_running()

    async def _pause(self, seconds: float) -> None:
        until = time.monotonic() + seconds
        while time.monotonic() < until and self.wanted() and not self.owner.blocked(self):
            await asyncio.sleep(0.5)


class Lives:
    """Every Eufy camera's Live, one video per HomeBase, the sweeper, and the early wake on a ring or a person."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lives: dict[str, Live] = {}
        self.links = Links(hass)
        self.unwatched_since: dict[str, float] = {}
        self.swept_at: dict[str, float] = {}

    def get(self, entity_id: str) -> Live:
        live = self.lives.get(entity_id)
        if live is None:
            live = self.lives[entity_id] = Live(self, entity_id)
        return live

    def blocked(self, live: Live) -> bool:
        """True when another camera of the same HomeBase is wanted by someone with a higher rank."""
        link = self.links.of(self.hass, live.entity_id)
        mine = live.rank()
        for other in self.lives.values():
            if other is live or not other.wanted() or self.links.of(self.hass, other.entity_id) != link:
                continue
            if other.rank() > mine:
                return True
        return False

    def _camera_for(self, sensor_id: str) -> str | None:
        """The Eufy camera of the same device as this sensor, for a wired camera only."""
        reg = er.async_get(self.hass)
        entry = reg.async_get(sensor_id)
        if entry is None or entry.device_id is None or entry.platform != EUFY:
            return None
        for e in er.async_entries_for_device(reg, entry.device_id):
            if e.domain == "camera" and not has_battery(self.hass, e.entity_id):
                return e.entity_id
        return None

    async def _watched_in_go2rtc(self) -> dict[str, int] | None:
        """Viewers of each go2rtc stream, or None when go2rtc cannot be asked (then nothing is swept)."""
        url = GO2RTC
        for entry in self.hass.config_entries.async_entries("webrtc"):
            url = entry.data.get("url") or url
        try:
            async with async_get_clientsession(self.hass).get(url.rstrip("/") + "/api/streams", timeout=aiohttp.ClientTimeout(total=5)) as res:
                streams = await res.json(content_type=None)
        except Exception:  # noqa: BLE001
            return None
        return {name: len((s or {}).get("consumers") or []) for name, s in (streams or {}).items()}

    async def sweep(self) -> None:
        """Stop leftover videos: a Eufy camera that streams for a minute with nobody watching it anywhere.
        Such a video blocks its HomeBase, so the camera someone opens next cannot start."""
        now = time.monotonic()
        streaming = [e for e in eufy_cameras(self.hass) if (s := self.hass.states.get(e)) is not None and s.state == "streaming"]
        for e in list(self.unwatched_since):
            if e not in streaming:
                self.unwatched_since.pop(e)
        if not streaming:
            return
        viewers = await self._watched_in_go2rtc()
        if viewers is None:
            return
        for entity_id in streaming:
            live = self.get(entity_id)
            serial = serial_of(self.hass, entity_id)
            seen = viewers.get(serial or "", 0) + viewers.get(entity_id, 0)
            if live.wanted() or seen:
                self.unwatched_since.pop(entity_id, None)
                continue
            since = self.unwatched_since.setdefault(entity_id, now)
            if now - since < UNWATCHED_S or now - self.swept_at.get(entity_id, -SWEEP_GAP_S) < SWEEP_GAP_S:
                continue
            self.swept_at[entity_id] = now
            _LOGGER.debug("%s: streamed %d s with nobody watching, so it is stopped", entity_id, now - since)
            await live.sleep(way_of(self.hass, entity_id))

    @callback
    def async_start(self) -> Callable[[], None]:
        @callback
        def changed(event: Event) -> None:
            entity_id: str = event.data.get("entity_id", "")
            if not entity_id.startswith("binary_sensor.") or not any(w in entity_id for w in EARLY_WORDS):
                return
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            if new is None or new.state != "on" or (old is not None and old.state == "on"):
                return
            camera = self._camera_for(entity_id)
            if camera is None:
                return
            live = self.get(camera)
            now = time.monotonic()
            if now - live.early_at < EARLY_GAP_S:
                return
            live.early_at = now
            _LOGGER.debug("%s: early wake, because %s turned on", camera, entity_id)
            live.hold(EARLY_S)

        async def sweep(_now: Any) -> None:
            await self.sweep()

        stops = [
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed),
            async_track_time_interval(self.hass, sweep, timedelta(seconds=SWEEP_S), name="hadash eufy sweeper"),
        ]

        @callback
        def stop_all() -> None:
            for stop in stops:
                stop()

        return stop_all


def lives(hass: HomeAssistant) -> Lives:
    if KEY not in hass.data:
        hass.data[KEY] = Lives(hass)
    return hass.data[KEY]


async def async_setup(hass: HomeAssistant) -> None:
    stop = lives(hass).async_start()
    hass.bus.async_listen_once("homeassistant_stop", lambda _e: stop())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/eufy/live",
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("picture", default=False): bool,
    }
)
@callback
def ws_eufy_live(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Watch a Eufy camera's live video, or wake it for one fresh picture (picture: true, gives way to anyone
    watching). Events: {"step": "waking"|"live"|"retrying"|"busy"|"resting"|"sleeping", "src": <go2rtc stream
    name>, "way": "p2p"|"rtsp", "why"?: text, "gave_up"?: true}. The video sleeps once every screen left."""

    @callback
    def send(event: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = lives(hass).get(msg["entity_id"]).join(send, msg["picture"])
