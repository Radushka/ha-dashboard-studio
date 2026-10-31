"""Dashboard Studio Pro in Home Assistant: the license, the 14-day trial, and the monthly refund check.

The same DS1 license works in the Mac app and here. Home Assistant counts as one of the license's
places on the lukaps.com server, like a Mac does. Its "machine" is a hash of this Home Assistant's
own instance ID, so it stays the same after updates and restores from a backup.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import LICENSE_STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

PREFIX = "DS1"
ACTIVATION_PREFIX = "DSA1"
PRODUCT = "dashboardstudio"
MAJOR_VERSION = 1
# The public half of Dashboard Studio's key on the lukaps.com license server, the same as in the Mac app.
PUBLIC_KEY = "YYX8S7dhHMtnRW0Z5pr3TFv0vWuHAvmDXVc+skRd00Q="
SERVER = "https://lukaps.com/homeradar/api/"
TRIAL_DAYS = 14
RECHECK = timedelta(days=30)
# After a failed recheck (no internet, server down), try again about a day later.
RETRY = timedelta(days=1)


class LicenseProblem(Exception):
    """A license that cannot be used. The text is shown to the user as it is."""


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def signed_payload(text: str, prefix: str, public_key: str) -> dict[str, Any]:
    """Checks the shape and the Ed25519 signature, and returns the signed JSON."""
    parts = text.strip().split(".")
    if len(parts) != 3 or parts[0] != prefix:
        raise LicenseProblem("unreadable")
    try:
        payload, signature = _b64url(parts[1]), _b64url(parts[2])
    except ValueError as err:
        raise LicenseProblem("unreadable") from err
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(signature, payload)
    except (InvalidSignature, ValueError) as err:
        raise LicenseProblem("bad_signature") from err
    try:
        data = json.loads(payload)
    except ValueError as err:
        raise LicenseProblem("unreadable") from err
    if not isinstance(data, dict):
        raise LicenseProblem("unreadable")
    return data


MESSAGES = {
    "unreadable": "This does not look like a license. Paste the whole license, starting with DS1.",
    "home_radar": "This is a Home Radar license. Dashboard Studio needs its own license, starting with DS1.",
    "bad_signature": "This license is not valid. Please check that you pasted all of it.",
    "wrong_product": "This license is for another app.",
    "wrong_edition": "This license is not for Dashboard Studio Pro.",
    "older_version": "This license is for an older version of Dashboard Studio.",
    "network": "Could not reach lukaps.com. Please check the internet connection of Home Assistant and try again.",
}


def verify(text: str, public_key: str = PUBLIC_KEY) -> dict[str, Any]:
    """The license's JSON when it is a valid Dashboard Studio Pro license. Raises LicenseProblem with a message."""
    try:
        lic = signed_payload(text, PREFIX, public_key)
        if str(lic.get("p") or lic.get("product") or "").lower() != PRODUCT:
            raise LicenseProblem("wrong_product")
        if str(lic.get("ed", "")).lower() != "pro":
            raise LicenseProblem("wrong_edition")
        if int(lic.get("mv", 0)) < MAJOR_VERSION:
            raise LicenseProblem("older_version")
        if not lic.get("n"):
            raise LicenseProblem("unreadable")
    except LicenseProblem as err:
        code = str(err)
        if code == "unreadable" and text.strip().startswith("HR1."):
            code = "home_radar"
        raise LicenseProblem(MESSAGES.get(code, code)) from err
    return lic


