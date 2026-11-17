"""Home Assistant restarts and updates: tells the screens just before Home Assistant stops.

When Home Assistant stops, it closes every websocket at once, so a screen cannot tell a restart from a Wi-Fi
drop. A shutdown job runs first, while the websockets are still open: it tells every screen that listens,
so the screen can show its calm "restarting" picture at once instead of "cannot reach Home Assistant".
Any signed-in user may listen, like the layout, so a tablet with a non-admin login hears it too.
"""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HassJob, HomeAssistant, callback

LISTENERS = "hadash_stopping"
# Time for the message to leave before Home Assistant closes the connections.
FLUSH = 0.3


@callback
def async_register_stopping(hass: HomeAssistant) -> None:
    hass.data[LISTENERS] = {}
    websocket_api.async_register_command(hass, ws_server_subscribe)

    async def announce() -> None:
        listeners = list(hass.data.get(LISTENERS, {}).values())
        for connection, msg_id in listeners:
            try:
                connection.send_message(websocket_api.event_message(msg_id, {"state": "stopping"}))
            except Exception:  # noqa: BLE001 - a closed screen must not stop the others
                pass
        if listeners:
            await asyncio.sleep(FLUSH)

    hass.async_add_shutdown_job(HassJob(announce))


@websocket_api.websocket_command({vol.Required("type"): "hadash/server/subscribe"})
@callback
def ws_server_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A screen listens for "Home Assistant is stopping"."""
    listeners: dict[object, tuple[websocket_api.ActiveConnection, int]] = hass.data.setdefault(LISTENERS, {})
    key = object()
    listeners[key] = (connection, msg["id"])

    @callback
    def stop() -> None:
        listeners.pop(key, None)

    connection.subscriptions[msg["id"]] = stop
    connection.send_result(msg["id"])
