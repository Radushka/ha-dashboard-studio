"""Home replay (Pro): the pictures of the day's doorbell rings and parcels, for the wall's short film of the day.

Home Assistant keeps every state change for about ten days, so the tablets can tell who came and went, the
doors, the weather and the music from its history. A camera's picture is gone a moment later, so this part
keeps one picture of each ring and each parcel, taken a few seconds after it happened:

1. A ring is an event entity with the doorbell device class, or a binary sensor named like a bell that turns on
   (the same rule as the tablets' doorbell moment, renderer lib/doorbell.svelte.ts). A parcel is a binary sensor
   with "package" or "parcel" in its name that turns on.
2. The camera is the one on the same device, else in the same room, else one named like the bell.
3. The picture is taken 8 seconds later (a battery camera needs a moment to wake and look), again at 20 seconds
   when that fails, made smaller (960 pixels wide) and kept in /config/hadash/replay for two days.
4. A second ring within a minute is the same visit: no new picture.

The tablets ask for the day's pictures with hadash/replay/moments. Nothing here speaks or shows anything.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
from io import BytesIO
import logging
from pathlib import Path
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

REPLAY = "hadash_replay"
KEEP = timedelta(hours=48)
SAME_VISIT = 60
FIRST_LOOK = 8
SECOND_LOOK = 20
WIDTH = 960

# The same words as the tablets' doorbell moment: the name must END with the bell word, and a doorbell's many
# other sensors (motion, person, package...) are someone walking by, not someone ringing.
BELL_ID = re.compile(r"(^|_)(doorbell|ringing|ring|ding|visitor|button_press(ed)?)$")
NOT_BELL = re.compile(
    r"motion|person|people|human|pet|animal|vehicle|package|parcel|detect|tamper|sound|cry|battery|online|connect|status|update|light|chime_|snooze|mute"
)
PARCEL_ID = re.compile(r"package|parcel")
NOT_PARCEL = re.compile(r"taken|picked|removed|gone|watch_off|battery|tamper")
DOORISH = re.compile(r"door|bell|porch|entr|front")
FILE = re.compile(r"^(\d{8}-\d{6})-(ring|parcel)-([a-z0-9_]+)\.jpg$")


def is_bell(state: State) -> bool:
    entity_id = state.entity_id
    key = entity_id.split(".", 1)[1]
    if NOT_BELL.search(key):
        return False
    if entity_id.startswith("event."):
        return state.attributes.get("device_class") == "doorbell" or bool(BELL_ID.search(key))
    return entity_id.startswith("binary_sensor.") and bool(BELL_ID.search(key))


def is_parcel(state: State) -> bool:
    entity_id = state.entity_id
    key = entity_id.split(".", 1)[1]
    return entity_id.startswith("binary_sensor.") and bool(PARCEL_ID.search(key)) and not NOT_PARCEL.search(key)


def moment_of(old: State | None, new: State | None) -> str | None:
    """'ring', 'parcel' or None for this state change."""
    if new is None or new.state in ("unavailable", "unknown"):
        return None
    if old is None or old.state in ("unavailable", "unknown"):
        # A sensor coming back after a restart is not a ring.
        return None
    if is_bell(new):
        if new.entity_id.startswith("event."):
            return "ring" if new.state != old.state else None
        return "ring" if new.state == "on" and old.state != "on" else None
    if is_parcel(new) and new.state == "on" and old.state != "on":
        return "parcel"
    return None


async def picture(hass: HomeAssistant, camera: str) -> bytes:
    """The camera's picture now."""
    from homeassistant.components.camera import async_get_image  # noqa: PLC0415

    return (await async_get_image(hass, camera, timeout=15)).content


def shrink(jpeg: bytes, width: int = WIDTH) -> bytes:
    """A smaller copy, so a day of pictures stays small on the disk and quick on a tablet."""
    from PIL import Image  # noqa: PLC0415 (Pillow comes with Home Assistant)

    with Image.open(BytesIO(jpeg)) as img:
        img = img.convert("RGB")
        if img.width > width:
            img = img.resize((width, max(1, round(img.height * width / img.width))))
        out = BytesIO()
        img.save(out, "JPEG", quality=72)
        return out.getvalue()


