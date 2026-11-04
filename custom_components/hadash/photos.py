"""Photos for the screensaver from Home Assistant's Media section.

Any media source works: the local media folder, Synology Photos, Immich, and others that add photos to
Media. The tablet never loads the original: phone photos are often 4000 pixels wide and several MB, and a
Fire tablet needs seconds to open one. This view makes a copy at the screen's size, keeps the last few
in memory (no disk writes), and the tablet only ever loads that small copy.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
from io import BytesIO
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web
import voluptuous as vol

from homeassistant.components import media_source, websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

PHOTO_URL = "/api/hadash/photo"
# How long a photo address from the list keeps working. The tablet asks for a new list every hour.
SIGN_FOR = timedelta(hours=6)
MAX_PHOTOS = 1000
# Folders inside the chosen folder are searched too, this many levels down.
MAX_DEPTH = 2
CACHE_SIZE = 40
MAX_SIDE = 2560
LOCAL_PREFIX = "media-source://media_source/"


# iPhone photos (HEIC) open only when Pillow has the HEIF plugin, or through Synology's JPEG copy;
# otherwise they are left out.
HEIF = (".heic", ".heif")


def _heif_ok() -> bool:
    try:
        from pillow_heif import register_heif_opener  # noqa: PLC0415

        register_heif_opener()
    except ImportError:
        return False
    return True


def _is_heif(media_id: str, title: str = "", kind: str = "") -> bool:
    name = f"{media_id} {title}".lower()
    return any(ext in name or kind.endswith(ext[1:]) for ext in HEIF)


def _is_photo(item: Any, heif: bool) -> bool:
    kind = str(getattr(item, "media_content_type", "") or "")
    if not (getattr(item, "media_class", None) == "image" or kind.startswith("image/")):
        return False
    # Synology and Immich make their own JPEG copy of every photo, so their HEIC photos always work.
    if heif or item.media_content_id.startswith(CONVERTS):
        return True
    return not _is_heif(item.media_content_id, item.title or "", kind)


SYNO_PREFIX = "media-source://synology_dsm/"
# Immich's "fullsize" address gives a JPEG for formats a browser cannot show, HEIC included.
CONVERTS = (SYNO_PREFIX, "media-source://immich/")


async def _synology_preview(hass: HomeAssistant, media_id: str) -> bytes | None:
    """Synology Photos' own JPEG copy ("xl", about 1280 pixels). It works for HEIC too.

    This reaches into the Synology DSM integration, so any change there only turns it off.
    """
    if not media_id.startswith(SYNO_PREFIX):
        return None
    try:
        from homeassistant.components.synology_dsm.media_source import (  # noqa: PLC0415
            SynologyPhotosMediaSourceIdentifier,
        )
        from synology_dsm.api.photos.model import SynoPhotosItem  # noqa: PLC0415

        ident = SynologyPhotosMediaSourceIdentifier(media_id[len(SYNO_PREFIX) :])
        if not ident.cache_key:
            return None
        entry = hass.config_entries.async_entry_for_domain_unique_id("synology_dsm", ident.unique_id)
        photos = entry.runtime_data.api.photos if entry and getattr(entry, "runtime_data", None) else None
        if photos is None:
            return None
        item_id = int(ident.cache_key.split("_")[0])
        item = SynoPhotosItem(item_id, "", ident.file_name or "", "", ident.cache_key, "xl", ident.is_shared, ident.passphrase)
        return await photos.download_item_thumbnail(item)
    except Exception as err:  # noqa: BLE001 - fall back to the original
        _LOGGER.debug("Synology preview for %s failed: %s", media_id, err)
        return None


async def async_list_photos(hass: HomeAssistant, media_id: str) -> tuple[str, list[dict[str, str]]]:
    """The folder's title and its photos (media ids and titles), its subfolders included."""
    root = await media_source.async_browse_media(hass, media_id)
    heif = await hass.async_add_executor_job(_heif_ok)
    found: list[dict[str, str]] = []

    async def walk(item: Any, depth: int) -> None:
        for child in item.children or []:
            if len(found) >= MAX_PHOTOS:
                return
            if not child.can_expand and _is_photo(child, heif):
                found.append({"id": child.media_content_id, "title": child.title or ""})
            elif child.can_expand and depth < MAX_DEPTH:
                try:
                    await walk(await media_source.async_browse_media(hass, child.media_content_id), depth + 1)
                except Exception:  # noqa: BLE001 - one broken folder must not hide the rest
                    _LOGGER.debug("Could not open %s", child.media_content_id)

    await walk(root, 0)
    return root.title or "", found


