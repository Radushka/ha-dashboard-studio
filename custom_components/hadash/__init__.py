"""Dashboard Studio: serves the wall-tablet dashboard and its studio, and keeps the published layout.

The dashboard files come with this integration, so an update through HACS updates every tablet.
The layout lives in Home Assistant's own storage, so it is part of every HA backup.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PANEL, PRO, URL_BASE, VERSION
from .layout import LayoutStore
from .license import ProManager
from .photos import PhotoView
from .websocket import async_register_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
WWW = Path(__file__).parent / "www"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the files and the websocket commands once, when Home Assistant starts."""
    hass.data[DOMAIN] = LayoutStore(hass)
    await hass.data[DOMAIN].async_load()
    hass.data[PRO] = ProManager(hass)
    await hass.data[PRO].async_load()
    hass.data[PRO].async_start()
    async_register_commands(hass)
    hass.http.register_view(PhotoView())
    # No cache headers: index.html must always be fresh. The asset files have a hash in their names.
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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the sidebar item. The files and the layout stay until Home Assistant restarts."""
    frontend.async_remove_panel(hass, PANEL)
    return True
