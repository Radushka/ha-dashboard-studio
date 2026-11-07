"""Auto-update: install a new version of Dashboard Studio by itself, soon after it is out. Off until the person turns it on.

How often it looks is the person's choice: every 15 minutes (the default), every hour, or each night between 3:10
and 3:50. Each home picks its own minute, so not every home asks GitHub at once. GitHub is asked with the last answer's ETag, so a look with nothing new costs no rate limit. It uses the same
update entity as the studio's Update button. A failed install of a version is tried again only after a few hours,
so a store that refuses is not asked every 15 minutes.

The store (the Marketplace, or HACS before Home Assistant 2026.11) looks for new versions only every 48 hours, or
never without a GitHub connection. So the newest public release is read from GitHub too, and installed by its
version when the store has not seen it yet. Both stores accept a version.

Most releases change only the screen files: those work at once, and every tablet reloads by itself. When a release
changes the Python code, Home Assistant must restart to finish. The update is installed, but Home Assistant is NOT
restarted by itself (that could stop alarms or automations). A clear notice asks for the restart instead.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification, websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import AUTO, AUTO_STORAGE_KEY, STORAGE_VERSION
from .updates import _disk_state, async_status, find_update_entity

_LOGGER = logging.getLogger(__name__)

RELEASES = "https://api.github.com/repos/Radushka/ha-dashboard-studio/releases/latest"
NOTICE = "hadash_auto_update"
EVERIES = (1, 15, 60, 1440)  # minutes between looks: 1440 is once a night, at 3 and the home's minute
# 1 is hidden in the studio (shown while Option or Alt is held), for someone who releases often. GitHub's "nothing
# new" answer (304, thanks to the ETag) does not count against its limit, so once a minute is fine.
EVERY = 15  # the default
NIGHT_HOUR = 3
RETRY_AFTER = timedelta(hours=6)  # after a failed install of the same version


def compare(a: str, b: str) -> int:
    """Below 0 when version a is older than b, 0 when the same, above 0 when newer. "v1.4.9" < "1.4.10"."""
    def parts(v: str) -> list[int]:
        out = []
        for p in v.strip().lstrip("vV").split("."):
            digits = "".join(ch for ch in p if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out

    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)


_etag: dict[str, str] = {"etag": "", "tag": ""}


async def public_release(hass: HomeAssistant) -> str:
    """The tag of the newest public release ("v1.4.44"), or "" when GitHub cannot be reached.

    The last answer's ETag goes with each question: "nothing changed" (304) does not count against GitHub's limit."""
    headers = {"Accept": "application/vnd.github+json"}
    if _etag["etag"]:
        headers["If-None-Match"] = _etag["etag"]
    try:
        session = async_get_clientsession(hass)
        async with session.get(RELEASES, headers=headers, timeout=20) as res:
            if res.status == 304:
                return _etag["tag"]
            if res.status != 200:
                return ""
            data = await res.json()
            tag = str(data.get("tag_name") or "")
            _etag.update(etag=res.headers.get("ETag", "") if tag else "", tag=tag)
            return tag
    except Exception:  # noqa: BLE001 - no network now and then is normal; try again at the next look.
        return ""


class AutoUpdater:
    """Keeps the switch and the last result in Home Assistant's storage, and runs the nightly look."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, AUTO_STORAGE_KEY)
        self.enabled = False
        self.every = EVERY
        self.minute = random.randint(10, 50)
        # {"at": iso time, "version": "1.4.44", "restart": bool} or {"at", "error": text}; None before the first try.
        self.last: dict[str, Any] | None = None
        self._unsub = None
        self._lock = asyncio.Lock()
        self._started = False

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.enabled = bool(data.get("enabled", False))
        self.minute = int(data.get("minute", self.minute))
        every = data.get("every", EVERY)
        self.every = every if every in EVERIES else EVERY
        self.last = data.get("last")
        # Home Assistant just started, so a restart that an earlier night asked for has happened.
        persistent_notification.async_dismiss(self.hass, NOTICE)

    async def _save(self) -> None:
        await self._store.async_save({"enabled": self.enabled, "every": self.every, "minute": self.minute, "last": self.last})

    def minutes(self) -> list[int]:
        """The minutes of each hour when this home looks, such as [7, 22, 37, 52] every 15 minutes."""
        if self.every == 1:
            return list(range(60))
        if self.every == 15:
            return list(range(self.minute % 15, 60, 15))
        return [self.minute]

    @callback
    def _schedule(self) -> None:
        if self._unsub:
            self._unsub()
        hour = NIGHT_HOUR if self.every == 1440 else None
        self._unsub = async_track_time_change(self.hass, self._tick, hour=hour, minute=self.minutes(), second=0)

    @callback
    def async_start(self) -> None:
        self._started = True
        self._schedule()

        # Home Assistant does not end this timer itself when it stops; a test then finds it still waiting.
        @callback
        def stop(_event: Event) -> None:
            if self._unsub:
                self._unsub()
                self._unsub = None

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)

    async def _tick(self, _now: datetime | None) -> None:
        if self.enabled and not self._lock.locked():
            async with self._lock:
                await self.async_run()

    async def async_set(self, enabled: bool | None = None, every: int | None = None) -> None:
        if enabled is not None:
            self.enabled = enabled
        if every is not None and every in EVERIES and every != self.every:
            self.every = every
            if self._started:
                self._schedule()
        await self._save()

    def info(self) -> dict[str, Any]:
        # "every" is new; "time" stays for studios of 1.4.60 and older, which show "each night at about {time}".
        return {"enabled": self.enabled, "every": self.every, "time": f"{NIGHT_HOUR}:{self.minute:02d}", "last": self.last}

    def _failed_lately(self, target: str) -> bool:
        """True when the last try was this same version, and it failed less than a few hours ago."""
        last = self.last or {}
        if not last.get("error") or compare(str(last.get("version") or ""), target) != 0:
            return False
        at = dt_util.parse_datetime(str(last.get("at") or ""))
        return at is not None and dt_util.utcnow() - at < RETRY_AFTER

    async def async_run(self) -> dict[str, Any] | None:
        """Look once and install a newer version. Returns the new last result, or None when there was nothing new."""
        hass = self.hass
        entity_id = find_update_entity(hass)
        if entity_id is None:
            if (self.last or {}).get("error") == "no_store":
                return self.last  # Said already; no need to write it again at every look.
            return await self._result({"error": "no_store"})
        _fp, files = await hass.async_add_executor_job(_disk_state)
        state = hass.states.get(entity_id)
        store_latest = str(state.attributes.get("latest_version") or "") if state else ""
        public = await public_release(hass)
        target, version = store_latest, None
        if public and (not store_latest or compare(public, store_latest) > 0):
            # The store has not seen this release yet: ask for it by name.
            target, version = public, public
        if not target or not files or compare(target, files) <= 0:
            return None
        if self._failed_lately(target):
            return None
        data: dict[str, Any] = {"entity_id": entity_id}
        if version:
            data["version"] = version
        try:
            await hass.services.async_call("update", "install", data, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Dashboard Studio auto-update to %s failed: %s", target, err)
            return await self._result({"error": str(err) or type(err).__name__, "version": target.lstrip("vV")})
        status = await async_status(hass)
        new = status["files"] or target.lstrip("vV")
        if status["restart_needed"]:
            persistent_notification.async_create(
                hass,
                f"Dashboard Studio was updated to {new} by itself. "
                "This version needs a restart of Home Assistant to finish. "
                "Restart it when it suits you: Settings, then the three dots, then Restart Home Assistant. "
                "The screens keep working until then.",
                title="Dashboard Studio: restart to finish the update",
                notification_id=NOTICE,
            )
        _LOGGER.info("Dashboard Studio updated itself to %s (restart needed: %s)", new, status["restart_needed"])
        return await self._result({"version": new, "restart": status["restart_needed"]})

    async def _result(self, last: dict[str, Any]) -> dict[str, Any]:
        self.last = {"at": dt_util.utcnow().isoformat(), **last}
        await self._save()
        return self.last


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/update/auto", vol.Optional("enabled"): bool, vol.Optional("every"): vol.In(EVERIES)}
)
@websocket_api.async_response
async def ws_auto(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The auto-update switch: {enabled, every, time, last}. With "enabled" or "every", changes them first."""
    auto: AutoUpdater = hass.data[AUTO]
    if "enabled" in msg or "every" in msg:
        await auto.async_set(msg.get("enabled"), msg.get("every"))
    connection.send_result(msg["id"], auto.info())
