"""Each Fire TV set up with "Add a Fire TV" as a Home Assistant media player.

Without Home Assistant's own Fire TV integration, a Fire TV only shows through Alexa, which says "idle" all day.
This player tells what is really on (the app in front, and the title it plays), and takes play, pause, volume
and power, through the same adb key as the TV remote tile. So the TV shows in Home Assistant's Media page, in
automations ("when the kitchen TV plays") and to voice.

It reads the TV every 10 seconds; a TV remote tile that is open reads it faster and the player hears that too.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, TVS
from .tvremote import FireTvLink, _link, _refresh_soon

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)
TV_ADDED = f"{DOMAIN}_tv_added"

# The names people know, for the apps most used on a Fire TV. Others show their package name's last word.
APP_NAMES = {
    "com.netflix.ninja": "Netflix",
    "com.amazon.firetv.youtube": "YouTube",
    "com.google.android.youtube.tv": "YouTube",
    "com.amazon.avod": "Prime Video",
    "com.amazon.avod.thirdpartyclient": "Prime Video",
    "com.disney.disneyplus": "Disney+",
    "com.spotify.tv.android": "Spotify",
    "com.plexapp.android": "Plex",
    "com.wbd.stream": "Max",
    "ca.bellmedia.cravetv": "Crave",
    "com.hulu.plus": "Hulu",
    "tv.twitch.android.app": "Twitch",
    "org.xbmc.kodi": "Kodi",
    "com.cbs.ott": "Paramount+",
    "com.apple.atve.amazon.appletv": "Apple TV",
    "com.lukaps.wall": "Dashboard Studio",
}
# The home screen is no app: the player then says only that the TV is on.
HOME_SCREENS = {"com.amazon.tv.launcher", "com.amazon.firebat"}


def app_name(package: str) -> str:
    if not package or package in HOME_SCREENS:
        return ""
    return APP_NAMES.get(package) or package.rsplit(".", 1)[-1].replace("_", " ").title()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    saved = hass.data.get(TVS)
    added: set[str] = set()

    @callback
    def add(hosts: list[str]) -> None:
        new = [h for h in hosts if h not in added and _link(hass, h) is not None]
        added.update(new)
        if new:
            async_add_entities([FireTvPlayer(hass, h) for h in new])

    add(list(saved.tvs) if saved else [])
    # A TV set up later in the studio joins at once, without a restart.
    @callback
    def added_later(host: str) -> None:
        add([host])

    entry.async_on_unload(async_dispatcher_connect(hass, TV_ADDED, added_later))


class FireTvPlayer(MediaPlayerEntity):
    """media_player.<the TV's name>, for one Fire TV set up from the studio."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, hass: HomeAssistant, host: str) -> None:
        self.hass = hass
        self._host = host
        info = hass.data[TVS].tvs.get(host, {})
        self._attr_unique_id = f"firetv_{host}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"firetv_{host}")},
            name=info.get("name") or "Fire TV",
            manufacturer="Amazon",
            model=info.get("model") or "Fire TV",
            configuration_url=None,
        )
        self._now: dict[str, Any] = {"reachable": None}

    @property
    def _link(self) -> FireTvLink | None:
        return _link(self.hass, self._host)

    async def async_added_to_hass(self) -> None:
        link = self._link
        if link is not None:
            # What an open TV remote tile reads every few seconds shows here at once, too.
            self.async_on_remove(link.observe(self._heard))

    @callback
    def _heard(self, now: dict[str, Any]) -> None:
        if now != self._now:
            self._now = now
            self.async_write_ha_state()

    async def async_update(self) -> None:
        link = self._link
        if link is None:
            self._now = {"reachable": False}
            return
        self._now = await link.read_now()
        link.share(self._now)

    @property
    def available(self) -> bool:
        return self._now.get("reachable") is not False

    @property
    def state(self) -> MediaPlayerState | None:
        now = self._now
        if not now.get("reachable") or not now.get("awake"):
            return MediaPlayerState.OFF
        if now.get("playing"):
            return MediaPlayerState.PLAYING
        if now.get("paused"):
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE if (now.get("app") or "") in ("", *HOME_SCREENS) else MediaPlayerState.ON

    @property
    def app_id(self) -> str | None:
        return self._now.get("app") or None

    @property
    def app_name(self) -> str | None:
        return app_name(self._now.get("app") or "") or None

    @property
    def media_title(self) -> str | None:
        return self._now.get("title") or None

    @property
    def media_artist(self) -> str | None:
        return self._now.get("sub") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # The dashboard's TV remote finds this TV's own link by it.
        return {"firetv_host": self._host}

    async def _key(self, key: str) -> None:
        link = self._link
        if link is None:
            return
        await link.key(key)
        self.hass.async_create_background_task(_refresh_soon(link), f"{DOMAIN} fire tv refresh")

    async def async_media_play(self) -> None:
        await self._key("play")

    async def async_media_pause(self) -> None:
        await self._key("pause")

    async def async_turn_on(self) -> None:
        await self._key("wake")

    async def async_turn_off(self) -> None:
        await self._key("sleep")

    async def async_volume_up(self) -> None:
        await self._key("volume_up")

    async def async_volume_down(self) -> None:
        await self._key("volume_down")

    async def async_mute_volume(self, mute: bool) -> None:
        # The TV has one mute key that turns it on and off; it does not tell which it is.
        await self._key("mute")
