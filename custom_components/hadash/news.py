"""News for the news card: stories from RSS and Atom feeds.

News sites, blogs, YouTube channels, Reddit, Mastodon, Bluesky and podcasts all offer a feed. Home Assistant
reads them here, not the tablets: a feed site rarely lets a web page read it directly, and a slow Fire tablet
should not parse a large feed. Each feed is read at most every 15 minutes, whatever the number of tablets.

Pictures go through Home Assistant too, as a small copy at the size the tablet asked for. A news photo is often
2000 pixels wide; a Fire tablet would need seconds to open it.

A tablet may only ask for feeds that are in the published layout (or the studio's draft), so a signed-in user
cannot make Home Assistant fetch any address. The studio (admins only) may check any link.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
from ipaddress import ip_address
from html.entities import name2codepoint
from io import BytesIO
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse
import xml.etree.ElementTree as ET

from aiohttp import ClientTimeout, web
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, DRAFT, NEWS, VERSION

_LOGGER = logging.getLogger(__name__)

IMAGE_URL = "/api/hadash/news/image"
FRESH = 15 * 60
# After a failed read, the next try comes sooner than a normal refresh.
RETRY = 5 * 60
MAX_ITEMS = 20
MAX_FEED_BYTES = 3_000_000
MAX_PAGE_BYTES = 400_000
MAX_IMAGE_BYTES = 12_000_000
SIGN_FOR = timedelta(hours=6)
IMAGE_CACHE = 80
# Stories without a picture in the feed: the story's own page often names one (og:image). Only a few per read.
PAGE_LOOKUPS = 8
SUMMARY_CHARS = 320
# Finding a feed from a link: each guess gets a short wait, and the whole search never takes longer than this.
GUESS_WAIT = 8
CHECK_WAIT = 25
WIDTHS = (320, 480, 720, 960, 1280, 1600)
HEADERS = {
    "User-Agent": f"DashboardStudio/{VERSION} (Home Assistant; +https://lukaps.com/dashboardstudio/)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
}
# These sites closed their open feeds; the studio says so instead of "no feed found".
CLOSED = ("x.com", "twitter.com", "facebook.com", "instagram.com", "threads.net", "threads.com", "tiktok.com", "linkedin.com")

NS_MEDIA = "http://search.yahoo.com/mrss/"
NS_ATOM = "http://www.w3.org/2005/Atom"
NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
NS_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
NS_DC = "http://purl.org/dc/elements/1.1/"


async def resolve(hass: HomeAssistant, host: str) -> list[str]:
    """The addresses a host name points to. Tests replace this."""
    infos = await hass.loop.getaddrinfo(host, None)
    return [i[4][0] for i in infos]


REDIRECTS = (301, 302, 303, 307, 308)


class NewsError(Exception):
    """A link that gives no news. code: closed, not_reachable, no_feed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- reading a feed


def _local(tag: Any) -> tuple[str, str]:
    """(namespace, name) of an element's tag."""
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _kids(el: ET.Element, name: str, ns: str | None = None) -> list[ET.Element]:
    out = []
    for c in el:
        cns, cname = _local(c.tag)
        if cname == name and (ns is None or cns == ns):
            out.append(c)
    return out


def _kid(el: ET.Element, name: str, ns: str | None = None) -> ET.Element | None:
    found = _kids(el, name, ns)
    return found[0] if found else None


def _kid_text(el: ET.Element, name: str, ns: str | None = None) -> str:
    k = _kid(el, name, ns)
    return "".join(k.itertext()).strip() if k is not None else ""


_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def _fix_entities(text: str) -> str:
    """HTML entities such as &nbsp; are not XML, and many feeds use them. Bare & signs are common too."""

    def entity(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        if name in name2codepoint:
            return f"&#{name2codepoint[name]};"
        return f"&amp;{name};"

    text = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]*;)", "&amp;", text)
    return re.sub(r"&([A-Za-z][A-Za-z0-9]*);", entity, text)


_TAGS = re.compile(r"<[^>]*>")
_DROP = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_SPACES = re.compile(r"\s+")
_IMG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)


