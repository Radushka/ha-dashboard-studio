"""The TV remote for Fire TVs that were set up with "Add a Fire TV".

Home Assistant's own Fire TV integration (Android Debug Bridge) is often not set up, and Alexa only sees a Fire TV
from far away. Home Assistant already holds an adb key that each TV set up from the studio accepted, so the TV tile
works the TV through that: the remote's keys, play and pause, volume, and what is on (the app in front and the
title it plays). Nothing to set up.

One connection per TV is kept open while anyone uses its tile, and closed after a quiet minute. What is on is read
every few seconds, only while a tile listens, and sent to every tile that listens.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, TVS
from .tvsetup import ADB_PORT

_LOGGER = logging.getLogger(__name__)

# Android key codes. "volume_*" go to the TV's own sound, and through HDMI-CEC to the television or sound bar.
KEYS: dict[str, int] = {
    "up": 19, "down": 20, "left": 21, "right": 22, "ok": 23, "back": 4, "home": 3,
    "play_pause": 85, "play": 126, "pause": 127, "forward": 90, "rewind": 89,
    "volume_up": 24, "volume_down": 25, "mute": 164, "wake": 224, "sleep": 223,
}
POLL_SECONDS = 4.0
IDLE_CLOSE_SECONDS = 60.0

# PlaybackState numbers in "dumpsys media_session".
PLAYING, PAUSED, BUFFERING = 3, 2, 6


def parse_focus(window: str) -> str:
    """The app in front, from "dumpsys window": the package of mCurrentFocus (or mFocusedApp)."""
    for pattern in (r"mCurrentFocus=Window\{[^}]*\s([\w.]+)/", r"mFocusedApp=.*?\s([\w.]+)/"):
        found = re.search(pattern, window)
        if found:
            return found.group(1)
    return ""


def parse_sessions(dump: str) -> dict[str, Any] | None:
    """The media session that plays (or is paused) from "dumpsys media_session": app, playing, title, sub.

    Each session lists "package=...", "active=true|false", then "state=PlaybackState {state=3, ...}" and
    "metadata: size=N, description=Title, Subtitle, ...". A new "package=" line starts the next session. Only an
    active session counts: Fire TV keeps old ones that still say "playing" (YouTube after it was closed). The first
    playing one wins, else the first paused one."""
    sessions: list[dict[str, Any]] = []
    for line in dump.splitlines():
        line = line.strip()
        if line.startswith("package="):
            sessions.append({"app": line[len("package="):].strip(), "active": False, "code": None, "title": "", "sub": ""})
        elif not sessions:
            continue
        elif line.startswith("active="):
            sessions[-1]["active"] = line == "active=true"
        elif line.startswith("state=PlaybackState"):
            code = re.search(r"\{state=(\d+)", line)
            sessions[-1]["code"] = int(code.group(1)) if code else None
        elif line.startswith("metadata:") and "description=" in line:
            parts = [p.strip() for p in line.split("description=", 1)[1].split(",")]
            words = [p for p in parts if p and p != "null"]
            # The title can hold commas itself; the last word is the channel or artist when there are several.
            if len(words) >= 2:
                sessions[-1]["title"], sessions[-1]["sub"] = ", ".join(words[:-1]), words[-1]
            elif words:
                sessions[-1]["title"] = words[0]
    best: dict[str, Any] | None = None
    for s in sessions:
        if not s["active"] or s["code"] not in (PLAYING, PAUSED, BUFFERING):
            continue
        found = {"app": s["app"], "playing": s["code"] != PAUSED, "title": s["title"], "sub": s["sub"]}
        if found["playing"]:
            return found
        best = best or found
    return best


class FireTvLink:
    """One Fire TV: an open adb connection (opened on first use), its listeners and what is on."""

    def __init__(self, hass: HomeAssistant, host: str, signer_factory: Callable[[], Any], device_factory: Callable[[str], Any] | None = None) -> None:
        self.hass = hass
        self.host = host
        self._signer_factory = signer_factory
        self._device_factory = device_factory
        self._device: Any = None
        self._lock = asyncio.Lock()
        self._listeners: set[Callable[[dict[str, Any]], None]] = set()
        self._poll: asyncio.Task[None] | None = None
        self._used = time.monotonic()
        self.now: dict[str, Any] = {"reachable": None}

    async def _connect(self) -> Any:
        if self._device is not None:
            return self._device
        from adb_shell.adb_device_async import AdbDeviceTcpAsync

        signer = await self._signer_factory()
        device = self._device_factory(self.host) if self._device_factory else AdbDeviceTcpAsync(self.host, ADB_PORT, default_transport_timeout_s=4.0)
        # Nobody stands at the TV to answer "Allow USB debugging?": a TV that asks gives up at once.
        await device.connect(rsa_keys=[signer], auth_timeout_s=3.0, auth_callback=lambda _d: None)
        self._device = device
        return device

    async def shell(self, command: str, timeout: float = 5.0) -> str:
        """Runs one command on the TV. A broken connection is opened again once."""
        self._used = time.monotonic()
        async with self._lock:
            for attempt in (1, 2):
                try:
                    device = await self._connect()
                    return str(await device.shell(command, read_timeout_s=timeout, timeout_s=timeout) or "")
                except Exception:  # noqa: BLE001 - adb fails in many ways; try a fresh connection once
                    await self._drop()
                    if attempt == 2:
                        raise
        return ""

    async def _drop(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            try:
                await device.close()
            except Exception:  # noqa: BLE001
                pass

    async def key(self, name: str, times: int = 1) -> None:
        # Starting "input" on the TV takes most of a second, so a swipe of several steps sends them in one go.
        await self.shell("input keyevent " + " ".join([str(KEYS[name])] * max(1, min(8, times))))

    async def read_now(self) -> dict[str, Any]:
        """What is on: the app in front, and the session that plays (with its title), or the TV asleep."""
        try:
            out = await self.shell("dumpsys power | grep -m1 mWakefulness=; dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'; dumpsys media_session", 6.0)
        except Exception:  # noqa: BLE001
            return {"reachable": False}
        awake = "mWakefulness=Awake" in out
        session = parse_sessions(out)
        front = parse_focus(out)
        now: dict[str, Any] = {"reachable": True, "awake": awake, "app": front, "playing": False, "paused": False, "title": "", "sub": ""}
        # A paused video behind the home screen is not "on": the home screen is. One that plays is, even behind
        # another app (music from Spotify while the home screen shows).
        if session and awake and (not front or session["app"] == front or session["playing"]):
            now.update(app=session["app"], playing=session["playing"], paused=not session["playing"], title=session["title"], sub=session["sub"])
        return now

    def listen(self, send: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.add(send)
        if self.now.get("reachable") is not None:
            send(self.now)
        if self._poll is None or self._poll.done():
            self._poll = self.hass.async_create_background_task(self._run(), f"{DOMAIN} fire tv {self.host}")

        def stop() -> None:
            self._listeners.discard(send)

        return stop

    async def refresh(self) -> None:
        """Reads at once (after a key), so the tile shows the change without waiting for the next round."""
        self._tell(await self.read_now())

    def _tell(self, now: dict[str, Any]) -> None:
        if now == self.now:
            return
        self.now = now
        for send in list(self._listeners):
            send(now)

    async def _run(self) -> None:
        try:
            while self._listeners or time.monotonic() - self._used < IDLE_CLOSE_SECONDS:
                if self._listeners:
                    self._tell(await self.read_now())
                await asyncio.sleep(POLL_SECONDS if self._listeners else 5.0)
        finally:
            await self._drop()


def _link(hass: HomeAssistant, host: str) -> FireTvLink | None:
    """The link to a Fire TV set up from the studio. Other addresses are refused: a tablet can only reach those."""
    saved = hass.data.get(TVS)
    if saved is None or host not in saved.tvs:
        return None
    links: dict[str, FireTvLink] = hass.data.setdefault(f"{DOMAIN}_tv_links", {})
    if host not in links:
        from .firetv import _signer

        links[host] = FireTvLink(hass, host, lambda: _signer(hass))
    return links[host]


@websocket_api.websocket_command({vol.Required("type"): "hadash/tv/remotes"})
@callback
def ws_tv_remotes(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The Fire TVs a TV tile can work: the ones set up with "Add a Fire TV"."""
    saved = hass.data.get(TVS)
    tvs = [{"host": h, "name": tv.get("name") or h} for h, tv in (saved.tvs.items() if saved else [])]
    connection.send_result(msg["id"], {"tvs": tvs})


