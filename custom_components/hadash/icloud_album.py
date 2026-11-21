"""Photos from an Apple Photos Shared Album with "Public Website" turned on.

Apple gives no way to read a Photos library from a tablet, but a Shared Album with a public website can be
read by anyone who has its link. In Apple Photos: make a Shared Album, turn on "Public Website", and copy
the link (https://www.icloud.com/sharedalbum/#B0aB…). Photos added to the album on an iPhone or a Mac
appear on the screens within the hour.

Home Assistant reads the album, not the tablet: Apple's addresses do not allow a web page on another
address to read them, and this way each tablet still gets a small copy at its own screen size.

Apple has two kinds of links, and both work:
- New (Photos in 2026): https://photos.icloud.com/shared/album/<key>. The album lives in CloudKit:
  1. records/resolve with the key gives the album's name, its zone, a short-lived public token and server.
  2. records/query on that server lists the photos: CPLAsset (date) and CPLMaster (sizes, each with a
     download address). The medium JPEG (2048 pixels) is used, so HEIC photos work too.
- Older: https://www.icloud.com/sharedalbum/#<token>, read with two requests:
1. webstream: the album's name and its photos, each with a few sizes (derivatives) and a checksum per size.
2. webasseturls: for some photos, the address of each size. These addresses stop working after a while,
   so they are asked for when a photo is needed and kept for a short time only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# A photo from a Shared Album, as the photo list names it: icloud-album://<album token>/<photo guid>.
ALBUM_PREFIX = "icloud-album://"
DATA_KEY = "hadash_icloud_albums"

# https://www.icloud.com/sharedalbum/#B0aB…, also with a language part (/sharedalbum/en-us/#…).
LINK = re.compile(r"icloud\.com/sharedalbum/(?:[A-Za-z-]+/)?#([A-Za-z0-9]{10,40})")
# https://photos.icloud.com/shared/album/058yJ… (and the same path on share.icloud.com or www.icloud.com).
CK_LINK = re.compile(r"icloud\.com/(?:photos/)?shared/album/([A-Za-z0-9_-]{15,40})")
# New links are kept as "ck:<key>" inside photo ids, so the two kinds never mix.
CK = "ck:"
CK_RESOLVE = "https://ckdatabasews.icloud.com/database/1/com.apple.photos.cloud/production/public/records/resolve"
CK_PAGE = 200
# The public token lives 20 minutes, and the photo addresses about as long: both are renewed after 15.
CK_KEEP = 15 * 60
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# The album's list is read again after this long. The tablets ask every hour.
LIST_KEEP = 30 * 60
# Apple's photo addresses work for about an hour; ours are used for 30 minutes at most.
URL_KEEP = 30 * 60
# Addresses are asked for in groups, so the next photos are ready too.
URL_GROUP = 25
MAX_PHOTOS = 1000
# The largest size that is still at most this many pixels on its long side is used.
MAX_SIDE = 2560
TIMEOUT = 20


class AlbumError(Exception):
    """The album could not be read: a wrong link, or "Public Website" is off."""


def album_token(text: str) -> str | None:
    """The album's token from its link ("ck:<key>" for a new link), or None when it is not a Shared Album link."""
    m = CK_LINK.search(text or "")
    if m:
        return CK + m[1]
    m = LINK.search(text or "")
    return m[1] if m else None


def is_album_link(text: str) -> bool:
    return album_token(text) is not None


def _base62(text: str) -> int:
    n = 0
    for ch in text:
        n = n * 62 + BASE62.index(ch)
    return n


def partition_host(token: str) -> str:
    """The server Apple keeps the album on, written in its token. Apple redirects when it is another."""
    try:
        n = _base62(token[1]) if token[0] == "A" else _base62(token[1:3])
    except ValueError:
        n = 23
    return f"p{n:02d}-sharedstreams.icloud.com"


def _date(raw: Any) -> str | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw or ""))
    return f"{m[1]}-{m[2]}-{m[3]}" if m and 1990 <= int(m[1]) <= 2099 else None


def _best_checksum(derivatives: dict[str, Any]) -> str | None:
    """The checksum of the largest size up to MAX_SIDE, or of the smallest when every size is larger."""
    sizes: list[tuple[int, str]] = []
    for d in (derivatives or {}).values():
        if not isinstance(d, dict) or not d.get("checksum"):
            continue
        try:
            side = max(int(d.get("width") or 0), int(d.get("height") or 0))
        except (TypeError, ValueError):
            side = 0
        sizes.append((side, d["checksum"]))
    if not sizes:
        return None
    fits = [s for s in sizes if 0 < s[0] <= MAX_SIDE]
    return max(fits)[1] if fits else min(sizes)[1]


# Videos in an album are left out; their poster frame is not a photo to show.
VIDEO_KINDS = ("movie", "video", "mpeg", "avi", "m4v")
# CloudKit keeps several sizes of each photo. The medium JPEG (2048 pixels) is the best fit for a screen; a
# small photo may only have its original (shown when it is a JPEG or PNG), and the thumbnail is the last choice.
CK_SIZES = ("resJPEGMedRes", "resJPEGFullRes", "resOriginalRes", "resJPEGThumbRes")


