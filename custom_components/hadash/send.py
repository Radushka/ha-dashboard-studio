"""Send a YouTube video to a screen (Pro): share it to the Home Assistant app, and it plays on a wall tablet or a TV.

The Home Assistant app is in every share sheet already, so there is nothing to set up (see the end of this file).
A secret webhook also takes {url, screen}, for automations and scripts: GET lists the screens, POST plays. Its
address is kept in .storage and shown to admins only.

The video plays inside the dashboard, in the watch view (YouTube's own player), never in another app. Back returns.
A TV set up with "Add a Fire TV" is also woken over adb and the wall app is brought to the front, so the video shows
even when the TV was dark or in another app. A tablet that connects again within a minute still gets the video.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from aiohttp import web

from homeassistant.components import webhook, websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PRO, TVS
from .voice import VOICE, Voice, Wall, plain

_LOGGER = logging.getLogger(__name__)

SEND = "hadash_send"
SEND_STORAGE_KEY = "hadash.send"
# How long a tablet has to say "playing", and how long a video waits for a screen that is coming back.
ANSWER_WAIT = 6.0
PENDING_FOR = 90.0
# A video shared from a phone waits this long for "Where?": a touch on a screen, or a button on the phone.
CATCH_FOR = 20.0

VIDEO_ID = re.compile(r"^[\w-]{6,20}$")
LINK = re.compile(r"https?://\S+")

TEXT = {
    "pro": {
        "en": "Send to screen is part of Dashboard Studio Pro.",
        "fr": "Envoyer à l'écran fait partie de Dashboard Studio Pro.",
        "de": "An Bildschirm senden gehört zu Dashboard Studio Pro.",
        "es": "Enviar a la pantalla es parte de Dashboard Studio Pro.",
    },
    "no_video": {
        "en": "This is not a YouTube video link.",
        "fr": "Ce n'est pas un lien de vidéo YouTube.",
        "de": "Das ist kein Link zu einem YouTube-Video.",
        "es": "Esto no es un enlace de un video de YouTube.",
    },
    "nowhere": {
        "en": "There is no screen called {where}.",
        "fr": "Il n'y a pas d'écran nommé {where}.",
        "de": "Es gibt keinen Bildschirm namens {where}.",
        "es": "No hay ninguna pantalla llamada {where}.",
    },
    "off": {
        "en": "{where} is not on right now. Turn it on, then send the video again.",
        "fr": "{where} n'est pas allumé. Allumez-le, puis envoyez la vidéo à nouveau.",
        "de": "{where} ist gerade nicht an. Schalten Sie es ein und senden Sie das Video noch einmal.",
        "es": "{where} no está encendida. Enciéndala y envíe el video otra vez.",
    },
    "waking": {
        "en": "Waking {where}. The video starts in a moment.",
        "fr": "{where} se réveille. La vidéo commence dans un instant.",
        "de": "{where} wird geweckt. Das Video startet gleich.",
        "es": "Despertando {where}. El video empieza en un momento.",
    },
    "title": {
        "en": "Send to screen",
        "fr": "Envoyer à l'écran",
        "de": "An Bildschirm senden",
        "es": "Enviar a la pantalla",
    },
    "where": {
        "en": "Where should it play? Touch a screen, or pick one here. Otherwise it plays on {where} in 20 seconds.",
        "fr": "Où la lire ? Touchez un écran, ou choisissez-en un ici. Sinon, elle passe sur {where} dans 20 secondes.",
        "de": "Wo soll es laufen? Tippen Sie auf einen Bildschirm oder wählen Sie hier einen. Sonst läuft es in 20 Sekunden auf {where}.",
        "es": "¿Dónde se reproduce? Toque una pantalla o elija una aquí. Si no, se reproduce en {where} en 20 segundos.",
    },
    "playing": {
        "en": "Playing on {where}.",
        "fr": "Lecture sur {where}.",
        "de": "Läuft auf {where}.",
        "es": "Reproduciendo en {where}.",
    },
}


def text(hass: HomeAssistant, key: str, **args: str) -> str:
    lang = (hass.config.language or "en").split("-")[0].lower()
    return TEXT[key].get(lang, TEXT[key]["en"]).format(**args)


def video_from(shared: str) -> dict[str, Any] | None:
    """The video in a shared link or text: {id, start}. The YouTube app shares "https://youtu.be/<id>?si=...".

    Same rules as videoId() in the renderer (lib/youtube.svelte.ts): watch, youtu.be, shorts, live and embed links."""
    for link in LINK.findall(shared or ""):
        u = urlparse(link.rstrip(".,;)\"'>"))
        host = (u.hostname or "").lower()
        host = re.sub(r"^(www|m|music)\.", "", host)
        q = parse_qs(u.query)
        vid: str | None = None
        if host == "youtu.be":
            vid = u.path.lstrip("/").split("/")[0]
        elif host in ("youtube.com", "youtube-nocookie.com"):
            vid = (q.get("v") or [None])[0]
            if not vid:
                m = re.match(r"^/(?:shorts|live|embed|v)/([\w-]+)", u.path)
                vid = m.group(1) if m else None
        if vid and VIDEO_ID.match(vid):
            start = 0
            t = (q.get("t") or q.get("start") or ["0"])[0]
            m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", t)
            if m:
                h, mi, s = (int(x) if x else 0 for x in m.groups())
                start = h * 3600 + mi * 60 + s
            return {"id": vid, "start": start}
    return None


class Sender:
    """Keeps the secret webhook, the screens' order, and videos waiting for a screen that is waking up."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, 1, SEND_STORAGE_KEY)
        self.webhook_id = ""
        self.last: list[str] = []
        self.pending: dict[str, tuple[dict[str, Any], float]] = {}
        # The last link shared from a phone, so the notification's buttons can move it to another screen.
        self.recent = ""
        # A shared video that waits for "Where?": {id, link, service, cancel}. None when nothing waits.
        self.offer: dict[str, Any] | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.webhook_id = data.get("webhook") or ""
        self.last = [s for s in data.get("last", []) if isinstance(s, str)]
        if not self.webhook_id:
            await self.async_new_webhook(register=False)

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"webhook": self.webhook_id, "last": self.last[:8]}, 1)

    async def async_new_webhook(self, register: bool = True) -> str:
        """A new secret address. The old one stops working at once."""
        if register and self.webhook_id:
            webhook.async_unregister(self.hass, self.webhook_id)
        self.webhook_id = f"hadash_send_{secrets.token_urlsafe(24)}"
        await self._store.async_save({"webhook": self.webhook_id, "last": self.last[:8]})
        if register:
            self.register()
        return self.webhook_id

    @callback
    def register(self) -> None:
        webhook.async_register(
            self.hass, DOMAIN, "Dashboard Studio: send to screen", self.webhook_id, handle, local_only=False, allowed_methods=["GET", "POST"]
        )

    def address(self) -> str:
        """The full webhook address: the Home Assistant Cloud or external one when there is one, so it works anywhere."""
        for kwargs in ({"prefer_external": True, "allow_cloud": True}, {"allow_external": False}):
            try:
                base = get_url(self.hass, **kwargs)
                return f"{base}{webhook.async_generate_path(self.webhook_id)}"
            except NoURLAvailableError:
                continue
        return webhook.async_generate_path(self.webhook_id)

    # ---- Screens ----

    def screens(self) -> list[dict[str, Any]]:
        """Every screen of the published layout: [{id, name, tv, on}]. TVs first, then the ones used last."""
        layout = self.hass.data[DOMAIN].layout or {}
        voice: Voice = self.hass.data[VOICE]
        walls = list(voice.walls.values())
        tvs: dict[str, dict[str, Any]] = self.hass.data[TVS].tvs
        found: list[dict[str, Any]] = [{"id": "", "name": str(layout.get("title") or "Home")}]
        for s in layout.get("screens") or []:
            if isinstance(s, dict) and s.get("id"):
                found.append({"id": str(s["id"]), "name": str(s.get("name") or s["id"])})
        for s in found:
            mine = [w for w in walls if w.screen == s["id"]]
            s["tv"] = any(w.tv for w in mine) or any(t.get("screen") == s["id"] for t in tvs.values())
            s["on"] = bool(mine)
        # A main screen that no tablet shows and no TV uses is only a name: leave it out when there are others.
        if len(found) > 1 and not found[0]["on"] and not found[0]["tv"]:
            found = found[1:]
        rank = {sid: i for i, sid in enumerate(self.last)}
        found.sort(key=lambda s: (not s["tv"], rank.get(s["id"], 99), not s["on"]))
        return found

    def find(self, where: str) -> dict[str, Any] | None:
        want = plain(where)
        screens = self.screens()
        return next((s for s in screens if plain(s["name"]) == want or s["id"] == where), None) or next(
            (s for s in screens if want and (want in plain(s["name"]) or plain(s["name"]) in want)), None
        )

    # ---- Sending ----

    async def send(self, where: str, shared: str) -> str:
        hass = self.hass
        if hass.data[PRO].state()["plan"] == "free":
            return text(hass, "pro")
        video = video_from(shared)
        if not video:
            return text(hass, "no_video")
        screens = self.screens()
        # No screen named: the first of the list (TVs first, then the one used last).
        screen = self.find(where) if where else (screens[0] if screens else None)
        if screen is None:
            return text(hass, "nowhere", where=where or "?")
        self.last = [screen["id"], *[s for s in self.last if s != screen["id"]]]
        self._save()
        video.update(await self._details(video["id"]))

        voice: Voice = hass.data[VOICE]
        walls = [w for w in voice.walls.values() if w.screen == screen["id"]]
        hosts = [h for h, tv in hass.data[TVS].tvs.items() if tv.get("screen") == screen["id"]]
        # The TV comes to the front first (it may be dark, or showing another app): the video must be seen there.
        for host in hosts:
            hass.async_create_background_task(self._wake_tv(host), f"hadash wake {host}")
        answer = await self._deliver(voice, walls, video) if walls else None
        if answer:
            return answer
        if hosts:
            # The TV's page connects again in a moment and gets the video then.
            self.pending[screen["id"]] = ({"action": "video", **video}, time.monotonic())
            return text(hass, "waking", where=screen["name"])
        return text(hass, "off", where=screen["name"])

    # ---- "Where?": share, then touch the screen you want, or pick it on the phone ----

    async def offer_video(self, link: str, service: str | None) -> None:
        """Every screen glows for a moment, and the phone asks "Where?". A touch on a screen (OK on a TV remote) or a
        button on the phone plays it there. With no answer, it plays on the first screen, as before."""
        hass = self.hass
        video = video_from(link)
        screens = self.screens()
        if hass.data[PRO].state()["plan"] == "free" or not video or len(screens) < 2:
            # Nothing to choose: it plays at once (or the phone hears why not).
            reply = await self.send("", link)
            await _tell_phone(hass, self, service, reply, reply not in {text(hass, "pro"), text(hass, "no_video")})
            return
        self._end_offer()
        offer_id = secrets.token_hex(6)
        self.recent = link
        self.offer = {"id": offer_id, "link": link, "service": service, "cancel": None}
        details = await self._details(video["id"])
        if not self.offer or self.offer["id"] != offer_id:
            return
        voice: Voice = hass.data[VOICE]
        glow = {"action": "catch", "offer": offer_id, "id": video["id"], "seconds": CATCH_FOR, **details}
        for wall in list(voice.walls.values()):
            wall.send(glow)

        @callback
        def no_answer(_now: Any) -> None:
            hass.async_create_task(self.answer_offer(offer_id, None))

        self.offer["cancel"] = async_call_later(hass, CATCH_FOR, no_answer)
        await _ask_phone(hass, service, text(hass, "where", where=screens[0]["name"]), screens)

    def _end_offer(self) -> dict[str, Any] | None:
        """Stops the wait: the timer ends and every screen's glow fades."""
        offer, self.offer = self.offer, None
        if offer is None:
            return None
        if offer["cancel"]:
            offer["cancel"]()
        voice: Voice = self.hass.data[VOICE]
        for wall in list(voice.walls.values()):
            wall.send({"action": "catch_end", "offer": offer["id"]})
        return offer

    async def answer_offer(self, offer_id: str, screen_id: str | None) -> bool:
        """The answer to "Where?": a screen's id, or None for the first screen. False when nothing waits any more."""
        if not self.offer or self.offer["id"] != offer_id:
            return False
        offer = self._end_offer()
        assert offer is not None
        where = ""
        if screen_id is not None:
            where = next((s["name"] for s in self.screens() if s["id"] == screen_id), screen_id) or screen_id
        reply = await self.send(where, offer["link"])
        await _tell_phone(self.hass, self, offer["service"], reply, True)
        return True

    async def _deliver(self, voice: Voice, walls: list[Wall], video: dict[str, Any]) -> str | None:
        """Sends to the screen's tablets; the first one that answers gives the reply."""
        return await self._deliver_command(voice, walls, {"action": "video", **video})

    async def _deliver_command(self, voice: Voice, walls: list[Wall], command: dict[str, Any]) -> str | None:
        """Sends any command to the screen's tablets (a video, or a card thrown from a tablet)."""
        request = next(voice._ids)
        answer: asyncio.Future[str] = self.hass.loop.create_future()
        voice.answers[request] = answer
        try:
            for wall in walls:
                wall.send({**command, "request": request})
            async with asyncio.timeout(ANSWER_WAIT):
                return await answer
        except TimeoutError:
            return None
        finally:
            voice.answers.pop(request, None)

    @callback
    def joined(self, wall: Wall) -> None:
        """A tablet started listening (the TV's page after a wake): it gets what waits for its screen (a video or a card)."""
        waiting = self.pending.pop(wall.screen, None)
        if waiting and time.monotonic() - waiting[1] < PENDING_FOR:
            wall.send(waiting[0])

    async def _details(self, vid: str) -> dict[str, Any]:
        """The title and channel, from YouTube's public oEmbed (no key). Optional: the video plays without them."""
        try:
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(4):
                r = await session.get(
                    "https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}
                )
                if r.status != 200:
                    return {}
                data = await r.json(content_type=None)
            return {"title": str(data.get("title") or "")[:200], "author": str(data.get("author_name") or "")[:80]}
        except Exception:  # noqa: BLE001
            return {}

    async def _wake_tv(self, host: str) -> None:
        from .firetv import _signer
        from .tvsetup import bring_forward

        try:
            await bring_forward(host, await _signer(self.hass))
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Send to screen: could not wake the TV at %s: %s", host, err)


