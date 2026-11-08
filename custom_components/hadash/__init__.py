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

from .assistant import async_register as async_register_assistant
from .autoupdate import AutoUpdater
from .const import AUTO, DOMAIN, DRAFT, PANEL, PHOTO_VIEW, PRO, SATELLITES, SPOTS, TVS, URL_BASE, VERSION, WWW
from .draft import DraftStore
from .ears import async_register as async_register_ears
from .firetv import TvStore
from .layout import LayoutStore
from .news import async_register as async_register_news
from .pages import FreshPageView
from .license import ProManager
from .photos import PhotoView
from .satellite import Satellites
from .satellite import async_register as async_register_satellites
from .send import async_setup as async_setup_send
from .hello import async_register as async_register_hello
from .throw import async_register as async_register_throw
from .spots import SpotStore
from .voice import async_register as async_register_voice
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
# Wall tablets as Assist satellites (Pro): one device per tablet, with its satellite and its two voice settings.
# Guest mode (free): one switch for the whole house (switch.py).
PLATFORMS = [Platform.ASSIST_SATELLITE, Platform.SELECT, Platform.SWITCH]


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
    # Throw it to the TV (Pro): a card flicked up on a tablet opens on a TV.
    async_register_throw(hass)
    # A tablet as a TV's ears (Pro): the tablet next to the TV hears, the TV listens, shows and answers.
    async_register_ears(hass)
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
