"""Doorstep watch (Pro in the studio): our own package monitor, for cameras that do not report packages.

The studio marks an area on a camera tile (the doorstep, as a shape with corners). Home Assistant looks at that
area only after a person leaves, never in between, and on a small grey copy of the camera's picture:

1. While nothing happens it keeps a fresh "before" picture (every 10 minutes, only when nobody is there).
2. When the camera's person sensor (or, without one, its motion sensor) goes quiet, it takes a picture
   after 45 seconds and another one 2.5 minutes later.
3. A package is there when the area changed against "before" in both pictures, and the two agree with each
   other. So a squirrel, a falling leaf or a passing car never counts: nothing happens without a visit, and
   anything that moves or goes away within those minutes is not the same in both pictures.
4. A change of light (a cloud, the porch light, dusk) moves the whole picture; the part outside the area tells
   how much, and that much is taken off. When the whole scene changed a lot (the camera turned), there is no answer.
5. The package is gone when, after a later visit, the area looks like "before" again.

The area is the tile's "doorstep" setting. The answer is binary_sensor.<camera>_package_watch ("Package present"), which the tile's Package monitor reads
like any camera's own package sensor. The tablets do no picture work.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
import logging
import re
from typing import Any, Callable

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The small copy: this many pixels wide. Enough for a parcel on a doorstep, cheap for a Celeron.
SIDE = 96
# A pixel counts as changed when it moved this many grey levels (of 255), after the light change is taken off.
PIXEL = 32
# Share of the area that must change for a package, and the most two pictures of one package may differ.
ARRIVE = 0.12
STABLE = 0.05
# When this much of the picture outside the area changed too, the camera moved or the scene is new: no answer.
SCENE = 0.35
FIRST_LOOK = 45
SECOND_LOOK = 150
BEFORE_EVERY = timedelta(minutes=10)

PERSON = re.compile(r"person|people|human|pedestrian")
NOT_MOTION = re.compile(r"doorbell|ringing|_ring$|_ding$|visitor|button|tamper|sound|cry|audio|package|parcel")


# ---- Pictures: pure functions, so they can be tested without Home Assistant ----

@dataclass
class Small:
    """A small grey copy of a picture: its width, height and pixels, and the shape of the original."""

    w: int
    h: int
    px: bytes
    aspect: float


def small(jpeg: bytes) -> Small:
    from PIL import Image  # noqa: PLC0415 (Pillow comes with Home Assistant; loaded only when a camera is watched)

    with Image.open(BytesIO(jpeg)) as img:
        aspect = img.width / max(img.height, 1)
        h = max(8, round(SIDE / aspect))
        grey = img.convert("L").resize((SIDE, h))
        return Small(SIDE, h, grey.tobytes(), aspect)


def area_mask(points: list[list[float]], w: int, h: int) -> bytes:
    """The area (corners from 0 to 1, as the studio stores them) as one byte per pixel: 1 inside, 0 outside."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon([(x * (w - 1), y * (h - 1)) for x, y in points], fill=1)
    return mask.tobytes()


def changed(a: Small, b: Small, mask: bytes) -> tuple[float, float]:
    """How much of the area, and of the rest of the picture, changed from a to b (shares from 0 to 1).

    The rest of the picture says how much the light changed; that much is taken off every pixel first.
    """
    inside = [i for i, m in enumerate(mask) if m]
    outside = [i for i, m in enumerate(mask) if not m]
    ref = outside or inside
    shift = sum(b.px[i] - a.px[i] for i in ref) / max(len(ref), 1)

    def share(ids: list[int]) -> float:
        if not ids:
            return 0.0
        return sum(1 for i in ids if abs(b.px[i] - a.px[i] - shift) > PIXEL) / len(ids)

    return share(inside), share(outside)


def same_shape(*pics: Small) -> bool:
    """A camera that sends a small event thumbnail with another crop between full pictures cannot be compared."""
    return all(p.w == pics[0].w and p.h == pics[0].h and abs(p.aspect - pics[0].aspect) < 0.03 for p in pics)