async def handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
    """GET: the screens' names, one per line (the Shortcut splits them for "Choose from List"), or as JSON with
    ?json. POST: {url, screen} starts the video and answers in a sentence that the Shortcut shows."""
    sender: Sender = hass.data[SEND]
    if request.method == "GET":
        names = [s["name"].replace("\n", " ") for s in sender.screens()]
        if "json" in request.query:
            return web.json_response(names)
        return web.Response(text="\n".join(names), content_type="text/plain")
    data: dict[str, Any] = {}
    try:
        if request.content_type == "application/json":
            body = await request.json()
            data = body if isinstance(body, dict) else {}
        else:
            data = dict(await request.post())
    except ValueError:
        data = {}
    data = {**request.query, **data}
    shared = str(data.get("url") or data.get("link") or data.get("text") or "")
    where = str(data.get("screen") or "").strip()
    reply = await sender.send(where, shared)
    return web.Response(text=reply, content_type="text/plain")


# ---- The studio: show the address, or make a new one ----


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hadash/send/info", vol.Optional("renew", default=False): bool})
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    sender: Sender = hass.data[SEND]
    if msg["renew"]:
        await sender.async_new_webhook()
    connection.send_result(msg["id"], {"address": sender.address(), "screens": sender.screens()})


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/send/catch", vol.Required("offer"): str, vol.Optional("screen", default=""): str}
)
@websocket_api.async_response
async def ws_catch(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A screen was touched while it glowed: the shared video plays there. Works for non-admin logins, like the layout."""
    sender: Sender = hass.data[SEND]
    caught = await sender.answer_offer(msg["offer"], msg["screen"])
    connection.send_result(msg["id"], {"caught": caught})


# ---- The Home Assistant app's own Share target: no setup at all ----
# The Home Assistant app (iPhone, iPad, Mac, Android) is in every share sheet as "Home Assistant". Sharing fires
# mobile_app.share with the link. A screen's name typed in the share box plays it there at once. Otherwise the phone
# asks "Where?" with a button for each screen, and every screen glows: touch the one you stand at (or press OK on the
# TV remote). With no answer in 20 seconds it plays on the first screen (TVs first, then the one used last). The
# phone then gets "Playing on Kitchen TV", whose buttons move the video to another screen with one tap.
# https://companion.home-assistant.io/docs/integrations/sharing

SHARE_EVENT = "mobile_app.share"
ACTION_EVENT = "mobile_app_notification_action"
ACTION_PREFIX = "HADASH_SEND:"
NOTE_TAG = "hadash-send"


def _notify_service(hass: HomeAssistant, data: dict[str, Any], user_id: str | None = None) -> str | None:
    """The notify service of the phone that shared ("mobile_app_radus_iphone"), when it can be found.

    The iPhone app sends its own device ID ("radus_iphone") and name ("Radu's iPhone"); Android sends device_id.
    These do not always match Home Assistant's slug of the name ("radu_s_iphone"), so the match ignores
    underscores and spaces, then tries the name, and last the person who shared when they have only one phone."""
    from homeassistant.util import slugify

    def key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    ids = {key(data.get(k)) for k in ("sourceDeviceID", "sourceDevicePermanentID", "device_id", "sourceDeviceName") if data.get(k)}
    phones = []
    for entry in hass.config_entries.async_entries("mobile_app"):
        d = entry.data
        name = str(d.get("device_name") or "")
        service = f"mobile_app_{slugify(name)}"
        if not name or not hass.services.has_service("notify", service):
            continue
        phones.append((service, d))
        if ids & {key(d.get("device_id")), key(name), key(entry.entry_id), key(d.get("webhook_id"))}:
            return service
    mine = [service for service, d in phones if user_id and d.get("user_id") == user_id]
    if len(mine) == 1:
        return mine[0]
    _LOGGER.info("Send to screen: no phone matched %s (phones: %s)", sorted(ids), [s for s, _ in phones])
    return None


async def _tell_phone(hass: HomeAssistant, sender: "Sender", service: str | None, reply: str, played: bool) -> None:
    if not service:
        return
    data: dict[str, Any] = {"tag": NOTE_TAG, "group": NOTE_TAG}
    if played:
        here = sender.last[0] if sender.last else None
        others = [s for s in sender.screens() if s["id"] != here][:4]
        if others:
            data["actions"] = [{"action": f"{ACTION_PREFIX}{s['id']}", "title": s["name"]} for s in others]
    try:
        await hass.services.async_call("notify", service, {"title": text(hass, "title"), "message": reply, "data": data})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Send to screen: could not notify %s", service, exc_info=True)


async def _ask_phone(hass: HomeAssistant, service: str | None, message: str, screens: list[dict[str, Any]]) -> None:
    """ "Where should it play?" with a button for each screen (Android shows the first three)."""
    if not service:
        return
    actions = [{"action": f"{ACTION_PREFIX}{s['id']}", "title": s["name"]} for s in screens[:4]]
    data = {"tag": NOTE_TAG, "group": NOTE_TAG, "actions": actions}
    try:
        await hass.services.async_call("notify", service, {"title": text(hass, "title"), "message": message, "data": data})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Send to screen: could not notify %s", service, exc_info=True)


async def async_setup(hass: HomeAssistant) -> None:
    sender = hass.data[SEND] = Sender(hass)
    await sender.async_load()
    sender.register()
    websocket_api.async_register_command(hass, ws_info)
    websocket_api.async_register_command(hass, ws_catch)

    async def shared(event: Any) -> None:
        data = dict(event.data)
        link = str(data.get("url") or data.get("text") or "")
        # Only YouTube videos. Other shares stay for the person's own automations.
        if not video_from(link):
            return
        entered = str(data.get("entered") or "").strip()
        service = _notify_service(hass, data, event.context.user_id)
        if not (entered and sender.find(entered)):
            await sender.offer_video(link, service)
            return
        sender.recent = link
        reply = await sender.send(entered, link)
        played = reply not in {text(hass, "pro"), text(hass, "no_video")}
        await _tell_phone(hass, sender, service, reply, played)

    async def moved(event: Any) -> None:
        action = str(event.data.get("action") or "")
        if not action.startswith(ACTION_PREFIX) or not sender.recent:
            return
        screen = action[len(ACTION_PREFIX):]
        # The answer to "Where?".
        if sender.offer and await sender.answer_offer(sender.offer["id"], screen):
            return
        reply = await sender.send(screen or next((s["name"] for s in sender.screens() if s["id"] == ""), "Home"), sender.recent)
        await _tell_phone(hass, sender, _notify_service(hass, dict(event.data), event.context.user_id), reply, True)

    hass.bus.async_listen(SHARE_EVENT, shared)
    hass.bus.async_listen(ACTION_EVENT, moved)

    @callback
    def stopping(_event: Any) -> None:
        sender._end_offer()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping)


__all__ = ["SEND", "Sender", "async_setup", "video_from"]
