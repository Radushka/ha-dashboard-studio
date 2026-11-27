"""Websocket commands. Any signed-in user can read the layout, so a tablet can use a non-admin login;
only an admin can publish."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .assistant import conversation_agents
from .const import DOMAIN, DRAFT, PRO
from .draft import DraftStore
from .eufy import ws_eufy_sleep
from .eufy_live import ws_eufy_live
from .firetv import ws_tv_find, ws_tv_forget, ws_tv_setup, ws_tv_update_app
from .tvremote import ws_tv_key, ws_tv_remotes, ws_tv_watch
from .layout import LayoutStore
from .license import LicenseProblem, ProManager
from .photos import ws_photos
from .spots import ws_photo_spot
from .autoupdate import ws_auto
from .updates import ws_install, ws_restart, ws_update
from .voicecheck import ws_voice_check, ws_voice_check_apply, ws_voice_check_undo

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
    websocket_api.async_register_command(hass, ws_photo_spot)
    websocket_api.async_register_command(hass, ws_update)
    websocket_api.async_register_command(hass, ws_install)
    websocket_api.async_register_command(hass, ws_restart)
    websocket_api.async_register_command(hass, ws_auto)
    websocket_api.async_register_command(hass, ws_saver_show)
    websocket_api.async_register_command(hass, ws_saver_subscribe)
    websocket_api.async_register_command(hass, ws_moment_play)
    websocket_api.async_register_command(hass, ws_moment_subscribe)
    websocket_api.async_register_command(hass, ws_draft_get)
    websocket_api.async_register_command(hass, ws_draft_save)
    websocket_api.async_register_command(hass, ws_draft_subscribe)
    websocket_api.async_register_command(hass, ws_agents)
    websocket_api.async_register_command(hass, ws_tv_find)
    websocket_api.async_register_command(hass, ws_tv_setup)
    websocket_api.async_register_command(hass, ws_tv_forget)
    websocket_api.async_register_command(hass, ws_tv_update_app)
    websocket_api.async_register_command(hass, ws_tv_remotes)
    websocket_api.async_register_command(hass, ws_tv_watch)
    websocket_api.async_register_command(hass, ws_tv_key)
    websocket_api.async_register_command(hass, ws_voice_check)
    websocket_api.async_register_command(hass, ws_voice_check_apply)
    websocket_api.async_register_command(hass, ws_voice_check_undo)
    websocket_api.async_register_command(hass, ws_secure_url)
    websocket_api.async_register_command(hass, ws_eufy_sleep)
    websocket_api.async_register_command(hass, ws_eufy_live)


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


def secure_url(hass: HomeAssistant) -> str | None:
    """This Home Assistant's https address away from home: its own external one, else Home Assistant Cloud's."""
    try:
        return get_url(hass, allow_internal=False, allow_ip=False, require_ssl=True, require_standard_port=True, allow_cloud=True)
    except NoURLAvailableError:
        return None


@websocket_api.websocket_command({vol.Required("type"): "hadash/secure_url"})
@callback
def ws_secure_url(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """{url}: browsers allow the microphone only on https, so a page opened at http offers this address to talk.
    Any signed-in user: a phone often has a non-admin login."""
    connection.send_result(msg["id"], {"url": secure_url(hass)})


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


# "Play on the tablets" in the studio: one moment (rain on the glass, the doorbell, welcome home...) plays on the
# real tablets of one screen for a short while, so it can be seen or filmed on the wall. Nothing in the home changes.
MOMENT_EVENT = "hadash_moment"
MOMENT_TABLETS = "hadash_moment_tablets"


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/moment/play",
        vol.Optional("screen", default=""): str,
        vol.Required("moment"): vol.Match(r"^[a-z][a-z-]{0,39}$"),
        vol.Optional("seconds", default=20): vol.All(int, vol.Range(min=3, max=60)),
    }
)
@websocket_api.require_admin
@callback
def ws_moment_play(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Asks the tablets of a screen ("" is the main layout) to play one moment now. Answers how many listen."""
    tablets: dict[int, str] = hass.data.setdefault(MOMENT_TABLETS, {})
    count = sum(1 for screen in tablets.values() if screen == msg["screen"])
    hass.bus.async_fire(MOMENT_EVENT, {"screen": msg["screen"], "moment": msg["moment"], "seconds": msg["seconds"]})
    connection.send_result(msg["id"], {"tablets": count})


@websocket_api.websocket_command({vol.Required("type"): "hadash/moment/subscribe", vol.Optional("screen", default=""): str})
@callback
def ws_moment_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A tablet waits for "Play on the tablets". Like the layout, this works for non-admin logins too."""
    tablets: dict[int, str] = hass.data.setdefault(MOMENT_TABLETS, {})
    key = id(connection) * 1000 + msg["id"]
    tablets[key] = msg["screen"]

    @callback
    def forward(event: Any) -> None:
        if event.data.get("screen", "") == msg["screen"]:
            connection.send_message(websocket_api.event_message(msg["id"], {"moment": event.data.get("moment"), "seconds": event.data.get("seconds", 20)}))

    stop = hass.bus.async_listen(MOMENT_EVENT, forward)

    @callback
    def gone() -> None:
        tablets.pop(key, None)
        stop()

    connection.subscriptions[msg["id"]] = gone
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
