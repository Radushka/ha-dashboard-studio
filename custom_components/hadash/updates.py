"""Updates from the studio: find the update entity for Dashboard Studio, install it, and restart only when needed.

The update entity comes from HACS before Home Assistant 2026.11, and from the built-in Marketplace (domain
"marketplace") from 2026.11 on. The Marketplace keeps the entity id, and makes its restart notices the same way.

The screens and the studio are served straight from this folder, so new files reach every tablet at once,
with no restart. Only a change in the Python code needs Home Assistant to restart. The code that runs now
is fingerprinted when it is imported; after an update, the files on disk are fingerprinted again.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, VERSION

HERE = Path(__file__).parent

# Where our updates come from: HACS before Home Assistant 2026.11, the built-in Marketplace after.
STORES = ("marketplace", "hacs")


def code_fingerprint(folder: Path = HERE) -> str:
    """A hash of everything that Home Assistant loads only at start: the Python files and the manifest's needs."""
    h = hashlib.sha256()
    for f in sorted(folder.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    try:
        manifest = json.loads((folder / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    # The version is left out on purpose: a new version number alone needs no restart.
    for key in ("requirements", "dependencies", "after_dependencies"):
        h.update(json.dumps(manifest.get(key), sort_keys=True).encode())
    return h.hexdigest()


def _disk_state(folder: Path = HERE) -> tuple[str, str]:
    """The fingerprint and version of the files on disk now."""
    try:
        version = json.loads((folder / "manifest.json").read_text()).get("version", "")
    except (OSError, ValueError):
        version = ""
    return code_fingerprint(folder), version


RUNNING = code_fingerprint()


def _is_ours(state: State) -> bool:
    """The store names its entity after us and shows our brand icon; either one is enough.

    HACS uses brands.home-assistant.io/_/hadash/icon.png, the Marketplace /api/brands/integration/hadash/icon.png.
    """
    picture = str(state.attributes.get("entity_picture") or "")
    if f"/_/{DOMAIN}/" in picture or f"/{DOMAIN}/icon" in picture:
        return True
    name = str(state.attributes.get("friendly_name") or state.attributes.get("title") or "")
    return name.lower().startswith("dashboard studio")


def find_update_entity(hass: HomeAssistant) -> str | None:
    """The update entity that HACS or the Marketplace made for Dashboard Studio, or None (installed by hand)."""
    registry = er.async_get(hass)
    found: list[tuple[int, str]] = []
    for state in hass.states.async_all("update"):
        if not _is_ours(state):
            continue
        entry = registry.async_get(state.entity_id)
        # A store's entity first; another one with the same name only when there is nothing better.
        found.append((0 if entry and entry.platform in STORES else 1, state.entity_id))
    return min(found)[1] if found else None


def _clear_store_restart_notice(hass: HomeAssistant) -> None:
    """HACS and the Marketplace ask for a restart after every integration update. When ours needs none,
    take that notice away. Both name it restart_required_<repository id>_<version>, with our domain on it."""
    registry = ir.async_get(hass)
    for (domain, issue_id), issue in list(registry.issues.items()):
        if domain in STORES and issue_id.startswith("restart_required_") and issue.issue_domain == DOMAIN:
            ir.async_delete_issue(hass, domain, issue_id)


async def async_status(hass: HomeAssistant) -> dict[str, Any]:
    fingerprint, version = await hass.async_add_executor_job(_disk_state)
    restart_needed = fingerprint != RUNNING
    if not restart_needed:
        _clear_store_restart_notice(hass)
    return {
        "entity_id": find_update_entity(hass),
        "running": VERSION,
        "files": version,
        "restart_needed": restart_needed,
    }


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/update"})
@websocket_api.async_response
async def ws_update(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Which HA entity carries our updates, and whether the files on disk need a restart."""
    connection.send_result(msg["id"], await async_status(hass))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/update/install"})
@websocket_api.async_response
async def ws_install(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Installs the newest version through the Marketplace (or HACS), then tells whether a restart is needed."""
    entity_id = find_update_entity(hass)
    if entity_id is None:
        connection.send_error(msg["id"], "not_found", "The Marketplace (or HACS) does not offer an update for Dashboard Studio.")
        return
    try:
        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True, context=connection.context(msg)
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "install_failed", str(err))
        return
    connection.send_result(msg["id"], await async_status(hass))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/update/restart"})
@websocket_api.async_response
async def ws_restart(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Restarts Home Assistant. The answer goes out first, because the connection closes soon after."""
    connection.send_result(msg["id"], {})
    await hass.services.async_call("homeassistant", "restart", {}, blocking=False, context=connection.context(msg))