@websocket_api.websocket_command({vol.Required("type"): "hadash/tv/watch", vol.Required("host"): str})
@callback
def ws_tv_watch(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """What is on a Fire TV, now and each time it changes, while the tile is on the screen."""
    link = _link(hass, msg["host"])
    if link is None:
        connection.send_error(msg["id"], "unknown_tv", "This TV was not set up in Dashboard Studio")
        return

    @callback
    def send(now: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], now))

    connection.subscriptions[msg["id"]] = link.listen(send)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/tv/key",
        vol.Required("host"): str,
        vol.Required("key"): vol.In(list(KEYS)),
        vol.Optional("times", default=1): vol.All(int, vol.Range(min=1, max=8)),
    }
)
@websocket_api.async_response
async def ws_tv_key(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """One key of the remote. Not admin only: a wall tablet signs in as a plain user."""
    link = _link(hass, msg["host"])
    if link is None:
        connection.send_error(msg["id"], "unknown_tv", "This TV was not set up in Dashboard Studio")
        return
    try:
        await link.key(msg["key"], msg["times"])
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "unreachable", str(err)[:200])
        return
    connection.send_result(msg["id"])
    # Play, pause and the apps change what is on: tell the tiles soon, not at the next round.
    if msg["key"] in ("play_pause", "play", "pause", "ok", "back", "home", "wake", "sleep"):
        hass.async_create_background_task(_refresh_soon(link), f"{DOMAIN} fire tv refresh")


