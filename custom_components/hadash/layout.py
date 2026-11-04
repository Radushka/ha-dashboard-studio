"""The published layout, kept in Home Assistant's storage (.storage/hadash.layout)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import HISTORY_KEEP, HISTORY_STORAGE_KEY, STORAGE_KEY, STORAGE_VERSION


class LayoutStore:
    """Holds the layout and tells every open tablet when a new one is published."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = {}
        # Kept in a file of its own, so the layout file that tablets read stays small.
        self._history_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, HISTORY_STORAGE_KEY)
        self.history: list[dict[str, Any]] = []
        self._listeners: set[Callable[[dict[str, Any]], None]] = set()

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or {}
        self.history = ((await self._history_store.async_load()) or {}).get("versions", [])
        # Installed before history existed: the layout that is live now is the first version.
        if not self.history and self.layout:
            self.history = [self._version(self.data)]

    @property
    def layout(self) -> dict[str, Any] | None:
        return self.data.get("layout")

    async def async_publish(self, layout: dict[str, Any], user: str | None) -> dict[str, Any]:
        """Save a new layout and send it to everyone listening. The revision tells tablets it changed."""
        self.data = {
            "layout": layout,
            "revision": self.data.get("revision", 0) + 1,
            "published_at": dt_util.utcnow().isoformat(),
            "published_by": user,
        }
        await self._store.async_save(self.data)
        # Tablets get the new layout as soon as it is saved. The history is written after, so it never delays them.
        for listener in list(self._listeners):
            listener(self.data)
        self.history = [self._version(self.data), *self.history][:HISTORY_KEEP]
        await self._history_store.async_save({"versions": self.history})
        return self.data

    @staticmethod
    def _version(data: dict[str, Any]) -> dict[str, Any]:
        return {k: data.get(k) for k in ("layout", "revision", "published_at", "published_by")}

    def versions(self) -> list[dict[str, Any]]:
        """The kept versions, newest first, without their layouts: enough to list them."""
        out = []
        for v in self.history:
            layout = v.get("layout") or {}
            out.append(
                {
                    "revision": v.get("revision"),
                    "published_at": v.get("published_at"),
                    "published_by": v.get("published_by"),
                    "cards": len(layout.get("widgets") or []),
                    "screens": len(layout.get("screens") or []),
                }
            )
        return out

    def version(self, revision: int) -> dict[str, Any] | None:
        return next((v for v in self.history if v.get("revision") == revision), None)

    @callback
    def async_listen(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)
