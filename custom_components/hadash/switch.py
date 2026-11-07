"""Guest mode (free): one switch makes every wall tablet safe for guests.

While it is on, the tablets hide the alarm, the cameras, the doors and locks, family places, the network and the
history, and show a welcome card with the Wi-Fi code. The switch is Home Assistant's own, so the Home Assistant app
and automations can use it. It turns on by itself while an event is on the calendar chosen in the studio
(theme.guest.calendar of the published layout), and off again when that event ends.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def guest_settings(hass: HomeAssistant) -> dict[str, Any]:
    """The studio's guest settings from the published main layout."""
    layout = hass.data[DOMAIN].layout or {}
    theme = layout.get("theme") if isinstance(layout, dict) else None
    guest = theme.get("guest") if isinstance(theme, dict) else None
    return guest if isinstance(guest, dict) else {}


def event_counts(settings: dict[str, Any], title: str) -> bool:
    """An event turns guest mode on when its title has one of the words, or when no words are set."""
    words = [str(w).strip().lower() for w in settings.get("words") or [] if str(w).strip()]
    return not words or any(w in (title or "").lower() for w in words)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([GuestSwitch(hass)])


class GuestSwitch(SwitchEntity, RestoreEntity):
    """switch.dashboard_studio_guest_mode."""

    _attr_has_entity_name = False
    _attr_name = "Dashboard Studio guest mode"
    _attr_unique_id = "guest_mode"
    _attr_icon = "mdi:account-key"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attr_is_on = False
        # True when the calendar turned it on, so only the calendar's end turns it off by itself.
        self._by_calendar = False
        self._calendar = ""
        self._stop_calendar: Any = None

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == STATE_ON
            self._by_calendar = bool(last.attributes.get("by_calendar"))
        self._follow_calendar()
        # A new publish may choose another calendar.
        self.async_on_remove(self.hass.data[DOMAIN].async_listen(lambda _data: self._follow_calendar()))
        self.async_on_remove(self._unfollow)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"by_calendar": self._by_calendar}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._by_calendar = False
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._by_calendar = False
        self._set(False)

    @callback
    def _set(self, on: bool) -> None:
        self._attr_is_on = on
        self.async_write_ha_state()

    # ---- The calendar ----

    @callback
    def _unfollow(self) -> None:
        if self._stop_calendar:
            self._stop_calendar()
            self._stop_calendar = None

    @callback
    def _follow_calendar(self) -> None:
        calendar = str(guest_settings(self.hass).get("calendar") or "")
        if calendar == self._calendar and (self._stop_calendar or not calendar):
            return
        self._unfollow()
        self._calendar = calendar
        if not calendar.startswith("calendar."):
            return
        self._stop_calendar = async_track_state_change_event(self.hass, [calendar], self._calendar_changed)
        # An event that is on right now counts too, so a guest who is already here is not missed.
        state = self.hass.states.get(calendar)
        if state and state.state == STATE_ON and not self._attr_is_on and event_counts(guest_settings(self.hass), str(state.attributes.get("message") or "")):
            self._by_calendar = True
            self._set(True)

    @callback
    def _calendar_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        old = event.data["old_state"]
        if new is None:
            return
        title = str(new.attributes.get("message") or "")
        started = new.state == STATE_ON and (old is None or old.state != STATE_ON or old.attributes.get("message") != new.attributes.get("message"))
        if started and event_counts(guest_settings(self.hass), title):
            if not self._attr_is_on:
                self._by_calendar = True
                self._set(True)
        elif new.state != STATE_ON and self._by_calendar and self._attr_is_on:
            # The event ended: guest mode ends with it, unless someone turned it on by hand.
            self._by_calendar = False
            self._set(False)