def _ck_address(fields: dict[str, Any], kind: str) -> str | None:
    for name in CK_SIZES:
        if name == "resOriginalRes" and not any(k in kind for k in ("jpeg", "jpg", "png")):
            continue
        value = (fields.get(name) or {}).get("value")
        if isinstance(value, dict) and value.get("downloadURL"):
            return str(value["downloadURL"])
    return None


@dataclass
class _Album:
    host: str
    title: str = ""
    at: float = 0.0
    # guid: (checksum of the size we use, taken YYYY-MM-DD or None, caption or "")
    photos: dict[str, tuple[str, str | None, str]] = field(default_factory=dict)
    # checksum: (address, when it was given)
    urls: dict[str, tuple[str, float]] = field(default_factory=dict)


class SharedAlbums:
    """The Shared Albums the screens use, read from Apple and kept for a short time."""

    def __init__(self) -> None:
        self._albums: dict[str, _Album] = {}

    async def _post(self, hass: HomeAssistant, album: _Album, token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        session = async_get_clientsession(hass)
        for _ in range(2):
            url = f"https://{album.host}/{token}/sharedstreams/{path}"
            # Apple's own page sends the JSON as plain text.
            async with session.post(url, data=json.dumps(body), headers={"Content-Type": "text/plain"}, timeout=TIMEOUT) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text) if text else {}
                except ValueError:
                    data = {}
                # The album lives on another server: Apple answers 330 with that server's name.
                other = data.get("X-Apple-MMe-Host") if isinstance(data, dict) else None
                if other and other != album.host:
                    album.host = str(other)
                    continue
                if resp.status >= 400:
                    raise AlbumError(f"Apple answered {resp.status}")
                if not isinstance(data, dict):
                    raise AlbumError("Apple sent an answer we do not understand")
                return data
        raise AlbumError("Apple sent the request on too many times")

    async def _ck_list(self, hass: HomeAssistant, token: str, album: _Album) -> None:
        """A new link: the album from CloudKit. Each photo's medium JPEG address comes with the list."""
        key = token[len(CK) :]
        session = async_get_clientsession(hass)

        async def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
            async with session.post(url, data=json.dumps(body), headers={"Content-Type": "text/plain"}, timeout=TIMEOUT) as resp:
                if resp.status >= 400:
                    # Apple's own reason, such as "BadRequestException: Unexpected input", helps a support answer.
                    try:
                        reason = (await resp.json(content_type=None)).get("reason", "")
                    except Exception:  # noqa: BLE001 - no reason given
                        reason = ""
                    raise AlbumError(f"Apple answered {resp.status} {reason}".strip())
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    raise AlbumError("Apple sent an answer we do not understand")
                return data

        found = await post(f"{CK_RESOLVE}?sharing_url_key={key}", {"shortGUIDs": [{"value": key}]})
        res = (found.get("results") or [{}])[0]
        public = res.get("anonymousPublicAccess") or {}
        zone, server, ptoken = res.get("zoneID"), str(public.get("databasePartition") or ""), public.get("token")
        if not zone or not ptoken or not server.startswith("https://"):
            raise AlbumError("The album has no public website")
        title = (((res.get("share") or {}).get("fields") or {}).get("cloudkit.title") or {}).get("value") or ""
        url = f"{server.removesuffix(':443')}/database/1/com.apple.photos.cloud/production/shared/records/query"
        # remapEnums: Apple reads "ASCENDING" as a name only with it; without it the query is refused (400).
        params = f"?remapEnums=true&sharing_url_key={key}&publicAccessAuthToken={quote(str(ptoken), safe='')}"
        masters: dict[str, tuple[str, str]] = {}  # master: (address, kind)
        dates: dict[str, str] = {}  # master: taken
        order: list[str] = []
        rank = 0
        # A page that adds nothing new ends the list, so an answer that repeats itself can never loop.
        for _ in range(MAX_PHOTOS // 50):
            if len(order) >= MAX_PHOTOS:
                break
            known = len(order) + len(masters)
            body = {
                "query": {
                    "recordType": "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted",
                    "filterBy": [
                        {"fieldName": "startRank", "fieldValue": {"type": "INT64", "value": rank}, "comparator": "EQUALS"},
                        {"fieldName": "direction", "fieldValue": {"type": "STRING", "value": "ASCENDING"}, "comparator": "EQUALS"},
                    ],
                },
                "zoneID": zone,
                "resultsLimit": CK_PAGE,
            }
            records = (await post(url + params, body)).get("records") or []
            assets = 0
            for r in records:
                fields = r.get("fields") or {}
                if r.get("recordType") == "CPLAsset":
                    assets += 1
                    master = ((fields.get("masterRef") or {}).get("value") or {}).get("recordName")
                    when = (fields.get("assetDate") or {}).get("value")
                    if master and isinstance(when, (int, float)):
                        # The date where the photo was taken: Apple keeps the time zone's offset beside it.
                        offset = (fields.get("timeZoneOffset") or {}).get("value") or 0
                        dates[master] = datetime.fromtimestamp(when / 1000 + offset, UTC).date().isoformat()
                    if master and master not in order:
                        order.append(master)
                elif r.get("recordType") == "CPLMaster":
                    kind = str((fields.get("itemType") or {}).get("value") or "")
                    if any(v in kind for v in VIDEO_KINDS):
                        continue
                    link = _ck_address(fields, kind)
                    if link:
                        masters[str(r.get("recordName"))] = (link.replace("${f}", "photo.jpg"), kind)
            if not assets or len(records) < CK_PAGE or len(order) + len(masters) == known:
                break
            rank += assets
        now = time.monotonic()
        album.title = str(title)
        album.photos = {m: (m, _date(dates.get(m)), "") for m in order if m in masters}
        album.urls = {m: (masters[m][0], now) for m in album.photos}
        album.at = now

    async def async_list(self, hass: HomeAssistant, link: str) -> tuple[str, list[dict[str, str]]]:
        """The album's name and its photos (id, taken, place) for the photo list."""
        token = album_token(link)
        if not token:
            raise AlbumError("This is not a Shared Album link")
        new = token.startswith(CK)
        album = self._albums.get(token) or _Album(host="" if new else partition_host(token))
        if new:
            if time.monotonic() - album.at > CK_KEEP or not album.photos:
                await self._ck_list(hass, token, album)
                self._albums[token] = album
        elif time.monotonic() - album.at > LIST_KEEP or not album.photos:
            data = await self._post(hass, album, token, "webstream", {"streamCtag": None})
            photos: dict[str, tuple[str, str | None, str]] = {}
            for p in data.get("photos") or []:
                if not isinstance(p, dict) or p.get("mediaAssetType") == "video":
                    continue
                guid, checksum = p.get("photoGuid"), _best_checksum(p.get("derivatives") or {})
                if not guid or not checksum:
                    continue
                photos[str(guid)] = (checksum, _date(p.get("dateCreated")) or _date(p.get("batchDateCreated")), str(p.get("caption") or "").strip())
                if len(photos) >= MAX_PHOTOS:
                    break
            album.photos = photos
            album.title = str(data.get("streamName") or "")
            album.at = time.monotonic()
            self._albums[token] = album
        out: list[dict[str, str]] = []
        for guid, (_, taken, caption) in album.photos.items():
            item = {"id": f"{ALBUM_PREFIX}{token}/{guid}", "title": ""}
            if taken:
                item["taken"] = taken
            # A short caption written in Apple Photos shows as the photo's quiet line, like a place.
            if caption and len(caption) <= 48 and "\n" not in caption:
                item["place"] = caption
            out.append(item)
        return album.title, out

    async def async_photo(self, hass: HomeAssistant, media_id: str) -> bytes:
        """The photo's bytes, at the largest size Apple has up to MAX_SIDE."""
        token, _, guid = media_id[len(ALBUM_PREFIX) :].partition("/")
        link = f"icloud.com/shared/album/{token[len(CK) :]}" if token.startswith(CK) else f"icloud.com/sharedalbum/#{token}"
        album = self._albums.get(token)
        if album is None or guid not in album.photos or (token.startswith(CK) and time.monotonic() - album.at > CK_KEEP):
            # Home Assistant restarted since the list was made, or the addresses are old: read the album again.
            await self.async_list(hass, link)
            album = self._albums.get(token)
            if album is None or guid not in album.photos:
                raise AlbumError("The photo is no longer in the album")
        checksum = album.photos[guid][0]
        now = time.monotonic()
        known = album.urls.get(checksum)
        if token.startswith(CK):
            if not known:
                raise AlbumError("Apple gave no address for the photo")
        elif not known or now - known[1] > URL_KEEP:
            # Ask for this photo and the next ones in the album together.
            guids = list(album.photos)
            start = guids.index(guid)
            group = [g for g in guids[start:] + guids[:start] if (u := album.urls.get(album.photos[g][0])) is None or now - u[1] > URL_KEEP][:URL_GROUP]
            if guid not in group:
                group.insert(0, guid)
            data = await self._post(hass, album, token, "webasseturls", {"photoGuids": group})
            items = data.get("items") or {}
            for key, it in items.items() if isinstance(items, dict) else ():
                if isinstance(it, dict) and it.get("url_location") and it.get("url_path"):
                    album.urls[key] = (f"https://{it['url_location']}{it['url_path']}", now)
            known = album.urls.get(checksum)
            if not known:
                raise AlbumError("Apple gave no address for the photo")
        session = async_get_clientsession(hass)
        async with session.get(known[0], timeout=30) as resp:
            if resp.status >= 400:
                album.urls.pop(checksum, None)
                # An old address: the next photo reads the album again.
                album.at = 0
            resp.raise_for_status()
            return await resp.read()


def albums(hass: HomeAssistant) -> SharedAlbums:
    found = hass.data.get(DATA_KEY)
    if found is None:
        found = hass.data[DATA_KEY] = SharedAlbums()
    return found
