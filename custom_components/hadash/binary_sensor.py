"""Package present: one sensor for each camera that the studio's Doorstep watch looks at (see doorstep.py).
Room presence: one sensor for each room with a wall screen (see rooms.py).

binary_sensor.<camera>_package_watch is on while a package waits in the marked area. The tile's Package monitor
reads it like a camera's own package sensor, and automations can use it too.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOORSTEP
from .doorstep import DoorstepWatch, camera_name
from .rooms import ROOMS, UNIQUE, Rooms


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    watch: DoorstepWatch = hass.data[DOORSTEP]
    made: set[str] = set()

    def add(cameras: list[str]) -> None:
        fresh = [c for c in cameras if c not in made]
        made.update(fresh)
        if fresh:
            async_add_entities([PackagePresent(watch, c, camera_name(hass, c)) for c in fresh])

    watch.set_adder(add)

    rooms: Rooms | None = hass.data.get(ROOMS)
    if rooms is None:
        return
    rooms_made: set[str] = set()

    def add_rooms(areas: list[str]) -> None:
        fresh = [a for a in areas if a not in rooms_made]
        rooms_made.update(fresh)
        if fresh:
            async_add_entities([RoomPresence(rooms, a) for a in fresh])

    rooms.set_adder(add_rooms)


class PackagePresent(BinarySensorEntity, RestoreEntity):
    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:package-variant-closed"

    def __init__(self, watch: DoorstepWatch, camera: str, name: str) -> None:
        self._watch = watch
        self._camera = camera
        self._attr_name = f"{name} Package present"
        self._attr_unique_id = f"package_watch_{camera}"
        self.entity_id = f"binary_sensor.{camera.split('.', 1)[1]}_package_watch"

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.state == STATE_ON:
            since = dt_util.parse_datetime(str(last.attributes.get("since") or ""))
            self._watch.restore(self._camera, True, since)
        self.async_on_remove(self._watch.listen(self._changed))

    @callback
    def _changed(self, camera: str) -> None:
        if camera == self._camera:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._camera in self._watch.watches

    @property
    def is_on(self) -> bool:
        w = self._watch.watches.get(self._camera)
        return bool(w and w.present)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self._watch.watches.get(self._camera)
        since: datetime | None = w.since if w and w.present else None
        return {"camera": self._camera, "since": since.isoformat() if since else None}


class RoomPresence(BinarySensorEntity):
    """Someone is in the room: the wall tablets and the room's own sensors together (rooms.py)."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, rooms: Rooms, area: str) -> None:
        self._rooms = rooms
        self._area = area
        self._attr_name = f"{rooms.area_name(area)} presence"
        self._attr_unique_id = f"{UNIQUE}{area}"

    async def async_added_to_hass(self) -> None:
        # The sensor lives in its room, so the Living picture, Room to room and automations find it there.
        reg = er.async_get(self.hass)
        entry = reg.async_get(self.entity_id)
        if entry and not entry.area_id and ar.async_get(self.hass).async_get_area(self._area):
            reg.async_update_entity(self.entity_id, area_id=self._area)
        self.async_on_remove(self._rooms.listen(self._changed))
        self._rooms.update()

    @callback
    def _changed(self, area: str) -> None:
        if area == self._area:
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        room = self._rooms.room(self._area)
        return bool(room and room.on)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self._rooms.room(self._area)
        if room is None:
            return {"people": 0, "can_see": False, "who": []}
        return {"people": room.people, "can_see": room.can_see, "who": room.who, "noticed_by": room.how or None}
