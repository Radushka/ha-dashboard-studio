"""Talk to the wall: Assist sentences that reach a wall screen from any Assist device (Pro).

Spoken at a wall tablet, "show me the front door" is handled by the tablet itself (renderer lib/voiceCommands.ts),
so it never takes a sentence away from other Assist devices. Here are only the sentences that name a screen:
"show the front door on the kitchen tablet", "turn off the kitchen screen". They work from a phone, a voice
satellite or another tablet. Each tablet tells Home Assistant which screen it shows; the sentence goes to the
tablets of the named screen, and the first one to answer gives the spoken reply.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import PRO
from .license import ProManager

_LOGGER = logging.getLogger(__name__)

VOICE = "hadash_voice"
# How long a spoken reply waits for the tablet to say what it did.
ANSWER_WAIT = 4.0

SENTENCES: dict[str, list[str]] = {
    "show": [
        "(show|put) [me] [the] {what} on [the] {where}",
        "(affiche|montre|montre moi|mets) {what} sur [le |la |l'] {where}",
        "(zeig|zeige) [mir] {what} auf [dem |der |den ] {where}",
        "(muestra|muéstrame|muestrame|pon) {what} en [el |la ] {where}",
    ],
    "off": [
        "turn off [the] {where} screen",
        "turn [the] {where} screen off",
        "(éteins|eteins) l'écran (de |du |de la ){where}",
        "schalte [den] bildschirm (im |in der |auf dem ){where} aus",
        "apaga la pantalla (de |del |de la ){where}",
    ],
}

TEXT = {
    "pro": {
        "en": "Voice commands for the wall are part of Dashboard Studio Pro.",
        "fr": "Les commandes vocales pour l'écran mural font partie de Dashboard Studio Pro.",
        "de": "Sprachbefehle für das Wanddisplay gehören zu Dashboard Studio Pro.",
        "es": "Los comandos de voz para la pantalla de pared son parte de Dashboard Studio Pro.",
    },
    "nowhere": {
        "en": "I could not find a wall screen called {where}.",
        "fr": "Je ne trouve pas d'écran mural nommé {where}.",
        "de": "Ich finde kein Wanddisplay namens {where}.",
        "es": "No encuentro una pantalla de pared llamada {where}.",
    },
    "sent": {
        "en": "On the {where} screen.",
        "fr": "Sur l'écran {where}.",
        "de": "Auf dem Display {where}.",
        "es": "En la pantalla {where}.",
    },
}

# Words around a screen's name: "the kitchen tablet", "la tablette de la cuisine".
DEVICE_WORDS = {
    "tablet", "tablets", "screen", "screens", "display", "wall", "dashboard", "panel",
    "tablette", "ecran", "mural", "de", "du", "la", "le", "l",
    "bildschirm", "display", "wanddisplay", "im", "in", "der", "dem",
    "tableta", "pantalla", "del", "el",
    "the", "on",
}
TV_WORDS = {"tv", "television", "telly", "tele", "televiseur", "fernseher", "televisor", "televisión", "television"}


def text(key: str, language: str | None, **args: str) -> str:
    lang = (language or "en").split("-")[0].lower()
    return TEXT[key].get(lang, TEXT[key]["en"]).format(**args)


def plain(value: str) -> str:
    """Lower case, no accents, no punctuation: "Cuisine d'été" -> "cuisine d ete"."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


@dataclass
class Wall:
    """One tablet that listens: the screen it shows, and how to reach it."""

    screen: str
    names: list[str]
    tv: bool
    send: Any
    words: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.words = {plain(n) for n in [self.screen, *self.names] if n}


