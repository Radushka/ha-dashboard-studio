"""The studio's and the tablets' pages, always fresh.

The asset files have a hash in their names, so they can be kept. The pages that name them cannot:
without a Cache-Control header a browser may keep an old studio.html for hours ("heuristic" caching).
After an update that old page asks for asset files the update removed, and the studio stays blank.
So the pages (and the small JSON files read at start) are served here with "no-cache": the browser
may keep a copy, but asks Home Assistant first each time, and gets a quick "not modified" when nothing changed.
"""

from __future__ import annotations

from pathlib import Path
import re

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

from .const import URL_BASE, WWW

# A plain file name: no folders, no "..".
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:html|json|webmanifest)$")
_FOLDERS = ("", "wall")


class FreshPageView(HomeAssistantView):
    """/hadash/<page>.html and /hadash/wall/<page>.html (and .json), sent with Cache-Control: no-cache."""

    url = f"{URL_BASE}/{{name:[^/]+\\.(?:html|json|webmanifest)}}"
    extra_urls = [f"{URL_BASE}/wall/{{name:[^/]+\\.(?:html|json|webmanifest)}}"]
    name = "hadash:page"
    # Like the other files here: the pages sign in to Home Assistant themselves.
    requires_auth = False

    def __init__(self, root: Path = WWW) -> None:
        self._root = root

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        folder = "wall" if request.path.startswith(f"{URL_BASE}/wall/") else ""
        if folder not in _FOLDERS or not _NAME.match(name):
            raise web.HTTPNotFound
        path = self._root / folder / name if folder else self._root / name
        hass = request.app["hass"]
        if not await hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