class Replay:
    """Keeps a picture of each ring and parcel of the last two days."""

    def __init__(self, hass: HomeAssistant, folder: Path) -> None:
        self.hass = hass
        self.folder = folder
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    def async_start(self) -> None:
        stops = [
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed),
            async_track_time_interval(self.hass, self._sweep, timedelta(hours=1)),
        ]

        @callback
        def stop(_event: Event) -> None:
            for s in stops:
                s()
            for task in list(self._tasks):
                task.cancel()

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
        self.hass.async_create_task(self._sweep(dt_util.utcnow()))

    @callback
    def _changed(self, event: Event) -> None:
        entity_id: str = event.data["entity_id"]
        if not entity_id.startswith(("event.", "binary_sensor.")):
            return
        kind = moment_of(event.data.get("old_state"), event.data.get("new_state"))
        if not kind:
            return
        now = time.time()
        if now - self._last.get(entity_id, 0) < SAME_VISIT:
            return
        self._last[entity_id] = now
        camera = self.camera_for(entity_id)
        if not camera:
            return
        task = self.hass.async_create_task(self._keep(kind, entity_id, camera, now))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def camera_for(self, source: str) -> str | None:
        """The camera that sees this bell: the same device, then the same room, then by name."""
        ents = er.async_get(self.hass)
        devs = dr.async_get(self.hass)
        cameras = [s.entity_id for s in self.hass.states.async_all("camera")]
        if not cameras:
            return None
        entry = ents.async_get(source)
        device = entry.device_id if entry else None

        def area_of(entity_id: str) -> str | None:
            e = ents.async_get(entity_id)
            if not e:
                return None
            if e.area_id:
                return e.area_id
            d = devs.async_get(e.device_id) if e.device_id else None
            return d.area_id if d else None

        if device:
            same = [c for c in cameras if (e := ents.async_get(c)) is not None and e.device_id == device]
            if same:
                return same[0]
        area = area_of(source)
        if area:
            here = [c for c in cameras if area_of(c) == area]
            if here:
                return here[0]
        key = source.split(".", 1)[1]
        words = [w for w in re.sub(r"_?(doorbell|ringing|ring|ding|visitor|button_pressed?|package\w*|parcel\w*)$", "", key).split("_") if len(w) > 2]
        named = [c for c in cameras if any(w in c for w in words)]
        if named:
            return named[0]
        doorish = [c for c in cameras if DOORISH.search(c)]
        return doorish[0] if doorish else None

    async def _keep(self, kind: str, source: str, camera: str, at: float) -> None:
        waited = 0
        for look in (FIRST_LOOK, SECOND_LOOK):
            await asyncio.sleep(look - waited)
            waited = look
            try:
                small = await self.hass.async_add_executor_job(shrink, await picture(self.hass, camera))
            except Exception as err:  # noqa: BLE001 (an asleep or offline camera is normal)
                _LOGGER.debug("Home replay: no picture from %s: %s", camera, err)
                continue
            stamp = datetime.fromtimestamp(at, dt_util.get_default_time_zone()).strftime("%Y%m%d-%H%M%S")
            name = f"{stamp}-{kind}-{re.sub(r'[^a-z0-9_]', '_', source.split('.', 1)[1])}.jpg"
            await self.hass.async_add_executor_job(self._write, name, small)
            return

    def _write(self, name: str, data: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / name).write_bytes(data)

    async def _sweep(self, _now: datetime) -> None:
        await self.hass.async_add_executor_job(self._remove_old)

    def _remove_old(self) -> None:
        if not self.folder.is_dir():
            return
        limit = time.time() - KEEP.total_seconds()
        for f in self.folder.glob("*.jpg"):
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass

    def moments(self, since: float, until: float) -> list[dict[str, Any]]:
        """The kept pictures between two times (seconds), oldest first, as small data addresses."""
        out: list[dict[str, Any]] = []
        if not self.folder.is_dir():
            return out
        tz = dt_util.get_default_time_zone()
        for f in sorted(self.folder.glob("*.jpg")):
            m = FILE.match(f.name)
            if not m:
                continue
            try:
                at = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=tz).timestamp()
            except ValueError:
                continue
            if not since <= at <= until:
                continue
            try:
                data = f.read_bytes()
            except OSError:
                continue
            domain = "event" if m.group(2) == "ring" and self.hass.states.get(f"event.{m.group(3)}") else "binary_sensor"
            out.append({
                "at": int(at * 1000),
                "kind": "parcel" if m.group(2) == "parcel" else "ring",
                "source": f"{domain}.{m.group(3)}",
                "image": "data:image/jpeg;base64," + base64.b64encode(data).decode(),
            })
        return out[-12:]


async def async_setup(hass: HomeAssistant) -> None:
    replay = Replay(hass, Path(hass.config.path("hadash", "replay")))
    hass.data[REPLAY] = replay
    replay.async_start()
    websocket_api.async_register_command(hass, ws_moments)


# Like the layout, this works for non-admin logins: wall tablets often use a plain user.
@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/replay/moments", vol.Required("since"): vol.Coerce(float), vol.Optional("until"): vol.Coerce(float)}
)
@websocket_api.async_response
async def ws_moments(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    replay: Replay | None = hass.data.get(REPLAY)
    if not replay:
        connection.send_result(msg["id"], {"moments": []})
        return
    since = msg["since"] / 1000
    until = msg.get("until", time.time() * 1000) / 1000
    moments = await hass.async_add_executor_job(replay.moments, since, until)
    connection.send_result(msg["id"], {"moments": moments})