def judge(before: Small, first: Small, second: Small, mask: bytes, present: bool, package: Small | None) -> str | None:
    """What the two pictures after a visit say: "arrived", "gone", "moved" (still there, looks different), or None."""
    if not same_shape(before, first, second) or (package and not same_shape(package, second)):
        return None
    steady, scene = changed(first, second, mask)
    if steady > STABLE or scene > SCENE:
        return None
    from_before, scene = changed(before, second, mask)
    if scene > SCENE:
        return None
    if not present:
        return "arrived" if from_before >= ARRIVE and changed(before, first, mask)[0] >= ARRIVE else None
    if from_before <= STABLE:
        return "gone"
    if package and changed(package, second, mask)[0] >= ARRIVE:
        return "moved"
    return None


# ---- Which cameras are watched, and their sensors ----

def watched(layout: dict[str, Any] | None) -> dict[str, list[list[float]]]:
    """Camera → area, from every camera tile with an area on any screen of the published layout."""
    out: dict[str, list[list[float]]] = {}
    if not isinstance(layout, dict):
        return out
    layouts = [layout] + [s.get("layout") for s in layout.get("screens") or [] if isinstance(s, dict)]
    for lay in layouts:
        for w in (lay or {}).get("widgets") or []:
            if not isinstance(w, dict) or w.get("type") != "camera" or not str(w.get("entity", "")).startswith("camera."):
                continue
            area = w.get("doorstep")
            if isinstance(area, list) and len(area) >= 3 and all(isinstance(p, list) and len(p) == 2 for p in area):
                out.setdefault(w["entity"], [[min(max(float(x), 0.0), 1.0), min(max(float(y), 0.0), 1.0)] for x, y in area])
    return out


def visit_sensors(hass: HomeAssistant, camera: str) -> list[str]:
    """The camera's person sensors, or its motion sensors when it has none (as the tile finds them)."""
    ents = er.async_get(hass)
    cam = ents.async_get(camera)
    if not cam or not cam.device_id:
        return []
    own = camera.split(".", 1)[1] + "_"
    person: list[str] = []
    motion: list[str] = []
    for e in er.async_entries_for_device(ents, cam.device_id):
        if e.domain != "binary_sensor" or e.disabled_by:
            continue
        full = e.entity_id.split(".", 1)[1]
        key = full[len(own):] if full.startswith(own) else full
        if NOT_MOTION.search(key):
            continue
        name = f"{key} {(e.name or e.original_name or '').lower()}"
        if PERSON.search(name):
            person.append(e.entity_id)
        elif (e.device_class or e.original_device_class) in ("motion", "occupancy", "presence") or "motion" in name:
            motion.append(e.entity_id)
    return person or motion


# ---- The watch ----

@dataclass
class Watch:
    camera: str
    area: list[list[float]]
    sensors: list[str] = field(default_factory=list)
    before: Small | None = None
    before_at: datetime | None = None
    package: Small | None = None
    present: bool = False
    since: datetime | None = None
    task: asyncio.Task | None = None


