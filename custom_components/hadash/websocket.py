"""Websocket commands. Any signed-in user can read the layout, so a tablet can use a non-admin login;
only an admin can publish."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .layout import LayoutStore

# A layout is a few hundred kilobytes at most, even with many screens. This stops a mistake from filling the disk.
MAX_LAYOUT = 2_000_000


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    return {k: data.get(k) for k in ("layout", "revision", "published_at")}


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_publish)
    websocket_api.async_register_command(hass, ws_subscribe)


@websocket_api.websocket_command({vol.Required("type"): "hadash/layout"})
@callback
def ws_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The published layout, or layout: null when nothing was published yet."""
    store: LayoutStore = hass.data[DOMAIN]
    connection.send_result(msg["id"], _summary(store.data))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/layout/publish", vol.Required("layout"): dict}
)
@websocket_api.async_response
async def ws_publish(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Saves a new layout. Every tablet that subscribed gets it at once."""
    layout = msg["layout"]
    if len(str(layout)) > MAX_LAYOUT:
        connection.send_error(msg["id"], "too_large", "The layout is too large to save.")
        return
    store: LayoutStore = hass.data[DOMAIN]
    data = await store.async_publish(layout, connection.user.name if connection.user else None)
    connection.send_result(msg["id"], {"revision": data["revision"], "published_at": data["published_at"]})


@websocket_api.websocket_command({vol.Required("type"): "hadash/layout/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Sends each new layout as it is published. Unlike HA events, this works for non-admin logins too."""
    store: LayoutStore = hass.data[DOMAIN]

    @callback
    def forward(data: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], _summary(data)))

    connection.subscriptions[msg["id"]] = store.async_listen(forward)
    connection.send_result(msg["id"])