async def _refresh_soon(link: FireTvLink) -> None:
    await asyncio.sleep(0.8)
    try:
        await link.refresh()
    except Exception:  # noqa: BLE001
        pass


async def fire_tv_key(hass: HomeAssistant, host: str, key: str) -> bool:
    """For voice: one key to a Fire TV set up in the studio. False when that TV is not known or does not answer."""
    link = _link(hass, host)
    if link is None or key not in KEYS:
        return False
    try:
        await link.key(key)
    except Exception:  # noqa: BLE001
        return False
    return True


async def fire_tv_now(hass: HomeAssistant, host: str) -> dict[str, Any]:
    link = _link(hass, host)
    return await link.read_now() if link else {"reachable": False}


# ---- Voice: "pause the TV", "back 30 seconds", "louder" ----

# Integrations whose players are TVs or TV boxes (as the tile's list).
TV_PLATFORMS = {"apple_tv", "androidtv", "androidtv_remote", "braviatv", "samsungtv", "roku", "webostv", "philips_js", "vizio", "panasonic_viera", "kodi"}
VOICE_ACTIONS = ["pause", "resume", "skip", "louder", "quieter", "mute", "unmute", "turn_on", "turn_off", "home", "back", "show_remote"]
# The remote's own skip keys, for apps that do not take an exact place.
_SKIP_KEYS = {
    "apple_tv": ("skip_forward", "skip_backward"),
    "androidtv_remote": ("MEDIA_FAST_FORWARD", "MEDIA_REWIND"),
    "braviatv": ("Forward", "Rewind"),
    "samsungtv": ("KEY_FF", "KEY_REWIND"),
    "roku": ("forward", "reverse"),
}
_HOME_BACK = {
    "apple_tv": ("home", "menu"),
    "androidtv_remote": ("HOME", "BACK"),
    "braviatv": ("Home", "Return"),
    "samsungtv": ("KEY_HOME", "KEY_RETURN"),
    "roku": ("home", "back"),
}


def _platform(hass: HomeAssistant, entity_id: str) -> str:
    from homeassistant.helpers import entity_registry as er

    entry = er.async_get(hass).async_get(entity_id)
    return entry.platform if entry else ""


