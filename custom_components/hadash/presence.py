"""Who is home, decided by Dashboard Studio itself: the newest report wins.

Home Assistant's own person state lets any non-GPS tracker that says "home" win over GPS. So a Wi-Fi tracker that
stopped reporting keeps a person home all day (14 Nov 2026: Angela left at 11:10 AM, her iPhone said away, but
Home Radar had stopped on the Mac and its tracker kept "home"). Here every source of a person counts, a source that
is off or silent is left out, and the newest report wins. The same rule is in renderer/src/lib/whoIsHome.ts.

Sources: the person's own trackers (attribute device_trackers) and the Home Radar tracker named after them
("Angela (Home Radar)"). Home Radar is left out while its Mac is offline (binary_sensor.home_radar_online not "on")
and when its updated_at is older than 20 minutes. A report's time is the source's own time when it sends one
(Home Radar last_seen), else when Home Assistant last heard from it. When no source is left, Home Assistant's
person state stands.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

RADAR_ONLINE = "binary_sensor.home_radar_online"
SILENT_S = 20 * 60
OFF = ("unavailable", "unknown", "")
# Reports this close together are a tie: Home Assistant's own person state decides then (after a restart every
# state is written at the same moment).
TIE_S = 60


def _time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    t = dt_util.parse_datetime(value)
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt_util.UTC)


def is_radar(s: State) -> bool:
    return bool(re.match(r"^device_tracker\.(home_radar|lanradar)_|^device_tracker\..+_home_radar$", s.entity_id)) or str(
        s.attributes.get("friendly_name", "")
    ).endswith("(Home Radar)")


def _first(name: str) -> str:
    return (name.split(" ")[0] if name else "").lower()


def sources(hass: HomeAssistant, person: State) -> list[State]:
    ids = [i for i in person.attributes.get("device_trackers") or [] if isinstance(i, str)]
    out = [s for s in (hass.states.get(i) for i in ids) if s]
    first = _first(str(person.attributes.get("friendly_name", "")))
    if first and not any(is_radar(s) for s in out):
        for s in hass.states.async_all("device_tracker"):
            if is_radar(s) and _first(str(s.attributes.get("friendly_name", ""))) == first:
                out.append(s)
                break
    return out


def _report_time(s: State) -> datetime:
    return _time(s.attributes.get("last_seen")) or s.last_updated


def _usable(hass: HomeAssistant, s: State, now: datetime) -> bool:
    if s.state in OFF:
        return False
    if is_radar(s):
        online = hass.states.get(RADAR_ONLINE)
        if online is not None and online.state != "on":
            return False
        seen = _time(s.attributes.get("updated_at"))
        if seen is not None and (now - seen).total_seconds() > SILENT_S:
            return False
    return True


def where(hass: HomeAssistant, person: State, now: datetime | None = None) -> str:
    """The person's place: "home", "not_home" or a zone name."""
    now = now or dt_util.utcnow()
    usable = [s for s in sources(hass, person) if _usable(hass, s, now)]
    if not usable:
        return person.state
    usable.sort(key=_report_time, reverse=True)
    newest = usable[0]
    if len(usable) > 1 and (_report_time(newest) - _report_time(usable[1])).total_seconds() < TIE_S:
        if newest.state != usable[1].state and person.state not in OFF:
            return person.state
    return newest.state


def is_home(hass: HomeAssistant, person: State) -> bool:
    return where(hass, person) == "home"
