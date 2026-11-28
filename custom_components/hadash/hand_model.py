"""Google's hand gesture model for the wall tablets, kept once in Home Assistant.

Hand gestures are Pro and most homes never turn them on, so the 8 MB model is not inside the wall app.
The first tablet that needs it asks Home Assistant at /hadash/hands/gesture_recognizer.task. Home Assistant
fetches it once from Google (Apache 2.0), checks it against the known fingerprint, keeps it in
/config/hadash/hands, and hands it to every tablet from then on. The tablets keep their own copy too.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import URL_BASE

_LOGGER = logging.getLogger(__name__)

NAME = "gesture_recognizer.task"
SOURCE = "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/" + NAME
SHA256 = "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"
SIZE = 8373440


def _good(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != SIZE:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == SHA256


def _keep(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    part.write_bytes(data)
    part.replace(path)


class HandModel:
    """The one copy of the model in Home Assistant, fetched the first time a tablet asks."""

    def __init__(self, hass: HomeAssistant, folder: Path) -> None:
        self._hass = hass
        self.path = folder / NAME
        self._lock = asyncio.Lock()
        self._checked = False

    async def ensure(self) -> bool:
        async with self._lock:
            if self._checked or await self._hass.async_add_executor_job(_good, self.path):
                self._checked = True
                return True
            try:
                async with async_get_clientsession(self._hass).get(SOURCE, timeout=120) as r:
                    r.raise_for_status()
                    data = await r.read()
            except Exception as err:  # noqa: BLE001 - any failure: the tablet tries again later
                _LOGGER.warning("Could not fetch the hand gesture model: %s", err)
                return False
            if len(data) != SIZE or hashlib.sha256(data).hexdigest() != SHA256:
                _LOGGER.warning("The hand gesture model from Google did not match its fingerprint; not kept")
                return False
            await self._hass.async_add_executor_job(_keep, self.path, data)
            self._checked = True
            _LOGGER.info("Hand gesture model kept in %s", self.path)
            return True


class HandModelView(HomeAssistantView):
    """/hadash/hands/gesture_recognizer.task for the wall app (no sign-in, like the wall app's own download)."""

    url = f"{URL_BASE}/hands/{NAME}"
    name = "hadash:hand_model"
    requires_auth = False

    def __init__(self, model: HandModel) -> None:
        self._model = model

    async def get(self, request: web.Request) -> web.StreamResponse:
        if not await self._model.ensure():
            raise web.HTTPServiceUnavailable(text="The hand model is not here yet. Try again in a minute.")
        return web.FileResponse(self._model.path, headers={"Cache-Control": "max-age=31536000, immutable"})


def async_setup_hand_model(hass: HomeAssistant) -> None:
    hass.http.register_view(HandModelView(HandModel(hass, Path(hass.config.path("hadash", "hands")))))
