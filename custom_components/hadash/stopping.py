"""Home Assistant restarts and updates: tells the screens just before Home Assistant stops.

When Home Assistant stops, it closes every websocket at once, so a screen cannot tell a restart from a Wi-Fi
drop. A shutdown job runs first, while the websockets are still open: it tells every screen that listens,
so the screen can show its calm "restarting" picture at once instead of "cannot reach Home Assistant".
Any signed-in user may listen, like the layout, so a tablet with a non-admin login hears it too.

It also learns how long this home takes to come back: the time from "stopping" to "started" is kept for the
last few restarts (and, apart, for updates of Home Assistant itself, which take longer). The screens get the
usual time, so the ring can fill like a real progress bar and the words can say "Back in about a minute".
With "stopping", the screens also get the names of the integrations that run now, so a slow start can say
which one Home Assistant still waits for ("Still waiting for Eufy Security") instead of a raw name.
"""

from __future__ import annotations

import asyncio
from statistics import median
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers.storage import Store

LISTENERS = "hadash_stopping"
LEARNED = "hadash_restart_times"
STORAGE_KEY = "hadash.restart_times"
# Time for the message to leave before Home Assistant closes the connections.
FLUSH = 0.3
# The restarts that count: the last few, so a faster new machine shows soon.
KEEP = 5
# Longer than this is not a restart (the machine was off, or someone stopped it for the night).
LONGEST = 30 * 60


class RestartTimes:
    """The usual time to come back, learned from the last restarts of this home."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, 1, STORAGE_KEY)
        self.data: dict[str, Any] = {}

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or {}

    def expect(self) -> dict[str, int]:
        """Seconds a restart and an update usually take here (the middle of the last few, not the average)."""
        out: dict[str, int] = {}
        for kind in ("restart", "update"):
            times = self.data.get(kind) or []
            if times:
                out[kind] = round(median(times))
        return out

    async def async_stopping(self, kind: str) -> None:
        """Home Assistant stops now: note the time, so the start can count."""
        self.data["stopped"] = {"at": time.time(), "kind": kind}
        await self._store.async_save(self.data)

    async def async_started(self) -> None:
        """Home Assistant runs again: the time since "stopping" is one more restart learned."""
        stopped = self.data.pop("stopped", None)
        if not stopped:
            return
        took = time.time() - float(stopped.get("at", 0))
        kind = "update" if stopped.get("kind") == "update" else "restart"
        if 5 <= took <= LONGEST:
            self.data[kind] = [*(self.data.get(kind) or []), round(took)][-KEEP:]
        await self._store.async_save(self.data)


def updating_itself(hass: HomeAssistant) -> bool:
    """Home Assistant's own update (Core, the operating system or the Supervisor) installs now."""
    for state in hass.states.async_all("update"):
        if state.attributes.get("in_progress") and (
            "home_assistant_" in state.entity_id or str(state.attributes.get("title", "")).startswith("Home Assistant ")
        ):
            return True
    return False


def integration_names(hass: HomeAssistant) -> dict[str, str]:
    """The friendly names of the integrations that run now ("eufy_security": "Eufy Security")."""
    from homeassistant.loader import async_get_loaded_integration  # noqa: PLC0415 - only at shutdown

    names: dict[str, str] = {}
    for domain in hass.config.top_level_components:
        try:
            name = async_get_loaded_integration(hass, domain).name
        except Exception:  # noqa: BLE001 - a name is a nicety
            continue
        if name and name != domain:
            names[domain] = name
    return names


@callback
def async_register_stopping(hass: HomeAssistant) -> None:
    hass.data[LISTENERS] = {}
    times = hass.data[LEARNED] = RestartTimes(hass)
    websocket_api.async_register_command(hass, ws_server_subscribe)

    async def announce() -> None:
        kind = "update" if updating_itself(hass) else "restart"
        listeners = list(hass.data.get(LISTENERS, {}).values())
        if listeners:
            message = {"state": "stopping", "expect": times.expect(), "names": integration_names(hass)}
            for connection, msg_id in listeners:
                try:
                    connection.send_message(websocket_api.event_message(msg_id, message))
                except Exception:  # noqa: BLE001 - a closed screen must not stop the others
                    pass
        try:
            await times.async_stopping(kind)
        except Exception:  # noqa: BLE001 - learning must never hold up the stop
            pass
        if listeners:
            await asyncio.sleep(FLUSH)

    hass.async_add_shutdown_job(HassJob(announce))

    loading = hass.async_create_task(times.async_load())

    async def started(_: Event) -> None:
        await loading
        await times.async_started()

    # Set up after the start (a reload): nothing to learn, the stop was too long ago to say.
    if hass.state is not CoreState.running:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, started)


@websocket_api.websocket_command({vol.Required("type"): "hadash/server/subscribe"})
@callback
def ws_server_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A screen listens for "Home Assistant is stopping". It also hears the usual restart time at once."""
    listeners: dict[object, tuple[websocket_api.ActiveConnection, int]] = hass.data.setdefault(LISTENERS, {})
    key = object()
    listeners[key] = (connection, msg["id"])

    @callback
    def stop() -> None:
        listeners.pop(key, None)

    connection.subscriptions[msg["id"]] = stop
    connection.send_result(msg["id"])
    times: RestartTimes | None = hass.data.get(LEARNED)
    if times and (expect := times.expect()):
        connection.send_message(websocket_api.event_message(msg["id"], {"state": "running", "expect": expect}))