class DoorstepWatch:
    """Follows the published layout's watched cameras and tells their Package present sensors."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.watches: dict[str, Watch] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: set[Callable[[str], None]] = set()
        self._add: Callable[[list[str]], None] | None = None

    @callback
    def async_start(self) -> Callable[[], None]:
        store = self.hass.data[DOMAIN]
        stop_layout = store.async_listen(lambda _layout: self.async_refresh())
        stop_timer = async_track_time_interval(self.hass, self._renew_before, BEFORE_EVERY)
        self.async_refresh()

        def stop() -> None:
            stop_layout()
            stop_timer()
            self._stop_tracking()
            for w in self.watches.values():
                if w.task:
                    w.task.cancel()

        return stop

    def set_adder(self, add: Callable[[list[str]], None]) -> None:
        """The binary_sensor platform: called with cameras that need a sensor."""
        self._add = add
        add(list(self.watches))

    @callback
    def listen(self, fn: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.add(fn)
        return lambda: self._listeners.discard(fn)

    def _tell(self, camera: str) -> None:
        for fn in list(self._listeners):
            fn(camera)

    def _stop_tracking(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []

    @callback
    def async_refresh(self) -> None:
        """The layout changed: watch the cameras that have an area now, with their newest areas."""
        wanted = watched(self.hass.data[DOMAIN].layout)
        for cam in list(self.watches):
            if cam not in wanted:
                w = self.watches.pop(cam)
                if w.task:
                    w.task.cancel()
                self._tell(cam)
        new = []
        for cam, area in wanted.items():
            w = self.watches.get(cam)
            if w is None:
                w = self.watches[cam] = Watch(cam, area)
                new.append(cam)
            elif w.area != area:
                # A new area: what was seen in the old one says nothing about it.
                w.area, w.before, w.package, w.present = area, None, None, False
                self._tell(cam)
            w.sensors = visit_sensors(self.hass, cam)
        self._stop_tracking()
        sensors = [s for w in self.watches.values() for s in w.sensors]
        if sensors:
            self._unsubs.append(async_track_state_change_event(self.hass, sensors, self._visit))
        if new and self._add:
            self._add(new)
        for cam in new:
            self.hass.async_create_task(self._take_before(self.watches[cam]))

    def restore(self, camera: str, present: bool, since: datetime | None) -> None:
        """After a restart: a package that was there is still there until a visit says otherwise."""
        w = self.watches.get(camera)
        if w and present:
            w.present, w.since = True, since

    async def _picture(self, camera: str) -> Small | None:
        from homeassistant.components.camera import async_get_image  # noqa: PLC0415

        try:
            img = await async_get_image(self.hass, camera, timeout=15)
            return await self.hass.async_add_executor_job(small, img.content)
        except Exception as err:  # noqa: BLE001 (an asleep or offline camera is normal)
            _LOGGER.debug("Doorstep watch: no picture from %s: %s", camera, err)
            return None

    def _busy(self, w: Watch) -> bool:
        return any(self.hass.states.is_state(s, "on") for s in w.sensors) or (w.task is not None and not w.task.done())

    async def _take_before(self, w: Watch) -> None:
        pic = await self._picture(w.camera)
        if not pic or self._busy(w):
            return
        if w.present:
            # A package is there: keep the picture from before it as "before", but learn how it looks now.
            w.package = w.package or pic
            return
        w.before, w.before_at = pic, dt_util.utcnow()

    async def _renew_before(self, _now: datetime) -> None:
        for w in list(self.watches.values()):
            if not self._busy(w):
                await self._take_before(w)

    @callback
    def _visit(self, event: Event[EventStateChangedData]) -> None:
        sensor = event.data["entity_id"]
        for w in self.watches.values():
            if sensor not in w.sensors:
                continue
            if any(self.hass.states.is_state(s, "on") for s in w.sensors):
                # Someone (back) in front of the camera: a look that was waiting starts over when they leave.
                if w.task and not w.task.done():
                    w.task.cancel()
                continue
            old = event.data["old_state"]
            if old and old.state == "on" and (w.task is None or w.task.done()):
                w.task = self.hass.async_create_task(self._after_visit(w))

    async def _after_visit(self, w: Watch) -> None:
        await asyncio.sleep(FIRST_LOOK)
        first = await self._picture(w.camera)
        await asyncio.sleep(SECOND_LOOK - FIRST_LOOK)
        second = await self._picture(w.camera)
        if not first or not second or not w.before:
            if second and not w.present:
                w.before, w.before_at = second, dt_util.utcnow()
            return
        mask = await self.hass.async_add_executor_job(area_mask, w.area, second.w, second.h)
        verdict = await self.hass.async_add_executor_job(judge, w.before, first, second, mask, w.present, w.package)
        _LOGGER.debug("Doorstep watch %s: %s", w.camera, verdict)
        if verdict == "arrived":
            w.present, w.package, w.since = True, second, dt_util.utcnow()
            self._tell(w.camera)
        elif verdict == "gone":
            w.present, w.package, w.since = False, None, None
            w.before, w.before_at = second, dt_util.utcnow()
            self._tell(w.camera)
        elif verdict == "moved":
            w.package = second
        elif not w.present and same_shape(w.before, second) and changed(first, second, mask)[0] <= STABLE:
            # Nothing was left: the quiet scene after the visit is the newest "before".
            w.before, w.before_at = second, dt_util.utcnow()


def camera_name(hass: HomeAssistant, camera: str) -> str:
    state = hass.states.get(camera)
    if state and state.attributes.get("friendly_name"):
        return str(state.attributes["friendly_name"])
    ent = er.async_get(hass).async_get(camera)
    dev = dr.async_get(hass).async_get(ent.device_id) if ent and ent.device_id else None
    return (dev.name_by_user or dev.name) if dev else camera.split(".", 1)[1].replace("_", " ").title()
