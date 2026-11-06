"""Home on TV: set up a Fire TV from the studio ("Add a Fire TV").

Fire TV gives apps no settings screen for "show over other apps", and the wall app needs it to come up over
Amazon's screensaver. Only adb can turn it on. Home Assistant is already a computer on the same network, so it
does the adb part: the person turns on ADB debugging on the TV and presses OK once on "Allow USB debugging?".

Then Home Assistant installs the wall app that it serves itself (www/wall), allows it over other apps, keeps it
out of the TV's battery saving, sets the screensaver start time, and opens the dashboard on the chosen screen.

The adb key is Home Assistant's own, kept in .storage (never shown), so the TV asks only the first time.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from . import tvsetup
from .const import DOMAIN, TVS, TVS_STORAGE_KEY, WWW
from .tvsetup import ADB_PORT, SAVER_MINUTES, SetupError

_LOGGER = logging.getLogger(__name__)

KEY_FILE = "hadash_adbkey"

# ---- What Home Assistant serves ----


def wall_app(www: Path = WWW) -> dict[str, Any] | None:
    """The wall app Home Assistant serves: {version, code, path}, or None when the files are missing."""
    try:
        feed = json.loads((www / "wall" / "wall.json").read_text())
        apk = www / "wall" / str(feed["apk"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if "/" in str(feed["apk"]) or not apk.is_file():
        return None
    return {"version": str(feed.get("version", "")), "code": int(feed.get("code", 0)), "path": apk}


async def tv_address(hass: HomeAssistant, screen: str) -> str:
    """The dashboard address for the TV: an IP address, because Fire TV cannot look up ".local" names."""
    api = hass.config.api
    url = ""
    internal = hass.config.internal_url
    if internal:
        host = re.sub(r"^\w+://", "", internal).split("/")[0].rsplit(":", 1)[0].strip("[]")
        if _is_ip(host):
            url = internal.rstrip("/")
    if not url:
        scheme = "https" if api is not None and api.use_ssl else "http"
        port = api.port if api is not None else 8123
        ip = api.local_ip if api is not None and _is_ip(api.local_ip) and api.local_ip != "0.0.0.0" else ""
        if not ip:
            try:
                from homeassistant.components import network

                ip = await network.async_get_source_ip(hass)
            except Exception:  # noqa: BLE001 - fall back to the name; the person can still type the address
                ip = "homeassistant.local"
        url = f"{scheme}://{ip}:{port}"
    url += "/hadash/index.html"
    if screen:
        url += "?screen=" + re.sub(r"[^A-Za-z0-9_-]", "", screen)
    return url


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# ---- Finding TVs on the network ----


async def _port_open(host: str, port: int = ADB_PORT, timeout: float = 0.6) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _host_name(hass: HomeAssistant, host: str) -> str:
    """The name the router gives the device, such as "amazon-1a2b3c" (empty when there is none)."""
    try:
        name, _, _ = await asyncio.wait_for(hass.async_add_executor_job(socket.gethostbyaddr, host), 1.5)
    except (OSError, asyncio.TimeoutError):
        return ""
    return name.split(".")[0]


def _known_tvs(hass: HomeAssistant) -> dict[str, str]:
    """TVs Home Assistant already knows from its Android TV integrations: {address: title}."""
    found: dict[str, str] = {}
    for domain in ("androidtv", "androidtv_remote"):
        for entry in hass.config_entries.async_entries(domain):
            host = entry.data.get("host")
            if isinstance(host, str) and host:
                found[host] = entry.title
    return found


async def _local_networks(hass: HomeAssistant) -> list[ipaddress.IPv4Network]:
    """The home networks to look in: each enabled IPv4 adapter, at most a /24 each, so a look takes seconds."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        from homeassistant.components import network

        adapters = await network.async_get_adapters(hass)
    except Exception:  # noqa: BLE001 - without the network helper, look next to Home Assistant's own address
        adapters = []
    for adapter in adapters:
        if not adapter.get("enabled"):
            continue
        for ip in adapter.get("ipv4", []):
            try:
                net = ipaddress.ip_network(f"{ip['address']}/{max(int(ip['network_prefix']), 24)}", strict=False)
            except (KeyError, ValueError):
                continue
            if net.is_private and net not in nets:
                nets.append(net)
    if not nets and hass.config.api is not None and _is_ip(hass.config.api.local_ip):
        nets.append(ipaddress.ip_network(f"{hass.config.api.local_ip}/24", strict=False))
    return nets


