"""Auto-update: once a night, install a new version of Dashboard Studio by itself. Off until the person turns it on.

It runs at a quiet minute between 3:10 and 3:50 at night (Home Assistant's time zone); each home picks its own minute,
so not every home asks GitHub at once. It uses the same update entity as the studio's Update button.

The store (the Marketplace, or HACS before Home Assistant 2026.11) looks for new versions only every 48 hours, or
never without a GitHub connection. So the newest public release is read from GitHub too, and installed by its
version when the store has not seen it yet. Both stores accept a version.

Most releases change only the screen files: those work at once, and every tablet reloads by itself. When a release
changes the Python code, Home Assistant must restart to finish. The update is installed, but Home Assistant is NOT
restarted by itself (that could stop alarms or automations in the night). A clear notice asks for the restart instead.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification, websocket_api
from homeassistant.core import HomeAssistant, callback
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
HOUR = 3


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


async def public_release(hass: HomeAssistant) -> str:
    """The tag of the newest public release ("v1.4.44"), or "" when GitHub cannot be reached."""
    try:
        session = async_get_clientsession(hass)
        async with session.get(RELEASES, headers={"Accept": "application/vnd.github+json"}, timeout=20) as res:
            if res.status != 200:
                return ""
            data = await res.json()
            return str(data.get("tag_name") or "")
    except Exception:  # noqa: BLE001 - no network at night is normal; try again tomorrow.
        return ""


class AutoUpdater:
    """Keeps the switch and the last result in Home Assistant's storage, and runs the nightly look."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, AUTO_STORAGE_KEY)
        self.enabled = False
        self.minute = random.randint(10, 50)
        # {"at": iso time, "version": "1.4.44", "restart": bool} or {"at", "error": text}; None before the first try.
        self.last: dict[str, Any] | None = None
        self._unsub = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.enabled = bool(data.get("enabled", False))
        self.minute = int(data.get("minute", self.minute))
        self.last = data.get("last")
        # Home Assistant just started, so a restart that an earlier night asked for has happened.
        persistent_notification.async_dismiss(self.hass, NOTICE)

    async def _save(self) -> None:
        await self._store.async_save({"enabled": self.enabled, "minute": self.minute, "last": self.last})

    @callback
    def async_start(self) -> None:
        self._unsub = async_track_time_change(self.hass, self._nightly, hour=HOUR, minute=self.minute, second=0)

    async def _nightly(self, _now: datetime) -> None:
        if self.enabled:
            await self.async_run()

    async def async_set(self, enabled: bool) -> None:
        self.enabled = enabled
        await self._save()

    def info(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "time": f"{HOUR}:{self.minute:02d}", "last": self.last}

    async def async_run(self) -> dict[str, Any] | None:
        """Look once and install a newer version. Returns the new last result, or None when there was nothing new."""
        hass = self.hass
        entity_id = find_update_entity(hass)
        if entity_id is None:
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
                f"Dashboard Studio was updated to {new} during the night. "
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
@websocket_api.websocket_command({vol.Required("type"): "hadash/update/auto", vol.Optional("enabled"): bool})
@websocket_api.async_response
async def ws_auto(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The auto-update switch: {enabled, time, last}. With "enabled", turns it on or off first."""
    auto: AutoUpdater = hass.data[AUTO]
    if "enabled" in msg:
        await auto.async_set(msg["enabled"])
    connection.send_result(msg["id"], auto.info())