def plain(markup: str) -> str:
    """Text from a bit of HTML: no tags, entities read, spaces tidied."""
    text = _DROP.sub(" ", markup or "")
    # Block tags part words; inline tags (<b>, <a>) do not.
    text = re.sub(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|td|blockquote|figure|figcaption|section|article)\b[^>]*>", " ", text, flags=re.I)
    text = html.unescape(_TAGS.sub("", text))
    return _SPACES.sub(" ", text).strip()


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:-–— ")
    return f"{cut}…"


def _tidy_summary(text: str, title: str) -> str:
    # Reddit adds "submitted by /u/name [link] [comments]" to every post.
    text = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", text, flags=re.S)
    text = text.replace("[link]", "").replace("[comments]", "").strip()
    if text.lower().startswith(title.lower()) and title:
        text = text[len(title):].lstrip(" :.-–—")
    return _cut(text, SUMMARY_CHARS)


def _when(text: str) -> float | None:
    """Seconds since 1970, from an RSS date (RFC 822) or an Atom date (ISO 8601)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _good_image(url: str | None) -> bool:
    if not url or url.startswith("data:"):
        return False
    low = url.lower().split("?")[0]
    # Tracking pixels, smileys and share buttons are not story pictures.
    return not any(w in low for w in ("pixel", "feedburner", "/emoji", "smilies", "1x1", "spacer", "gravatar.com/avatar", "/share"))


def _media_image(el: ET.Element) -> str | None:
    """A picture from Media RSS: a thumbnail, or a content that is an image. Looks inside media:group too."""
    for scope in [el, *_kids(el, "group", NS_MEDIA)]:
        for t in _kids(scope, "thumbnail", NS_MEDIA):
            if _good_image(t.get("url")):
                return t.get("url")
        best = None
        for c in _kids(scope, "content", NS_MEDIA):
            kind = (c.get("medium") or c.get("type") or "").lower()
            if "image" in kind or (not kind and re.search(r"\.(jpe?g|png|webp)(\?|$)", c.get("url") or "", re.I)):
                width = int(c.get("width") or 0) if (c.get("width") or "").isdigit() else 0
                if _good_image(c.get("url")) and (best is None or width > best[0]):
                    best = (width, c.get("url"))
        if best:
            return best[1]
    return None


def _kind_of(url: str, items: list[dict[str, Any]], audio: bool) -> str:
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host:
        return "video"
    if "reddit.com" in host:
        return "forum"
    if audio:
        return "podcast"
    if "bsky.app" in host or (items and sum(1 for i in items if i.get("untitled")) > len(items) / 2):
        return "social"
    return "news"


def parse_feed(data: bytes | str, url: str = "") -> dict[str, Any]:
    """The feed's title, site and stories, newest first. Raises ValueError when this is not a feed."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = text.lstrip("﻿ \t\r\n")
    # The declared encoding is for bytes; the text is already decoded.
    text = re.sub(r"^<\?xml[^>]*\?>", "", text)
    # A feed never defines its own entities. A file that does could ask the reader to expand them a billion times.
    if re.search(r"<!ENTITY", text[:20000], re.I):
        raise ValueError("defines its own entities")
    try:
        root = ET.fromstring(_fix_entities(text))
    except ET.ParseError as err:
        raise ValueError(f"not XML: {err}") from err
    _, rname = _local(root.tag)
    items: list[dict[str, Any]] = []
    audio = False
    if rname == "feed":
        title = plain(_kid_text(root, "title"))
        site = ""
        for link in _kids(root, "link"):
            if link.get("rel", "alternate") == "alternate":
                site = link.get("href", "")
                break
        icon = _kid_text(root, "icon") or _kid_text(root, "logo")
        for entry in _kids(root, "entry"):
            href = ""
            for link in _kids(entry, "link"):
                if link.get("rel", "alternate") == "alternate":
                    href = link.get("href", "")
                    break
            content = _kid_text(entry, "content") or _kid_text(entry, "summary")
            if not content:
                group = _kid(entry, "group", NS_MEDIA)
                content = _kid_text(group, "description", NS_MEDIA) if group is not None else ""
            image = _media_image(entry)
            if not image:
                m = _IMG.search(content)
                image = m.group(1) if m and _good_image(m.group(1)) else None
            items.append(
                {
                    "id": _kid_text(entry, "id") or href,
                    "raw_title": _kid_text(entry, "title"),
                    "content": content,
                    "link": urljoin(url, href) if href else "",
                    "published": _when(_kid_text(entry, "published") or _kid_text(entry, "updated")),
                    "image": image,
                }
            )
    elif rname in ("rss", "RDF"):
        channel = _kid(root, "channel")
        if channel is None:
            raise ValueError("RSS without a channel")
        title = plain(_kid_text(channel, "title"))
        site = _kid_text(channel, "link", "") or _kid_text(channel, "link")
        img = _kid(channel, "image")
        icon = _kid_text(img, "url") if img is not None else ""
        if not icon:
            it = _kid(channel, "image", NS_ITUNES)
            icon = it.get("href", "") if it is not None else ""
        # RSS 2.0 keeps stories in the channel; RSS 1.0 (RDF) next to it.
        for item in _kids(channel, "item") + _kids(root, "item"):
            content = _kid_text(item, "encoded", NS_CONTENT) or _kid_text(item, "description")
            image = _media_image(item)
            for enc in _kids(item, "enclosure"):
                kind = (enc.get("type") or "").lower()
                if kind.startswith("audio"):
                    audio = True
                if not image and kind.startswith("image") and _good_image(enc.get("url")):
                    image = enc.get("url")
            if not image:
                it = _kid(item, "image", NS_ITUNES)
                if it is not None and _good_image(it.get("href")):
                    image = it.get("href")
            if not image:
                m = _IMG.search(content)
                image = m.group(1) if m and _good_image(m.group(1)) else None
            link = _kid_text(item, "link", "") or _kid_text(item, "link")
            guid = _kid_text(item, "guid")
            if not link and guid.startswith("http"):
                link = guid
            items.append(
                {
                    "id": guid or link,
                    "raw_title": _kid_text(item, "title"),
                    "content": content,
                    "link": urljoin(url, link) if link else "",
                    "published": _when(_kid_text(item, "pubDate") or _kid_text(item, "date", NS_DC)),
                    "image": image,
                }
            )
    else:
        raise ValueError(f"not a feed: <{rname}>")

    stories = []
    for it in items:
        story_title = plain(it.pop("raw_title"))
        text = plain(it.pop("content"))
        untitled = not story_title
        if untitled:
            # Mastodon and Bluesky posts have no title: the post itself is the headline.
            story_title, text = _cut(text, 140), ""
        if not story_title:
            continue
        it["title"] = story_title
        it["summary"] = _tidy_summary(text, story_title)
        it["untitled"] = untitled
        if it["image"]:
            it["image"] = urljoin(url, html.unescape(it["image"]))
        stories.append(it)
    # Newest first; stories without a date keep the feed's own order after the dated ones.
    stories.sort(key=lambda s: -(s["published"] or 0))
    stories = stories[:MAX_ITEMS]
    kind = _kind_of(url or site, stories, audio)
    for s in stories:
        s.pop("untitled", None)
    return {
        "title": title or urlparse(url).netloc,
        "site": urljoin(url, site) if site else "",
        "icon": urljoin(url, icon) if icon else "",
        "kind": kind,
        "items": stories,
    }