class ProManager:
    """Pro for this Home Assistant: kept in .storage/hadash.license, so it is part of every backup."""

    def __init__(self, hass: HomeAssistant, public_key: str = PUBLIC_KEY, server: str = SERVER) -> None:
        self.hass = hass
        self.public_key = public_key
        self.server = server
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, LICENSE_STORAGE_KEY)
        self.data: dict[str, Any] = {}
        self.machine_id = ""
        self._listeners: set[Any] = set()

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or {}
        uuid = await instance_id.async_get(self.hass)
        # Like the Mac app's machine ID: lowercase hex SHA-256, here of the Home Assistant instance ID.
        self.machine_id = hashlib.sha256(f"homeassistant:{uuid}".encode()).hexdigest()

    @callback
    def async_start(self) -> callback:
        """Rechecks the license about every 30 days. Returns a function that stops it."""

        async def tick(_now: datetime | None = None) -> None:
            await self.async_recheck_if_due()

        self.hass.async_create_background_task(tick(), "hadash license check")
        return async_track_time_interval(self.hass, tick, timedelta(hours=6), cancel_on_shutdown=True)

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.data, 1)

    # MARK: State

    def license(self) -> dict[str, Any] | None:
        text = self.data.get("license")
        if not text:
            return None
        try:
            lic = verify(text, self.public_key)
        except LicenseProblem:
            return None
        return lic if self._activation_ok(self.data.get("activation"), lic) else None

    def _activation_ok(self, token: str | None, lic: dict[str, Any]) -> bool:
        if not token:
            return False
        try:
            a = signed_payload(token, ACTIVATION_PREFIX, self.public_key)
        except LicenseProblem:
            return False
        return a.get("n") == lic.get("n") and a.get("m") == self.machine_id

    def trial_days_left(self) -> int:
        """Whole calendar days. The latest date seen only moves forward, so setting the clock back gives nothing."""
        if self.data.get("trial_used"):
            # A license was used here once, so a refund or a removal does not start a new trial.
            return 0
        now = dt_util.now()
        start = dt_util.parse_datetime(self.data.get("trial_start", "")) or now
        seen = dt_util.parse_datetime(self.data.get("trial_seen", "")) or now
        seen = max(seen, now)
        if self.data.get("trial_start") is None or self.data.get("trial_seen") != seen.isoformat():
            self.data["trial_start"] = start.isoformat()
            self.data["trial_seen"] = seen.isoformat()
            self._save()
        used = (dt_util.as_local(seen).date() - dt_util.as_local(start).date()).days
        return max(TRIAL_DAYS - used, 0)

    def state(self) -> dict[str, Any]:
        """What the studio needs: plan pro / trial / free, days left, and who the license is for."""
        lic = self.license()
        if lic:
            return {"plan": "pro", "daysLeft": 0, "license": {"name": lic.get("name"), "email": lic.get("email"), "number": lic.get("n")}}
        left = self.trial_days_left()
        return {"plan": "trial" if left > 0 else "free", "daysLeft": left, "license": None}

    @callback
    def async_listen(self, listener) -> callback:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _changed(self) -> None:
        state = self.state()
        for listener in list(self._listeners):
            listener(state)

    # MARK: Activate and remove

    async def async_activate(self, text: str) -> dict[str, Any]:
        """Checks the license, turns it on for this Home Assistant, and keeps it. Raises LicenseProblem."""
        text = text.strip()
        verify(text, self.public_key)
        reply = await self._post("activate.php", {"license": text, "machine_id": self.machine_id, "machine_name": "Home Assistant"})
        if reply is None:
            raise LicenseProblem(MESSAGES["network"])
        token = reply.get("activation")
        if not reply.get("ok") or not isinstance(token, str):
            raise LicenseProblem(reply.get("message") or "The license could not be turned on.")
        lic = verify(text, self.public_key)
        if not self._activation_ok(token, lic):
            raise LicenseProblem("The license server gave an answer that does not fit this license.")
        self.data.update(license=text, activation=token, last_check=dt_util.utcnow().isoformat(), trial_used=True)
        self._save()
        self._changed()
        return self.state()

    async def async_remove(self) -> dict[str, Any]:
        """Turns the license off here, so its place can be used by a Mac or another Home Assistant."""
        text = self.data.get("license")
        if text:
            await self._post("deactivate.php", {"license": text, "machine_id": self.machine_id})
        for key in ("license", "activation", "last_check"):
            self.data.pop(key, None)
        self._save()
        self._changed()
        return self.state()

    async def async_recheck_if_due(self) -> None:
        """Only a "revoked" answer (a refund) turns Pro off. Any other failure tries again the next day."""
        text = self.data.get("license")
        if not text or not self.license():
            return
        last = dt_util.parse_datetime(self.data.get("last_check", "")) or datetime.min.replace(tzinfo=dt_util.UTC)
        if dt_util.utcnow() - last < RECHECK:
            return
        reply = await self._post("activate.php", {"license": text, "machine_id": self.machine_id, "machine_name": "Home Assistant"})
        if reply and reply.get("ok") and isinstance(reply.get("activation"), str):
            self.data.update(activation=reply["activation"], last_check=dt_util.utcnow().isoformat())
        elif reply and reply.get("error") == "revoked":
            _LOGGER.warning("Dashboard Studio: the license was refunded or turned off, so Pro is off again")
            for key in ("license", "activation", "last_check"):
                self.data.pop(key, None)
            self._changed()
        else:
            self.data["last_check"] = (dt_util.utcnow() - RECHECK + RETRY).isoformat()
        self._save()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(self.server + path, json=body, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        return data if isinstance(data, dict) else None