def _remote_of(hass: HomeAssistant, entity_id: str) -> str | None:
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    entry = reg.async_get(entity_id)
    if entry and entry.device_id:
        for other in er.async_entries_for_device(reg, entry.device_id):
            if other.entity_id.startswith("remote."):
                return other.entity_id
    same = "remote." + entity_id.split(".", 1)[1]
    return same if hass.states.get(same) else None


def tv_choices(hass: HomeAssistant, layouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every TV the person can mean: the TV tiles of the screens first (with their sound), then Home Assistant's TVs."""
    found: dict[str, dict[str, Any]] = {}
    saved = hass.data.get(TVS)
    for layout in layouts:
        for w in layout.get("widgets") or []:
            if isinstance(w, dict) and w.get("type") == "tv" and isinstance(w.get("entity"), str) and w["entity"]:
                found.setdefault(w["entity"], {"id": w["entity"], "name": w.get("name"), "sound": w.get("sound"), "tile": True})
    for state in hass.states.async_all("media_player"):
        if state.attributes.get("device_class") == "tv" or _platform(hass, state.entity_id) in TV_PLATFORMS:
            found.setdefault(state.entity_id, {"id": state.entity_id, "name": None, "sound": None, "tile": False})
    for tv in found.values():
        if tv["id"].startswith("firetv:"):
            host = tv["id"][len("firetv:"):]
            tv["name"] = tv["name"] or (saved.tvs.get(host, {}).get("name") if saved else None) or "Fire TV"
        else:
            state = hass.states.get(tv["id"])
            tv["name"] = tv["name"] or (str(state.attributes.get("friendly_name") or tv["id"]) if state else tv["id"])
    return list(found.values())


def pick_tv(
    hass: HomeAssistant, choices: list[dict[str, Any]], here: list[str], name: str | None, near: list[str] | None = None
) -> dict[str, Any] | None:
    """The TV meant: by name; else the TV tile on the screen the person looks at; else the one that plays; else
    the one in the same room as the tablet the person spoke to (near)."""
    if name:
        words = [w for w in re.findall(r"\w+", name.lower()) if w not in ("tv", "the", "television")]
        named = [c for c in choices if words and all(w in c["name"].lower() for w in words)]
        if named:
            return named[0]
    shown = [c for c in choices if c["id"] in here]
    if len(shown) == 1:
        return shown[0]
    pool = shown or choices
    playing = [c for c in pool if not c["id"].startswith("firetv:") and getattr(hass.states.get(c["id"]), "state", "") == "playing"]
    if playing:
        return playing[0]
    close = [c for c in pool if c["id"] in (near or [])]
    if close:
        return close[0]
    return pool[0] if len(pool) == 1 else (shown[0] if shown else None)


def _sound_of(hass: HomeAssistant, tv: dict[str, Any]) -> str | None:
    """The player that takes the TV's volume: the tile's choice, the TV when it reports a volume, else a sound bar
    in the same room that lists the TV as a source (a Sonos Playbar)."""
    if tv.get("sound"):
        return tv["sound"]
    if tv["id"].startswith("firetv:"):
        return None
    state = hass.states.get(tv["id"])
    if state and isinstance(state.attributes.get("volume_level"), (int, float)):
        return tv["id"]
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    reg = er.async_get(hass)
    devices = dr.async_get(hass)

    def area_of(entity_id: str) -> str | None:
        entry = reg.async_get(entity_id)
        if entry is None:
            return None
        if entry.area_id:
            return entry.area_id
        device = devices.async_get(entry.device_id) if entry.device_id else None
        return device.area_id if device else None

    area = area_of(tv["id"])
    if area:
        for other in hass.states.async_all("media_player"):
            sources = [str(s).lower() for s in other.attributes.get("source_list") or []]
            if other.entity_id != tv["id"] and area_of(other.entity_id) == area and isinstance(other.attributes.get("volume_level"), (int, float)) and any(
                s in ("tv", "tv audio", "hdmi", "hdmi arc", "hdmi earc") for s in sources
            ):
                return other.entity_id
    return tv["id"]


async def tv_do(hass: HomeAssistant, tv: dict[str, Any], action: str, seconds: int | None = None) -> str:
    """Does one thing on the TV. Returns a short word for the answer; raises ValueError when it cannot."""
    tid: str = tv["id"]
    call = hass.services.async_call
    if tid.startswith("firetv:"):
        host = tid[len("firetv:"):]
        sound = _sound_of(hass, tv)
        if action in ("louder", "quieter", "mute", "unmute") and sound:
            return await _volume(hass, sound, action)
        key = {
            "pause": "pause", "resume": "play", "louder": "volume_up", "quieter": "volume_down", "mute": "mute",
            "unmute": "mute", "turn_on": "wake", "turn_off": "sleep", "home": "home", "back": "back",
        }.get(action)
        if action == "skip":
            key = "rewind" if (seconds or 30) < 0 else "forward"
        times = 3 if action in ("louder", "quieter") else 1
        link = _link(hass, host)
        if link is None or key is None:
            raise ValueError("That TV cannot do this.")
        try:
            await link.key(key, times)
        except Exception as err:  # noqa: BLE001
            raise ValueError("The TV did not answer.") from err
        return "Done."
    state = hass.states.get(tid)
    if state is None or state.state == "unavailable":
        raise ValueError("Home Assistant cannot reach that TV now.")
    platform = _platform(hass, tid)
    if platform == "apple_tv":
        remote = _remote_of(hass, tid)
        if remote and getattr(hass.states.get(remote), "state", "") != "on":
            raise ValueError("Home Assistant is not connected to that Apple TV now.")
    data = {"entity_id": tid}
    if action == "pause":
        await call("media_player", "media_pause", data, blocking=True)
    elif action == "resume":
        await call("media_player", "media_play", data, blocking=True)
    elif action in ("turn_on", "turn_off"):
        await call("media_player", action, data, blocking=True)
    elif action in ("louder", "quieter", "mute", "unmute"):
        return await _volume(hass, _sound_of(hass, tv) or tid, action)
    elif action == "skip":
        step = seconds or 30
        position = state.attributes.get("media_position")
        length = state.attributes.get("media_duration")
        if isinstance(position, (int, float)) and isinstance(length, (int, float)) and length > 0 and state.attributes.get("supported_features", 0) & 2:
            updated = state.attributes.get("media_position_updated_at")
            if state.state == "playing" and updated is not None:
                from homeassistant.util import dt as dt_util

                position += (dt_util.utcnow() - updated).total_seconds()
            await call("media_player", "media_seek", {**data, "seek_position": max(0, min(length, position + step))}, blocking=True)
        else:
            keys = _SKIP_KEYS.get(platform)
            remote = _remote_of(hass, tid)
            if not keys or not remote:
                raise ValueError("That TV cannot skip from here.")
            await call("remote", "send_command", {"entity_id": remote, "command": keys[0] if step > 0 else keys[1]}, blocking=True)
    elif action in ("home", "back"):
        keys = _HOME_BACK.get(platform)
        remote = _remote_of(hass, tid)
        if not keys or not remote:
            raise ValueError("That TV takes no menu keys from here.")
        await call("remote", "send_command", {"entity_id": remote, "command": keys[0] if action == "home" else keys[1]}, blocking=True)
    return "Done."


async def _volume(hass: HomeAssistant, player: str, action: str) -> str:
    state = hass.states.get(player)
    data = {"entity_id": player}
    if action in ("mute", "unmute"):
        await hass.services.async_call("media_player", "volume_mute", {**data, "is_volume_muted": action == "mute"}, blocking=True)
        return "Done."
    level = state.attributes.get("volume_level") if state else None
    if isinstance(level, (int, float)) and state is not None and state.attributes.get("supported_features", 0) & 4:
        step = 0.06 if action == "louder" else -0.06
        await hass.services.async_call("media_player", "volume_set", {**data, "volume_level": round(max(0.0, min(1.0, level + step)), 2)}, blocking=True)
    else:
        for _ in range(3):
            await hass.services.async_call("media_player", "volume_up" if action == "louder" else "volume_down", data, blocking=True)
    return "Done."