def _local_file(hass: HomeAssistant, media_id: str) -> Path | None:
    """A photo in the local media folder is read from disk directly."""
    if not media_id.startswith(LOCAL_PREFIX):
        return None
    source_dir, _, location = media_id[len(LOCAL_PREFIX) :].partition("/")
    base = hass.config.media_dirs.get(source_dir)
    if not base or not location:
        return None
    path = (Path(base) / location).resolve()
    # Never leave the media folder.
    if Path(base).resolve() not in path.parents:
        return None
    return path


async def _original(hass: HomeAssistant, media_id: str) -> bytes:
    path = _local_file(hass, media_id)
    if path is not None:
        return await hass.async_add_executor_job(path.read_bytes)
    play = await media_source.async_resolve_media(hass, media_id, None)
    url = play.url
    session = async_get_clientsession(hass, verify_ssl=False)
    if url.startswith("/"):
        # The photo is served by Home Assistant itself (Synology Photos, Immich): ask it over the loopback.
        if "authSig=" not in url:
            url = async_sign_path(hass, url, timedelta(minutes=2), use_content_user=True)
        api = hass.config.api
        scheme = "https" if api and api.use_ssl else "http"
        port = api.port if api else 8123
        url = f"{scheme}://127.0.0.1:{port}{url}"
    async with session.get(url, timeout=30) as resp:
        resp.raise_for_status()
        return await resp.read()


def _shrink(data: bytes, width: int, height: int) -> bytes:
    """A JPEG that covers the screen (or half of it, for a pair), turned the right way up. Smaller photos are not enlarged."""
    from PIL import Image, ImageOps  # Home Assistant ships Pillow; imported late to keep startup light.

    with Image.open(BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        # An upright photo on a wide screen is shown in a pair, half the screen wide (and the other way
        # round on an upright screen), so it only needs to cover half the screen.
        if (img.height > img.width) != (height > width):
            if width >= height:
                width = (width + 1) // 2
            else:
                height = (height + 1) // 2
        scale = max(width / img.width, height / img.height)
        if scale < 1:
            img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        out = BytesIO()
        img.save(out, "JPEG", quality=82, optimize=True, progressive=True)
        return out.getvalue()


async def _photo(hass: HomeAssistant, media_id: str, width: int, height: int) -> bytes:
    """The photo at the screen's size. HEIC from Synology comes as Synology's own JPEG copy."""
    if _is_heif(media_id):
        preview = await _synology_preview(hass, media_id)
        if preview:
            return await hass.async_add_executor_job(_shrink, preview, width, height)
    original = await _original(hass, media_id)
    try:
        return await hass.async_add_executor_job(_shrink, original, width, height)
    except Exception:
        # Pillow cannot open it (for example HEIC under another name): try Synology's copy.
        preview = await _synology_preview(hass, media_id)
        if not preview:
            raise
        return await hass.async_add_executor_job(_shrink, preview, width, height)


class PhotoView(HomeAssistantView):
    """One photo at the screen's size. The address comes signed from the photo list."""

    url = PHOTO_URL
    name = "api:hadash:photo"
    requires_auth = True

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, int, int], bytes] = OrderedDict()

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        media_id = request.query.get("id", "")
        try:
            width = max(64, min(MAX_SIDE, int(request.query.get("w", "1280"))))
            height = max(64, min(MAX_SIDE, int(request.query.get("h", "800"))))
        except ValueError:
            return web.Response(status=400)
        if not media_id.startswith("media-source://"):
            return web.Response(status=400)
        key = (media_id, width, height)
        body = self._cache.get(key)
        if body is None:
            try:
                body = await _photo(hass, media_id, width, height)
            except Exception as err:  # noqa: BLE001 - the tablet skips a photo it cannot load
                _LOGGER.debug("Photo %s failed: %s", media_id, err)
                return web.Response(status=404)
            self._cache[key] = body
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=21600"})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/photos",
        vol.Required("media"): str,
        vol.Optional("width", default=1280): vol.All(int, vol.Range(min=64, max=MAX_SIDE)),
        vol.Optional("height", default=800): vol.All(int, vol.Range(min=64, max=MAX_SIDE)),
    }
)
@websocket_api.async_response
async def ws_photos(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The photos of a Media folder or album, each with a signed address of a copy at the screen's size."""
    try:
        title, photos = await async_list_photos(hass, msg["media"])
    except Exception as err:  # noqa: BLE001 - a folder that is gone or a source that is offline
        connection.send_error(msg["id"], "not_found", f"Could not open the folder: {err}")
        return
    size = f"&w={msg['width']}&h={msg['height']}"
    for p in photos:
        p["url"] = async_sign_path(hass, f"{PHOTO_URL}?id={quote(p.pop('id'), safe='')}{size}", SIGN_FOR)
    connection.send_result(msg["id"], {"title": title, "photos": photos})
