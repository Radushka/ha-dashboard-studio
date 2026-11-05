"""Websocket commands. Any signed-in user can read the layout, so a tablet can use a non-admin login;
only an admin can publish."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .assistant import conversation_agents
from .const import DOMAIN, DRAFT, PRO
from .draft import DraftStore
from .layout import LayoutStore
from .license import LicenseProblem, ProManager
from .photos import ws_photos
from .updates import ws_install, ws_restart, ws_update

# A layout is a few hundred kilobytes at most, even with many screens. This stops a mistake from filling the disk.
MAX_LAYOUT = 2_000_000


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    return {k: data.get(k) for k in ("layout", "revision", "published_at")}


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_publish)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_version)
    websocket_api.async_register_command(hass, ws_pro)
    websocket_api.async_register_command(hass, ws_pro_activate)
    websocket_api.async_register_command(hass, ws_pro_remove)
    websocket_api.async_register_command(hass, ws_photos)
    websocket_api.async_register_command(hass, ws_update)
    websocket_api.async_register_command(hass, ws_install)
    websocket_api.async_register_command(hass, ws_restart)
    websocket_api.async_register_command(hass, ws_saver_show)
    websocket_api.async_register_command(hass, ws_saver_subscribe)
    websocket_api.async_register_command(hass, ws_draft_get)
    websocket_api.async_register_command(hass, ws_draft_save)
    websocket_api.async_register_command(hass, ws_draft_subscribe)
    websocket_api.async_register_command(hass, ws_agents)


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
    # The draft is live now; an assistant works on the published layout until the studio changes it again.
    drafts: DraftStore = hass.data[DRAFT]
    drafts.clear()
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


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/layout/history"})
@callback
def ws_history(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The last published versions, newest first: revision, when, by whom, and how many cards and screens."""
    store: LayoutStore = hass.data[DOMAIN]
    connection.send_result(msg["id"], {"versions": store.versions()})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/layout/version", vol.Required("revision"): int})
@callback
def ws_version(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """One kept version with its layout, so the studio can open it. Nothing changes until it is published."""
    store: LayoutStore = hass.data[DOMAIN]
    version = store.version(msg["revision"])
    if version is None:
        connection.send_error(msg["id"], "not_found", "That version is no longer kept.")
        return
    connection.send_result(msg["id"], version)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/pro"})
@callback
def ws_pro(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Pro in this Home Assistant: {plan: pro | trial | free, daysLeft, license: {name, email, number} | null}."""
    pro: ProManager = hass.data[PRO]
    connection.send_result(msg["id"], pro.state())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/pro/activate", vol.Required("license"): str})
@websocket_api.async_response
async def ws_pro_activate(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Turns a license on for this Home Assistant. The error message is written for the user."""
    pro: ProManager = hass.data[PRO]
    try:
        connection.send_result(msg["id"], await pro.async_activate(msg["license"]))
    except LicenseProblem as err:
        connection.send_error(msg["id"], "license", str(err))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/pro/remove"})
@websocket_api.async_response
async def ws_pro_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Turns the license off here, so it can be used somewhere else."""
    pro: ProManager = hass.data[PRO]
    connection.send_result(msg["id"], await pro.async_remove())


# "Show now" in the studio: the tablets of one screen start their screensaver at once, to try it out.
SAVER_EVENT = "hadash_saver"


@websocket_api.websocket_command({vol.Required("type"): "hadash/saver/show", vol.Optional("screen", default=""): str})
@websocket_api.require_admin
@callback
def ws_saver_show(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Asks the tablets of a screen ("" is the main layout) to show their screensaver now."""
    hass.bus.async_fire(SAVER_EVENT, {"screen": msg["screen"]})
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/saver/subscribe"})
@callback
def ws_saver_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Passes "Show now" on to a tablet. Like the layout, this works for non-admin logins too."""

    @callback
    def forward(event: Any) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"screen": event.data.get("screen", "")}))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(SAVER_EVENT, forward)
    connection.send_result(msg["id"])


# Build a screen by talking (assistant.py): the studio shares its draft, and gets the assistant's changes at once.


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/draft"})
@callback
def ws_draft_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The draft kept in Home Assistant: {project, base, change, by, note, updated}. Fields are null when empty."""
    drafts: DraftStore = hass.data[DRAFT]
    connection.send_result(msg["id"], drafts.public())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/draft/save",
        vol.Required("project"): dict,
        vol.Required("base"): int,
        vol.Required("seen"): int,
    }
)
@callback
def ws_draft_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The studio's draft, a moment after each change. Refused (saved: false) when the assistant changed it
    since the studio last looked; the studio then takes the assistant's version first."""
    if len(str(msg["project"])) > MAX_LAYOUT:
        connection.send_error(msg["id"], "too_large", "The layout is too large to save.")
        return
    drafts: DraftStore = hass.data[DRAFT]
    saved = drafts.save_from_studio(msg["project"], msg["base"], msg["seen"])
    connection.send_result(msg["id"], {"saved": saved, "change": drafts.change})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/draft/subscribe"})
@callback
def ws_draft_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Sends each change the assistant makes, as it makes it."""
    drafts: DraftStore = hass.data[DRAFT]

    @callback
    def forward(data: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], data))

    connection.subscriptions[msg["id"]] = drafts.async_listen(forward)
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/assistant/agents"})
@callback
def ws_agents(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The AI agents set up in Home Assistant for the Ask box; "ready" ones may use Dashboard Studio."""
    connection.send_result(msg["id"], {"agents": conversation_agents(hass)})