class Voice:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.walls: dict[int, Wall] = {}
        self.answers: dict[int, asyncio.Future[str]] = {}
        self._ids = itertools.count(1)

    def match(self, where: str) -> list[Wall]:
        """The tablets a spoken place means: a screen's id or name, or "the TV"."""
        words = plain(where).split()
        tv = any(w in TV_WORDS for w in words)
        name = " ".join(w for w in words if w not in DEVICE_WORDS and w not in TV_WORDS)
        walls = list(self.walls.values())
        if name:
            found = [w for w in walls if name in w.words]
            if not found:
                # "the kitchen" for a screen called "Kitchen tablet".
                found = [w for w in walls if any(name in n.split() or n in name.split() for n in w.words if n)]
            if tv:
                found = [w for w in found if w.tv] or found
            return found
        return [w for w in walls if w.tv] if tv else []

    async def ask(self, command: dict[str, Any], where: str, language: str | None) -> str:
        if self.hass.data[PRO].state()["plan"] == "free":
            return text("pro", language)
        walls = self.match(where)
        if not walls:
            return text("nowhere", language, where=where)
        request = next(self._ids)
        answer: asyncio.Future[str] = self.hass.loop.create_future()
        self.answers[request] = answer
        try:
            for wall in walls:
                wall.send({**command, "request": request, "language": language or "en"})
            async with asyncio.timeout(ANSWER_WAIT):
                return await answer
        except TimeoutError:
            label = walls[0].names[0] if walls[0].names else walls[0].screen
            return text("sent", language, where=label)
        finally:
            self.answers.pop(request, None)

    @callback
    def answered(self, request: int, reply: str) -> None:
        answer = self.answers.get(request)
        if answer and not answer.done():
            answer.set_result(reply)


@callback
def async_register(hass: HomeAssistant) -> None:
    voice = hass.data[VOICE] = Voice(hass)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_answer)

    # The conversation integration keeps sentence triggers even before it has started.
    try:
        from homeassistant.components.conversation.agent_manager import get_agent_manager
    except ImportError:
        # A Home Assistant without Assist: tablets still listen, only the sentences are missing.
        return
    try:
        # Home Assistant before 2026.11 takes the sentences and the callback in one TriggerDetails.
        from homeassistant.components.conversation.trigger import TriggerDetails
    except ImportError:
        TriggerDetails = None

    def slot(result: Any, name: str) -> str:
        entity = result.entities.get(name)
        return str(entity.text if entity is not None else "").strip()

    async def show(user_input: Any, result: Any) -> str | None:
        return await voice.ask({"action": "show", "what": slot(result, "what")}, slot(result, "where"), user_input.language)

    async def off(user_input: Any, result: Any) -> str | None:
        return await voice.ask({"action": "off"}, slot(result, "where"), user_input.language)

    # A change in Home Assistant's Assist must never stop the dashboards from loading: only the sentences go.
    try:
        manager = get_agent_manager(hass)
        for sentences, call in ((SENTENCES["show"], show), (SENTENCES["off"], off)):
            if TriggerDetails is not None:
                manager.register_trigger(TriggerDetails(sentences, call))
            else:
                manager.register_trigger(sentences, call)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Talk to the wall: could not add the Assist sentences", exc_info=True)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/voice/subscribe",
        vol.Optional("screen", default=""): str,
        vol.Optional("names", default=[]): [str],
        vol.Optional("tv", default=False): bool,
    }
)
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A wall tablet listens for sentences that name its screen. Works for non-admin logins, like the layout."""
    voice: Voice = hass.data[VOICE]
    key = id(connection) * 1000 + msg["id"]

    @callback
    def send(command: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], command))

    voice.walls[key] = Wall(msg["screen"], [n for n in msg["names"] if n][:4], msg["tv"], send)

    # Answer cards, wall commands and the wake word are Pro. The tablet learns the plan now and when it changes.
    pro: ProManager = hass.data[PRO]

    @callback
    def plan(*_: Any) -> None:
        send({"action": "plan", "pro": pro.state()["plan"] != "free"})

    stop_plan = pro.async_listen(plan)

    @callback
    def gone() -> None:
        voice.walls.pop(key, None)
        stop_plan()

    connection.subscriptions[msg["id"]] = gone
    connection.send_result(msg["id"])
    plan()


@websocket_api.websocket_command(
    {vol.Required("type"): "hadash/voice/answer", vol.Required("request"): int, vol.Required("text"): str}
)
@callback
def ws_answer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """What the tablet did, as one short sentence for Assist to speak."""
    voice: Voice = hass.data[VOICE]
    voice.answered(msg["request"], msg["text"][:300])
    connection.send_result(msg["id"])
