"""Eufy cameras: put a camera back to sleep after the dashboard woke it, without a false error in the log.

A Eufy livestream ends by itself after a while. The eufy_security integration then marks the camera idle at once,
but Home Assistant keeps showing the old "streaming" state until its next update. So a screen that closes its live
view cannot tell from the state that the stream already ended. A stop sent then fails with
"device_livestream_not_running", and Home Assistant logs that failure as an unexpected error.

This command asks the camera itself (the integration's live is_streaming) before it stops anything:
1. Already stopped: nothing is sent, and the stale "streaming" state is corrected for every screen.
2. Still streaming: the stop is sent. If the stream ended in that same moment, the "not running" answer is
   expected and kept out of the log. Every other failure is raised as before, so real errors stay visible.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

_LOGGER = logging.getLogger(__name__)

EUFY = "eufy_security"
NOT_RUNNING = "device_livestream_not_running"


def _camera(hass: HomeAssistant, entity_id: str) -> Any | None:
    """The camera entity object, or None when it cannot be found (then the stop is simply sent)."""
    for component in (hass.data.get("entity_components", {}).get("camera"), hass.data.get("camera")):
        get = getattr(component, "get_entity", None)
        if get is not None:
            return get(entity_id)
    return None


def _not_running(err: BaseException) -> bool:
    return getattr(err, "error_code", None) == NOT_RUNNING or NOT_RUNNING in str(err)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/eufy/sleep",
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("way"): vol.In(["rtsp", "p2p"]),
    }
)
@websocket_api.async_response
async def ws_eufy_sleep(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Stop a Eufy livestream the dashboard started. Answers {"stopped": true} or {"stopped": false, "why": ...}."""
    entity_id = msg["entity_id"]
    camera = _camera(hass, entity_id)
    live = getattr(camera, "is_streaming", None)
    if live is False:
        state = hass.states.get(entity_id)
        if state is not None and state.state == "streaming":
            camera.async_write_ha_state()
        connection.send_result(msg["id"], {"stopped": False, "why": "ended"})
        return
    try:
        await hass.services.async_call(
            EUFY, f"stop_{msg['way']}_livestream", target={"entity_id": entity_id}, blocking=True, context=connection.context(msg)
        )
    except Exception as err:  # noqa: BLE001 - only the expected answer is kept quiet; the rest is raised again
        if not _not_running(err):
            raise
        _LOGGER.debug("%s: the livestream had already ended, so there was nothing to stop", entity_id)
        if camera is not None and hasattr(camera, "async_write_ha_state"):
            camera.async_write_ha_state()
        connection.send_result(msg["id"], {"stopped": False, "why": "ended"})
        return
    connection.send_result(msg["id"], {"stopped": True})
