"""When and where a screensaver photo was taken, for "On this day" and the quiet place and date line.

The facts come from the cheapest place that has them:
1. Synology Photos already knows the date and the place (it looks up the address from the GPS position).
   One request per album gives them for every photo, without opening a single file.
2. A photo in the local media folder has its date in the EXIF header. Only the header is read, once;
   the answer is kept until the file changes.
3. Any other source: a date in the file name, such as IMG_20240512_101500.jpg or 2024-05-12 Florence.jpg.

Nothing here is required: a photo without facts is simply shown without a line.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from pathlib import Path
import re
import sys
import time
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SYNO_PREFIX = "media-source://synology_dsm/"
LOCAL_PREFIX = "media-source://media_source/"
# Synology's answer is kept this long; the tablets ask for a new list every hour.
SYNO_KEEP = 50 * 60
SYNO_PAGE = 500
SYNO_MAX = 2000

# Synology Photos' address parts, the most useful town name first.
PLACE_KEYS = ("city", "town", "village", "district", "county", "state")

# 20240512, 2024-05-12, 2024_05_12 or 2024.05.12 in a file name, between 1990 and 2099.
NAME_DATE = re.compile(r"(?<!\d)(19[9]\d|20\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")

# EXIF tags: DateTimeOriginal in the Exif block, and the plain DateTime as a fallback.
EXIF_IFD = 0x8769
DATE_ORIGINAL = 36867
DATE_PLAIN = 306


def date_from_name(name: str) -> str | None:
    """A date written in the file name, as YYYY-MM-DD."""
    m = NAME_DATE.search(name or "")
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3])).date().isoformat()
    except ValueError:
        return None


def _exif_date(raw: str | bytes | None) -> str | None:
    """'2024:05:12 10:15:00' becomes '2024-05-12'. Cameras without a clock write zeros: those are skipped."""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    m = re.match(r"\s*(\d{4})[:\-](\d{2})[:\-](\d{2})", raw or "")
    if not m:
        return None
    try:
        day = datetime(int(m[1]), int(m[2]), int(m[3])).date()
    except ValueError:
        return None
    return day.isoformat() if day.year >= 1990 else None


def read_exif_date(path: Path) -> str | None:
    """The date the photo was taken, from its EXIF header. Pillow reads the header only, not the picture."""
    from PIL import Image  # noqa: PLC0415 - Home Assistant ships Pillow; imported late to keep startup light.

    try:
        with Image.open(path) as img:
            exif = img.getexif()
            return _exif_date(exif.get_ifd(EXIF_IFD).get(DATE_ORIGINAL)) or _exif_date(exif.get(DATE_PLAIN))
    except Exception:  # noqa: BLE001 - a broken or unknown file has no date
        return None


def place_from_address(address: dict[str, Any] | None) -> str | None:
    """A short place name from Synology Photos' address: the town, or the region when there is no town."""
    if not isinstance(address, dict):
        return None
    for key in PLACE_KEYS:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = address.get("country")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _syno_parts(media_id: str) -> tuple[str, str, str] | None:
    """(Synology entry, album part, thumbnail cache key) from a Synology Photos media id.

    The cache key ("40640_1725908154") is how a photo is matched: its first number is not the photo's id.
    """
    parts = media_id[len(SYNO_PREFIX) :].split("/")
    if len(parts) < 4 or not parts[2]:
        return None
    return parts[0], parts[1], parts[2]