# ---------------------------------------------------------------- finding a feed from a link

_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-zA-Z:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/xml", "text/xml", "application/rdf+xml")


def _attrs(tag: str) -> dict[str, str]:
    return {k.lower(): html.unescape(v.strip("\"'")) for k, v in _ATTR.findall(tag)}


def find_feeds(page: str, base: str) -> list[str]:
    """The feeds a web page points to with <link rel="alternate">, in its own order."""
    found = []
    for tag in _LINK_TAG.findall(page):
        a = _attrs(tag)
        if "alternate" in a.get("rel", "").lower().split() and a.get("type", "").lower() in _FEED_TYPES and a.get("href"):
            if "json" in a["type"]:
                continue
            url = urljoin(base, a["href"])
            # Comment feeds come after the main feed on most blogs; skip them.
            if "comments" in url.lower() and found:
                continue
            if url not in found:
                found.append(url)
    return found


_A_TAG = re.compile(r"<a\b[^>]*>", re.I)
_FEEDISH = re.compile(r"(rss|atom|/feeds?(/|$|\.|\?)|\.xml($|\?))", re.I)


def feed_links(page: str, base: str) -> list[str]:
    """Ordinary links on a page that look like feeds, on the same site: a site's "RSS feeds" page lists them this way
    (cbc.ca/rss). Also the page's own "RSS" link, which can lead to that list."""
    host = urlparse(base).netloc.lower().removeprefix("www.")
    found: list[str] = []
    for tag in _A_TAG.findall(page):
        href = _attrs(tag).get("href", "")
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = urljoin(base, href).split("#")[0]
        other = urlparse(url).netloc.lower().removeprefix("www.")
        same = other == host or other.endswith(f".{host}") or host.endswith(f".{other}")
        if same and _FEEDISH.search(urlparse(url).path + ("?" + urlparse(url).query if urlparse(url).query else "")) and url not in found and url.rstrip("/") != base.rstrip("/"):
            found.append(url)
    return found


