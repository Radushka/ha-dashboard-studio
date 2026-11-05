"""The studio's unpublished changes, kept in Home Assistant too, so an AI assistant works on what the person sees.

The studio sends its draft here a moment after each change. An assistant (Claude through HA's MCP Server, or
the studio's own Ask box) reads the same draft, changes it, and the studio shows the change at once as one undo
step. Nothing reaches the tablets until the person presses Publish.

Each change by the assistant raises "change". The studio says which change it has seen, so a late save from an
older studio state can never wipe out what the assistant just did.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DRAFT_STORAGE_KEY, STORAGE_VERSION

BY_STUDIO = "studio"
BY_ASSISTANT = "assistant"


class DraftStore:
    """Holds the draft: the whole project (main layout with its screens), and what it was based on."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, DRAFT_STORAGE_KEY)
        self.data: dict[str, Any] = {}
        self._listeners: set[Callable[[dict[str, Any]], None]] = set()

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or {}

    @property
    def change(self) -> int:
        return int(self.data.get("change", 0))

    def project_for(self, revision: int) -> dict[str, Any] | None:
        """The draft, when it was made on top of the version that is live now. Otherwise it is out of date."""
        if self.data.get("project") and self.data.get("base") == revision:
            return copy.deepcopy(self.data["project"])
        return None

    def public(self) -> dict[str, Any]:
        return {k: self.data.get(k) for k in ("project", "base", "change", "by", "note", "updated")}

    def save_from_studio(self, project: dict[str, Any], base: int, seen: int) -> bool:
        """The studio's own change. Refused when the assistant changed the draft after the studio last looked."""
        if self.data.get("by") == BY_ASSISTANT and seen < self.change and self.data.get("base") == base:
            return False
        self.data = {
            "project": project,
            "base": base,
            "change": self.change,
            "by": BY_STUDIO,
            "note": None,
            "updated": dt_util.utcnow().isoformat(),
        }
        self._store.async_delay_save(lambda: self.data, 2)
        return True

    def save_from_assistant(self, project: dict[str, Any], base: int, note: str) -> dict[str, Any]:
        """A change by the assistant. Every open studio gets it at once."""
        self.data = {
            "project": project,
            "base": base,
            "change": self.change + 1,
            "by": BY_ASSISTANT,
            "note": note,
            "updated": dt_util.utcnow().isoformat(),
        }
        self._store.async_delay_save(lambda: self.data, 1)
        for listener in list(self._listeners):
            listener(self.public())
        return self.data

    def clear(self) -> None:
        """After a publish the draft is the live layout, so there is nothing left to keep."""
        self.data = {"change": self.change}
        self._store.async_delay_save(lambda: self.data, 1)

    @callback
    def async_listen(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)