class PhotoInfo:
    """Date and place per photo, kept in memory. Shared by every tablet."""

    def __init__(self) -> None:
        # Local files: media id -> (file time, date).
        self._local: dict[str, tuple[float, str | None]] = {}
        # Synology: (entry, album part) -> (when asked, {cache key: (date, place)}).
        self._syno: dict[tuple[str, str], tuple[float, dict[str, tuple[str | None, str | None]]]] = {}

    async def add_facts(self, hass: HomeAssistant, photos: list[dict[str, Any]]) -> None:
        """Adds "taken" (YYYY-MM-DD) and "place" to each photo that has them. Each photo needs "id" and "title"."""
        syno_albums: set[tuple[str, str]] = set()
        local: list[tuple[dict[str, Any], Path]] = []
        for p in photos:
            media_id = p["id"]
            if media_id.startswith(SYNO_PREFIX) and (parts := _syno_parts(media_id)):
                syno_albums.add(parts[:2])
            elif media_id.startswith(LOCAL_PREFIX) and (path := _local_path(hass, media_id)):
                local.append((p, path))

        for album in syno_albums:
            await self._load_synology(hass, *album)
        if local:
            await hass.async_add_executor_job(self._read_local, local)

        for p in photos:
            taken = place = None
            if p["id"].startswith(SYNO_PREFIX) and (parts := _syno_parts(p["id"])):
                taken, place = self._syno.get(parts[:2], (0, {}))[1].get(parts[2], (None, None))
            elif p["id"] in self._local:
                taken = self._local[p["id"]][1]
            taken = taken or date_from_name(p.get("title", ""))
            if taken:
                p["taken"] = taken
            if place:
                p["place"] = place

    def _read_local(self, items: list[tuple[dict[str, Any], Path]]) -> None:
        for p, path in items:
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            known = self._local.get(p["id"])
            if known and known[0] == stamp:
                continue
            self._local[p["id"]] = (stamp, read_exif_date(path))

    async def _load_synology(self, hass: HomeAssistant, unique_id: str, album: str) -> None:
        key = (unique_id, album)
        known = self._syno.get(key)
        if known and time.monotonic() - known[0] < SYNO_KEEP:
            return
        facts: dict[str, tuple[str | None, str | None]] = {}
        try:
            for item in await _synology_items(hass, unique_id, album):
                stamp = item.get("time")
                taken = None
                if isinstance(stamp, (int, float)) and stamp > 0:
                    # Synology keeps the camera's own clock time as if it were UTC, so UTC gives the right day.
                    taken = datetime.fromtimestamp(stamp, UTC).date().isoformat()
                address = (item.get("additional") or {}).get("address")
                cache_key = ((item.get("additional") or {}).get("thumbnail") or {}).get("cache_key")
                if cache_key:
                    facts[str(cache_key)] = (taken, place_from_address(address))
        except Exception as err:  # noqa: BLE001 - any change in Synology's answer only turns this off
            _LOGGER.debug("Synology photo facts for %s failed: %s", album, err)
        self._syno[key] = (time.monotonic(), facts)


def _local_path(hass: HomeAssistant, media_id: str) -> Path | None:
    source_dir, _, location = media_id[len(LOCAL_PREFIX) :].partition("/")
    base = hass.config.media_dirs.get(source_dir)
    if not base or not location:
        return None
    path = (Path(base) / location).resolve()
    if Path(base).resolve() not in path.parents:
        return None
    return path


async def _synology_items(hass: HomeAssistant, unique_id: str, album: str) -> list[dict[str, Any]]:
    """The album's photos as Synology Photos lists them, with their time and address.

    This asks the Synology DSM integration's own connection, the same way its media browser does,
    only with the address added. The album part is "<album id>_<passphrase>", or "shared" for the shared space.
    """
    if "homeassistant.components.synology_dsm.media_source" not in sys.modules:
        return []
    entry = hass.config_entries.async_entry_for_domain_unique_id("synology_dsm", unique_id)
    api = getattr(getattr(entry, "runtime_data", None), "api", None) if entry else None
    photos = getattr(api, "photos", None)
    dsm = getattr(photos, "_dsm", None)
    if photos is None or dsm is None:
        return []
    album_id, _, passphrase = album.partition("_")
    params: dict[str, Any] = {"additional": '["thumbnail","address"]'}
    if album_id == "shared":
        api_key = photos.BROWSE_ITEM_FOTOTEAM_API_KEY
    else:
        api_key = photos.BROWSE_ITEM_API_KEY
        if passphrase:
            params["passphrase"] = passphrase
        else:
            params["album_id"] = int(album_id)
    found: list[dict[str, Any]] = []
    while len(found) < SYNO_MAX:
        raw = await dsm.get(api_key, "list", {**params, "offset": len(found), "limit": SYNO_PAGE})
        page = (raw or {}).get("data", {}).get("list") if isinstance(raw, dict) else None
        if not page:
            break
        found.extend(page)
        if len(page) < SYNO_PAGE:
            break
    return found