def og_image(page: str, base: str) -> str | None:
    """The picture a story's page shares with social sites."""
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        a = _attrs(tag)
        if a.get("property", a.get("name", "")).lower() in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image") and _good_image(a.get("content")):
            return urljoin(base, a["content"])
    return None


def guesses(url: str) -> list[str]:
    """Feed addresses that sites use without saying so on the page."""
    p = urlparse(url)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("old.")
    path = p.path.rstrip("/")
    out: list[str] = []
    if host.endswith("reddit.com") and re.match(r"^/(r|user|u)/[^/]+", path) and not path.endswith(".rss"):
        out.append(f"https://www.reddit.com{path}/.rss")
    if host == "bsky.app" and re.match(r"^/profile/[^/]+$", path):
        out.append(f"https://bsky.app{path}/rss")
    if host.endswith("youtube.com"):
        m = re.match(r"^/channel/(UC[\w-]+)", path)
        if m:
            out.append(f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}")
        lst = parse_qs(p.query).get("list")
        if lst:
            out.append(f"https://www.youtube.com/feeds/videos.xml?playlist_id={lst[0]}")
    if re.match(r"^/@[\w.-]+$", path) and not host.endswith("youtube.com"):
        # Mastodon and other fediverse servers.
        out.append(f"{p.scheme}://{p.netloc}{path}.rss")
    root = f"{p.scheme}://{p.netloc}"
    out += [f"{root}{s}" for s in ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")]
    return out


async def read_up_to(stream: Any, limit: int) -> bytes:
    """The whole answer, up to limit bytes. A stream's read(n) gives only what has arrived so far, which cut big feeds
    (Global News, Ottawa Citizen) in the middle, so they looked like broken XML: "This address is not a feed"."""
    parts: list[bytes] = []
    size = 0
    async for chunk in stream.iter_chunked(64 * 1024):
        parts.append(chunk)
        size += len(chunk)
        if size >= limit:
            break
    return b"".join(parts)[:limit]


def closed_site(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    return any(host == c or host.endswith(f".{c}") for c in CLOSED)


def normalize(url: str) -> str:
    url = url.strip()
    if url and "://" not in url:
        url = f"https://{url}"
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise NewsError("no_feed", "This is not a web address.")
    return url


# ---------------------------------------------------------------- the store


@dataclass
class _Feed:
    url: str
    data: dict[str, Any] | None = None
    error: str | None = None
    read_at: float = 0
    tried_at: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:20]


class NewsStore:
    """Read feeds, kept in memory. Nothing is written to disk."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._feeds: dict[str, _Feed] = {}
        self._images: dict[str, tuple[str, bool]] = {}
        self._pages: OrderedDict[str, str | None] = OrderedDict()

    async def at_home(self, url: str) -> bool:
        """True when the address is in a home or private network (or names no host we can find)."""
        host = urlparse(url).hostname
        if not host:
            return True
        try:
            found = [host] if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]*:[0-9a-fA-F:.]*", host) else await resolve(self.hass, host)
            return not found or any(not ip_address(a.split("%")[0]).is_global for a in found)
        except (OSError, ValueError):
            return True

    async def _get(self, url: str, limit: int, wait: float = 15, public: bool = False) -> tuple[bytes, str, str]:
        """(body, content type, final address).

        public: the address came from a feed, not from the person (a story's page or picture). It, and every
        address it sends us on to, must be on the internet: a feed must not make Home Assistant call devices
        in the home network.
        """
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(wait):
                for _ in range(6):
                    if public and await self.at_home(url):
                        raise NewsError("not_reachable", "This address is in a home network.")
                    async with session.get(url, headers=HEADERS, timeout=ClientTimeout(total=wait), allow_redirects=not public) as resp:
                        if public and resp.status in REDIRECTS and resp.headers.get("Location"):
                            url = urljoin(str(resp.url), resp.headers["Location"])
                            continue
                        if resp.status >= 400:
                            raise NewsError("not_reachable", f"The site answered {resp.status}.")
                        body = await read_up_to(resp.content, limit)
                        return body, resp.headers.get("Content-Type", "").lower(), str(resp.url)
                raise NewsError("not_reachable", "The site sent us on too many times.")
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise NewsError("not_reachable", "The site did not answer in time.") from err
        except NewsError:
            raise
        except Exception as err:  # noqa: BLE001 - any network problem means the same to the user
            raise NewsError("not_reachable", f"Could not reach the site ({type(err).__name__}).") from err

    @staticmethod
    def _decode(body: bytes, ctype: str) -> str:
        m = re.search(r"charset=([\w-]+)", ctype)
        enc = m.group(1) if m else None
        if not enc:
            m2 = re.search(rb"""encoding=["']([\w-]+)["']""", body[:300]) or re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", body[:3000], re.I)
            enc = m2.group(1).decode() if m2 else "utf-8"
        try:
            return body.decode(enc, "replace")
        except LookupError:
            return body.decode("utf-8", "replace")

    async def _read(self, url: str, pictures: bool = True, wait: float = 15) -> dict[str, Any]:
        body, ctype, final = await self._get(url, MAX_FEED_BYTES, wait)
        try:
            data = await self.hass.async_add_executor_job(parse_feed, self._decode(body, ctype), final)
        except ValueError as err:
            raise NewsError("no_feed", "This address is not a feed.") from err
        # A feed the person chose from their own home network may also link pictures and pages there.
        data["home"] = await self.at_home(final)
        if pictures:
            await self._add_pictures(data)
        return data

    async def _add_pictures(self, data: dict[str, Any]) -> None:
        """Stories without a picture: look on the story's own page, for a few of the newest."""
        if data["kind"] in ("forum", "social"):
            return
        todo = [s for s in data["items"] if not s["image"] and s["link"]][:PAGE_LOOKUPS]
        sem = asyncio.Semaphore(3)

        async def one(story: dict[str, Any]) -> None:
            link = story["link"]
            if link in self._pages:
                story["image"] = self._pages[link]
                return
            async with sem:
                try:
                    body, ctype, final = await self._get(link, MAX_PAGE_BYTES, public=not data["home"])
                    found = og_image(self._decode(body, ctype), final)
                except NewsError:
                    found = None
            self._pages[link] = found
            while len(self._pages) > 3000:
                self._pages.popitem(last=False)
            story["image"] = found

        await asyncio.gather(*(one(s) for s in todo))

    async def async_feed(self, url: str) -> _Feed:
        """The feed, read again when it is older than 15 minutes."""
        feed = self._feeds.setdefault(url, _Feed(url))
        now = time.time()
        due = (now - feed.read_at > FRESH) and (now - feed.tried_at > (RETRY if feed.error else 0))
        if not due:
            return feed
        async with feed.lock:
            if time.time() - feed.tried_at < 5:
                return feed
            feed.tried_at = time.time()
            try:
                feed.data = await self._read(url)
                feed.error = None
                feed.read_at = time.time()
            except NewsError as err:
                # The last good stories stay on the wall; the studio shows the problem.
                feed.error = str(err)
                _LOGGER.debug("News feed %s: %s", url, err)
        return feed

    async def async_check(self, link: str) -> dict[str, Any]:
        """Find the feed behind any link: a feed address, a web page, a channel or a profile. At most CHECK_WAIT seconds."""
        url = normalize(link)
        if closed_site(url):
            raise NewsError("closed", "This site has no open feed.")
        try:
            async with asyncio.timeout(CHECK_WAIT):
                return await self._find(url)
        except TimeoutError as err:
            raise NewsError("not_reachable", "The site did not answer in time.") from err

    async def _find(self, url: str) -> dict[str, Any]:
        tried: set[str] = set()
        last: NewsError | None = None

        async def attempt(candidate: str) -> dict[str, Any] | None:
            nonlocal last
            if candidate in tried:
                return None
            tried.add(candidate)
            try:
                # No picture lookups here, so the studio hears back fast. The first real read adds them.
                feed = await self._read(candidate, pictures=False, wait=GUESS_WAIT)
            except NewsError as err:
                last = err
                return None
            return found(candidate, feed)

        def found(address: str, feed: dict[str, Any]) -> dict[str, Any] | None:
            if not feed["items"]:
                return None
            # Kept for the card, but due for a full read (with pictures) as soon as a screen asks.
            self._feeds[address] = _Feed(address, feed)
            return {"url": address, "title": feed["title"], "kind": feed["kind"], "count": len(feed["items"]), "latest": feed["items"][0]["title"]}

        async def first(candidates: list[str]) -> dict[str, Any] | None:
            """Tries them all at once; the first in the list that works wins."""
            for result in await asyncio.gather(*(attempt(c) for c in candidates)):
                if result:
                    return result
            return None

        everywhere = guesses(url)
        if result := await first(everywhere[:-6]):
            return result
        try:
            body, ctype, final = await self._get(url, MAX_FEED_BYTES)
        except NewsError as err:
            last = err
            body, ctype, final = b"", "", url
        if body:
            text = self._decode(body, ctype)
            try:
                feed = await self.hass.async_add_executor_job(parse_feed, text, final)
            except ValueError:
                if result := await first(find_feeds(text, final)[:4]):
                    return result
                # No <link> to a feed: ordinary links that look like feeds, such as a list of feeds or the footer's
                # "RSS" link. One page further at most (cbc.ca/news, then cbc.ca/rss/, then its first feed).
                links = feed_links(text, final)
                if result := await first(links[:4]):
                    return result
                for page in links[:2]:
                    try:
                        body2, ctype2, final2 = await self._get(page, MAX_FEED_BYTES, GUESS_WAIT)
                    except NewsError:
                        continue
                    if result := await first(feed_links(self._decode(body2, ctype2), final2)[:4]):
                        return result
            else:
                if result := found(final, feed):
                    return result
        if result := await first(everywhere[-6:]):
            return result
        if isinstance(last, NewsError) and last.code == "not_reachable" and len(tried) <= 1 and not body:
            raise last
        raise NewsError("no_feed", "No feed was found at this address.")

    def image_url(self, url: str | None, width: int, home: bool = False) -> str | None:
        if not url:
            return None
        key = _key(url)
        self._images[key] = (url, home)
        if len(self._images) > 5000:
            self._images.pop(next(iter(self._images)))
        width = min(WIDTHS, key=lambda w: abs(w - width))
        return async_sign_path(self.hass, f"{IMAGE_URL}?k={key}&w={width}", SIGN_FOR)

    def image_source(self, key: str) -> tuple[str, bool] | None:
        """The picture's address, and whether its feed is in the home network (then the picture may be too)."""
        return self._images.get(key)


def layout_feeds(hass: HomeAssistant) -> set[str]:
    """Every feed named in the published layout and in the studio's draft."""
    found: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for f in v.get("feeds") or []:
                if isinstance(f, dict) and isinstance(f.get("url"), str):
                    found.add(f["url"])
            for x in v.values():
                if isinstance(x, (dict, list)):
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    store = hass.data.get(DOMAIN)
    if store is not None:
        walk(getattr(store, "data", {}).get("layout"))
    drafts = hass.data.get(DRAFT)
    if drafts is not None:
        walk(getattr(drafts, "data", None))
    return found


# ---------------------------------------------------------------- websocket and pictures


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/news",
        vol.Required("feeds"): vol.All([str], vol.Length(max=30)),
        vol.Optional("width", default=960): vol.All(int, vol.Range(min=64, max=4000)),
    }
)
@websocket_api.async_response
async def ws_news(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The stories of these feeds. A tablet gets only feeds from the layout; the studio may preview any."""
    store: NewsStore = hass.data[NEWS]
    admin = bool(connection.user and connection.user.is_admin)
    allowed = layout_feeds(hass)
    urls = [u for u in dict.fromkeys(msg["feeds"]) if admin or u in allowed]
    feeds = await asyncio.gather(*(store.async_feed(u) for u in urls))
    out = []
    for f in feeds:
        d = f.data or {}
        out.append(
            {
                "url": f.url,
                "title": d.get("title", ""),
                "kind": d.get("kind", "news"),
                "error": f.error,
                "items": [
                    {
                        "id": s["id"],
                        "title": s["title"],
                        "summary": s["summary"],
                        "link": s["link"],
                        "published": int(s["published"] * 1000) if s["published"] else None,
                        "image": store.image_url(s["image"], msg["width"], d.get("home", False)),
                        "thumb": store.image_url(s["image"], 480, d.get("home", False)),
                    }
                    for s in d.get("items", [])
                ],
            }
        )
    connection.send_result(msg["id"], {"feeds": out})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/news/check", vol.Required("url"): str})
@websocket_api.async_response
async def ws_news_check(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The studio's "Add": finds the feed behind a link and says what it is."""
    store: NewsStore = hass.data[NEWS]
    try:
        connection.send_result(msg["id"], await store.async_check(msg["url"]))
    except NewsError as err:
        connection.send_error(msg["id"], err.code, str(err))


def _shrink(data: bytes, width: int) -> tuple[bytes, str]:
    from PIL import Image, ImageOps  # noqa: PLC0415 - Home Assistant ships Pillow; imported late.

    with Image.open(BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.width > width:
            img = img.resize((width, max(1, round(img.height * width / img.width))), Image.Resampling.LANCZOS)
        out = BytesIO()
        # A logo or drawing with a clear background stays clear (WebP keeps it), so the card's own color shows
        # behind it. Saved as JPEG, the clear parts would turn black.
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
            if img.getextrema()[3][0] < 255:
                img.save(out, "WEBP", quality=80, method=4)
                return out.getvalue(), "image/webp"
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(out, "JPEG", quality=80, optimize=True, progressive=True)
        return out.getvalue(), "image/jpeg"


class NewsImageView(HomeAssistantView):
    """A story's picture at the size the tablet asked for. The address comes signed with the stories."""

    url = IMAGE_URL
    name = "api:hadash:news_image"
    requires_auth = True

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, int], tuple[bytes, str]] = OrderedDict()

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        store: NewsStore = hass.data[NEWS]
        key = request.query.get("k", "")
        try:
            width = int(request.query.get("w", "960"))
        except ValueError:
            width = 960
        width = min(WIDTHS, key=lambda w: abs(w - width))
        source = store.image_source(key)
        if not source:
            return web.Response(status=404)
        hit = self._cache.get((key, width))
        if hit is None:
            try:
                address, home = source
                body, ctype, _ = await store._get(address, MAX_IMAGE_BYTES, public=not home)  # noqa: SLF001
                if not ctype.startswith("image/") and not body[:12].startswith((b"\xff\xd8", b"\x89PNG", b"RIFF", b"GIF8")):
                    return web.Response(status=404)
                # An SVG from a feed is never passed on: it can hold a script, and this address is Home Assistant's
                # own. A GIF becomes a small JPEG of its first frame like any photo: kept whole, a few large GIFs
                # would fill Home Assistant's memory, and a slow tablet would play them.
                if "svg" in ctype or body.lstrip()[:5].lower() in (b"<?xml", b"<svg "):
                    return web.Response(status=404)
                hit = await hass.async_add_executor_job(_shrink, body, width)
            except Exception:  # noqa: BLE001 - a missing picture only means the card shows its colour instead
                _LOGGER.debug("News picture %s failed", source[0], exc_info=True)
                return web.Response(status=404)
            self._cache[(key, width)] = hit
            while len(self._cache) > IMAGE_CACHE:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end((key, width))
        body, ctype = hit
        return web.Response(
            body=body, content_type=ctype, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}
        )


@callback
def async_register(hass: HomeAssistant) -> None:
    hass.data[NEWS] = NewsStore(hass)
    hass.http.register_view(NewsImageView())
    websocket_api.async_register_command(hass, ws_news)
    websocket_api.async_register_command(hass, ws_news_check)


__all__ = ["async_register", "find_feeds", "guesses", "og_image", "parse_feed", "quote"]
