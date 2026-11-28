"""Dashboard Studio: serves the wall-tablet dashboard and its studio, and keeps the published layout.

The dashboard files come with this integration, so an update through HACS updates every tablet.
The layout lives in Home Assistant's own storage, so it is part of every HA backup.
"""

from __future__ import annotations

import logging

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.start import async_at_started

from .alarms import async_setup as async_setup_alarms
from .care import async_setup as async_setup_care
from .family_window import async_setup as async_setup_family_window
from .intercom import async_setup as async_setup_intercom
from .gestures import async_setup as async_setup_gestures
from .energy import async_setup as async_setup_energy
from .drive import async_setup as async_setup_drive
from .eufy_live import async_setup as async_setup_eufy_live
from .door_talk import async_setup as async_setup_door_talk
from .notices import async_setup as async_setup_notices
from .nudges import async_setup as async_setup_nudges
from .replay import async_setup as async_setup_replay
from .habits import async_setup as async_setup_habits
from .safety import async_setup as async_setup_safety
from .phones import async_setup as async_setup_phones
from .guest_link import async_setup as async_setup_guest_link
from .reminders import async_setup as async_setup_reminders
from .timed import async_setup as async_setup_timed
from .assistant import async_register as async_register_assistant
from .autoupdate import AutoUpdater
from .const import AUTO, DOMAIN, DOORSTEP, DRAFT, PANEL, PHOTO_VIEW, PRO, SATELLITES, SPOTS, TVS, URL_BASE, VERSION, WWW
from .doorstep import DoorstepWatch
from .draft import DraftStore
from .ears import async_register as async_register_ears
from .firetv import TvStore
from .layout import LayoutStore
from .news import async_register as async_register_news
from .pages import FreshPageView
from .hand_model import async_setup_hand_model
from .license import ProManager
from .photos import PhotoView
from .satellite import Satellites
from .satellite import async_register as async_register_satellites
from .send import async_setup as async_setup_send
from .hello import async_register as async_register_hello
from .throw import async_register as async_register_throw
from .follow import async_setup as async_setup_follow
from .rooms import async_setup as async_setup_rooms
from .spots import SpotStore
from .voice import async_register as async_register_voice
from .stopping import async_register_stopping
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
# Wall tablets as Assist satellites (Pro): one device per tablet, with its satellite and its two voice settings.
# Guest mode (free): one switch for the whole house (switch.py).
PLATFORMS = [Platform.ASSIST_SATELLITE, Platform.BINARY_SENSOR, Platform.MEDIA_PLAYER, Platform.SELECT, Platform.SWITCH]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the files and the websocket commands once, when Home Assistant starts."""
    hass.data[DOMAIN] = LayoutStore(hass)
    await hass.data[DOMAIN].async_load()
    hass.data[DRAFT] = DraftStore(hass)
    await hass.data[DRAFT].async_load()
    hass.data[PRO] = ProManager(hass)
    await hass.data[PRO].async_load()
    hass.data[PRO].async_start()
    async_register_commands(hass)
    # Restarts and updates: the screens hear that Home Assistant stops, and show a calm picture until it is back.
    async_register_stopping(hass)
    # Auto-update (off until turned on in the studio): a quiet look once a night.
    hass.data[AUTO] = AutoUpdater(hass)
    await hass.data[AUTO].async_load()
    hass.data[AUTO].async_start()
    # Build a screen by talking: the studio's tools for Home Assistant's AI assistants.
    async_register_assistant(hass)
    # Screensaver photos: small copies for the tablets, and where the clock may go on each (off faces).
    hass.data[SPOTS] = SpotStore(hass)
    await hass.data[SPOTS].async_load()
    hass.data[PHOTO_VIEW] = PhotoView()
    hass.http.register_view(hass.data[PHOTO_VIEW])
    # Home on TV: the Fire TVs set up from the studio.
    hass.data[TVS] = TvStore(hass)
    await hass.data[TVS].async_load()
    # News cards: feeds read here, with small copies of their pictures.
    async_register_news(hass)
    # Talk to the wall: Assist sentences that name a wall screen ("show the front door on the kitchen tablet").
    async_register_voice(hass)
    hass.data[SATELLITES] = Satellites(hass)
    await hass.data[SATELLITES].async_load()
    hass.data[SATELLITES].async_start()
    async_register_satellites(hass)
    # Send to screen (Pro): the Share button on an iPhone or a Mac starts a YouTube video on a wall screen or TV.
    await async_setup_send(hass)
    # Morning hello (Pro): who was greeted today, across all tablets.
    async_register_hello(hass)

    await async_setup_alarms(hass)
    await async_setup_reminders(hass)
    # On for a while: a heater or a pump on for some minutes, turned off here (free).
    await async_setup_timed(hass)
    await async_setup_phones(hass)
    # Guest phone link (free): the guest's phone as a small, safe remote while guest mode is on.
    await async_setup_guest_link(hass)
    await async_setup_intercom(hass)
    # Hand gestures (Pro): a camera next to a TV, the hand tracker on the Mac, the dashboard of that place answers.
    await async_setup_gestures(hass)
    await async_setup_nudges(hass)
    await async_setup_safety(hass)
    # Notices (Pro): the home talks first, one calm line on the screen nearest the person.
    await async_setup_notices(hass)
    # Care (family homes): a calm view of a family member's home, with gentle alerts, and a heart both ways.
    await async_setup_care(hass)
    await async_setup_family_window(hass)
    # Eufy live: one owner for each Eufy camera's video, shared by every screen, and an early wake on a ring.
    await async_setup_eufy_live(hass)
    # Talk at the door (Pro): a tablet's voice comes out of the Eufy doorbell, through the Eufy add-on.
    await async_setup_door_talk(hass)
    # Home replay (Pro): a picture of each ring and parcel of the day, for the wall's short film of the day.
    await async_setup_replay(hass)
    # Energy without a meter (free): each device's use guessed from its on and off times, and one tip a week.
    await async_setup_energy(hass)
    # Drive time (free): how long the drive to the next calendar place, or to work, takes now with traffic (Waze).
    await async_setup_drive(hass)
    # Habits: the home learns the family's days and offers ready automations, one tap to accept (one free, Pro for all).
    await async_setup_habits(hass)
    # Doorstep watch: our own package monitor, for cameras with an area marked in the studio. It starts once the
    # cameras are up, and looks only after a person leaves.
    hass.data[DOORSTEP] = DoorstepWatch(hass)

    async def start_doorstep(_hass: HomeAssistant) -> None:
        stop = hass.data[DOORSTEP].async_start()
        hass.bus.async_listen_once("homeassistant_stop", lambda _e: stop())

    async_at_started(hass, start_doorstep)
    # Throw it to the TV (Pro): a card flicked up on a tablet opens on a TV.
    async_register_throw(hass)
    # Room to room (Pro): what you watch or hear comes with you to the next room.
    async_setup_follow(hass)
    # Room presence: the tablets tell which rooms have someone in them, even someone who sits still.
    async_setup_rooms(hass)
    # A tablet as a TV's ears (Pro): the tablet next to the TV hears, the TV listens, shows and answers.
    async_register_ears(hass)
    # The hand model for the wall tablets' cameras (Pro), fetched once when a tablet first asks.
    async_setup_hand_model(hass)
    # The pages first, with "no-cache", so a browser never keeps an old studio.html after an update.
    hass.http.register_view(FreshPageView())
    # Everything else: the asset files have a hash in their names.
    await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(WWW), False)])
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Show the studio in the sidebar, for admins only."""
    frontend.async_register_built_in_panel(
        hass,
        # A custom panel, not an iframe panel: HA then draws no title bar, and the studio's own
        # top bar (with a ☰ button for HA's sidebar on narrow screens) uses the whole height.
        component_name="custom",
        sidebar_title="Dashboard Studio",
        sidebar_icon="mdi:palette-outline",
        frontend_url_path=PANEL,
        config={
            "_panel_custom": {
                "name": "hadash-studio-panel",
                "module_url": f"{URL_BASE}/panel.js?v={VERSION}",
                "embed_iframe": False,
                "trust_external": False,
            }
        },
        require_admin=True,
    )
    # A Home Assistant without Assist (or a change in it) must never stop the dashboards: only the satellites go.
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Wall tablets as Assist satellites: could not start", exc_info=True)
    # Tablets that connected while Home Assistant was starting become satellites now.
    hass.data[SATELLITES].set_entry(entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the sidebar item. The files and the layout stay until Home Assistant restarts."""
    frontend.async_remove_panel(hass, PANEL)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[SATELLITES].set_entry(None)
    return unloaded


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry) -> bool:
    """A wall tablet may be deleted in Home Assistant; it comes back the next time it is a satellite."""
    for domain, tablet in device.identifiers:
        if domain == DOMAIN:
            hass.data[SATELLITES].forget(tablet)
    return True