async def find_tvs(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Devices on the home network with ADB debugging on (port 5555 open), plus the TVs Home Assistant knows."""
    known = _known_tvs(hass)
    own = {hass.config.api.local_ip} if hass.config.api is not None else set()
    hosts = [str(h) for net in await _local_networks(hass) for h in net.hosts() if str(h) not in own]
    for host in known:
        if host not in hosts:
            hosts.append(host)
    gate = asyncio.Semaphore(64)

    async def look(host: str) -> str | None:
        async with gate:
            return host if await _port_open(host) else None

    open_hosts = [h for h in await asyncio.gather(*(look(h) for h in hosts)) if h]
    saved: TvStore = hass.data[TVS]
    found = []
    for host in open_hosts:
        name = saved.name(host) or known.get(host) or await _host_name(hass, host)
        found.append({"host": host, "name": name, "added": saved.name(host) is not None})
    # Then the ones Home Assistant knows but that do not answer: their ADB debugging is off.
    for host, title in known.items():
        if host not in open_hosts:
            found.append({"host": host, "name": title, "added": saved.name(host) is not None, "closed": True})
    return found


# ---- The TVs that were set up ----


class TvStore:
    """The TVs set up from the studio: {host: {name, model, screen, version}}, so the studio can list them."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, 1, TVS_STORAGE_KEY)
        self.tvs: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self.tvs = dict((data or {}).get("tvs", {}))

    def name(self, host: str) -> str | None:
        tv = self.tvs.get(host)
        return tv.get("name", "") if tv is not None else None

    async def async_save_tv(self, host: str, info: dict[str, Any]) -> None:
        self.tvs[host] = info
        await self._store.async_save({"tvs": self.tvs})

    async def async_remove(self, host: str) -> None:
        if self.tvs.pop(host, None) is not None:
            await self._store.async_save({"tvs": self.tvs})


# ---- The setup itself ----


async def _signer(hass: HomeAssistant) -> Any:
    from adb_shell.auth.keygen import keygen
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner

    path = Path(hass.config.path(".storage", KEY_FILE))

    def load() -> Any:
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            keygen(str(path))
        return PythonRSASigner.FromRSAKeyPath(str(path))

    return await hass.async_add_executor_job(load)


async def run_setup(
    hass: HomeAssistant,
    host: str,
    screen: str,
    saver_minutes: int,
    replace: bool,
    report: Callable[[dict[str, Any]], None],
    device_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Sets up one TV, telling the studio about each step. Returns what the studio shows at the end."""
    app = wall_app()
    if app is None:
        raise SetupError("no_app")
    url = await tv_address(hass, screen)
    signer = await _signer(hass)
    result = await tvsetup.set_up(host, signer, app, url, saver_minutes, replace, report, device_factory)
    saved: TvStore = hass.data[TVS]
    await saved.async_save_tv(
        host, {"name": result["name"], "model": result["model"], "screen": screen, "version": app["version"], "tv": result["tv"]}
    )
    return result


# ---- Websocket commands ----


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/tv/find"})
@websocket_api.async_response
async def ws_tv_find(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Fire TVs on the home network with ADB debugging on, and the TVs set up before."""
    found = await find_tvs(hass)
    saved: TvStore = hass.data[TVS]
    app = wall_app()
    connection.send_result(
        msg["id"],
        {
            "found": found,
            "added": [{"host": h, **tv} for h, tv in saved.tvs.items()],
            "app": {"version": app["version"]} if app else None,
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/tv/setup",
        vol.Required("host"): vol.All(str, vol.Length(min=1, max=255)),
        vol.Optional("screen", default=""): str,
        vol.Optional("saver_minutes", default=5): vol.In(SAVER_MINUTES),
        vol.Optional("replace", default=False): bool,
    }
)
@callback
def ws_tv_setup(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Sets up a TV and sends each step as it happens. Closing the dialog stops it."""
    host = msg["host"].strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
        connection.send_error(msg["id"], "invalid_host", "That is not a network address.")
        return

    @callback
    def report(data: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], data))

    async def go() -> None:
        try:
            result = await run_setup(hass, host, msg["screen"], msg["saver_minutes"], msg["replace"], report)
            report({"done": True, **result})
        except SetupError as err:
            report({"error": err.code, "detail": err.detail})
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the studio shows a plain message instead of hanging
            _LOGGER.warning("Fire TV setup for %s failed: %s", host, err)
            report({"error": "failed", "detail": str(err)[:300]})

    # The answer first: the task starts at once, and its first step must not arrive before it.
    connection.send_result(msg["id"])
    task = hass.async_create_background_task(go(), f"{DOMAIN} tv setup {host}")
    connection.subscriptions[msg["id"]] = task.cancel


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/tv/forget", vol.Required("host"): str})
@websocket_api.async_response
async def ws_tv_forget(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Takes a TV off the studio's list. The app stays on the TV."""
    saved: TvStore = hass.data[TVS]
    await saved.async_remove(msg["host"])
    connection.send_result(msg["id"])
