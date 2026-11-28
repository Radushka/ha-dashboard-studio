"""Room presence: the wall tablets tell Home Assistant who is in each room, with no extra hardware.

Radu (27 Nov 2026): "the tablets sense who is in each room and the screens adapt."

What a tablet notices (only a number or a word leaves it, never a picture):
1. Faces in front of its camera, also of someone who sits still. The wall app counts them every two seconds with
   the face finder built into Android, or Apple's on the iPad. A count of 0 is news too: the camera looked and saw
   nobody.
2. Movement in front of the camera, a touch, the wake word, a TV remote.

One sensor for each room with a screen: binary_sensor.<room>_presence (device class occupancy, in that room).
It joins the tablets with the motion, occupancy and presence sensors already in the room:
1. On: a sensor in the room is on, or a tablet there noticed someone in the last HOLD_S seconds. A face keeps it
   on for as long as the face is there, so a person who reads on the couch is not "gone" after two minutes.
2. Off: no sign of anyone for HOLD_S seconds.
3. "can_see": a tablet's camera looked in the last LOOK_FRESH_S seconds. Off with can_see means the room is truly
   empty, not only quiet, so "lights off when the room is empty" is safe.
4. "people": how many faces the tablets see now (at least 1 while on).
5. "who" (Pro): the person picked on that screen (Follow me), else the only person at home.

Room to room and the other features read the same signs: a face also tells Room to room that its room is not
quiet yet (follow.py), so a video does not leave a person who sits still.

Over the websocket:
- "hadash/rooms/see": {screen, how, people?, who?} from a screen.
- "hadash/rooms/where": where everyone is, for the studio and the voice answer.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, PRO

_LOGGER = logging.getLogger(__name__)

ROOMS = "hadash_rooms"
# Someone still counts as in the room this long after their last sign.
HOLD_S = 120.0
# A camera that reported this recently can see the room.
LOOK_FRESH_S = 60.0
SIGN_CLASSES = ("motion", "occupancy", "presence")
HOWS = ("look", "motion", "touch", "voice", "remote")
UNIQUE = "room_presence_"


@dataclass
class Signs:
    """What one screen noticed last (monotonic seconds)."""

    looked_at: float = -1e9
    people: int = 0
    face_at: float = -1e9
    sign_at: float = -1e9
    who: str = ""
    who_at: float = -1e9


@dataclass
class Room:
    area: str
    on: bool
    people: int
    can_see: bool
    who: list[str]
    since: float
    how: str


class Rooms:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.screens: dict[str, Signs] = {}
        # The area each screen was last in, so a room keeps its sensor while its tablet is away.
        self.areas: set[str] = set()
        self.state: dict[str, Room] = {}
        self._listeners: list[Any] = []
        self._adder: Any = None
        self._timer: Any = None
        self._stop: Any = None

    # ---- Starting ----

    @callback
    def async_start(self) -> None:
        self._stop = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stopping)
        # The rooms that had a sensor before the restart keep it, so automations do not break.
        for entry in er.async_get(self.hass).entities.values():
            if entry.platform == DOMAIN and entry.domain == "binary_sensor" and entry.unique_id.startswith(UNIQUE):
                self.areas.add(entry.unique_id[len(UNIQUE) :])

    @callback
    def _stopping(self, _event: Event) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    @callback
    def set_adder(self, adder: Any) -> None:
        self._adder = adder
        adder(sorted(self.areas))

    @callback
    def listen(self, cb: Any) -> Any:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    # ---- Helpers ----

    @property
    def pro(self) -> bool:
        pro = self.hass.data.get(PRO)
        return bool(pro) and pro.state()["plan"] != "free"

    def _follow(self) -> Any:
        from .follow import FOLLOW

        return self.hass.data.get(FOLLOW)

    def area_of(self, screen: str) -> str | None:
        follow = self._follow()
        return follow.area_of(screen) if follow else None

    def entity_area(self, entity_id: str) -> str | None:
        follow = self._follow()
        return follow.entity_area(entity_id) if follow else None

    def area_name(self, area: str) -> str:
        found = ar.async_get(self.hass).async_get_area(area)
        return found.name if found else area

    def _own(self, entity_id: str) -> bool:
        entry = er.async_get(self.hass).async_get(entity_id)
        return bool(entry and entry.platform == DOMAIN)

    def sensors(self, area: str) -> list[Any]:
        """The room's own motion, occupancy and presence sensors (not ours)."""
        return [
            s
            for s in self.hass.states.async_all("binary_sensor")
            if s.attributes.get("device_class") in SIGN_CLASSES and not self._own(s.entity_id) and self.entity_area(s.entity_id) == area
        ]

    def _people_home(self) -> list[Any]:
        from .presence import is_home

        return [p for p in self.hass.states.async_all("person") if is_home(self.hass, p)]

    # ---- Signs ----

    @callback
    def see(self, screen: str, how: str, people: int | None = None, who: str = "") -> None:
        now = time.monotonic()
        s = self.screens.setdefault(screen, Signs())
        if how == "look":
            s.looked_at = now
            s.people = max(0, people or 0)
            if s.people:
                s.face_at = now
                s.sign_at = now
        else:
            s.sign_at = now
        if who:
            s.who, s.who_at = who, now
        follow = self._follow()
        if follow:
            # Room to room: a face is someone here, and a camera that looked can tell an empty room.
            if how == "look":
                follow.sensed_at[screen] = now
            if s.people or how != "look":
                follow.seen_at[screen] = now
        area = self.area_of(screen)
        if area and area not in self.areas:
            self.areas.add(area)
            if self._adder:
                self._adder([area])
        self.update()

    @callback
    def _state_changed(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is None or new.domain != "binary_sensor" or new.attributes.get("device_class") not in SIGN_CLASSES:
            return
        if self.areas and not self._own(new.entity_id) and self.entity_area(new.entity_id) in self.areas:
            self.update()

    # ---- The rooms ----

    def compute(self, area: str, now: float) -> Room:
        screens = [(name, s) for name, s in self.screens.items() if self.area_of(name) == area]
        sensors = self.sensors(area)
        wall_now = time.time()
        last = -1e9
        how = ""
        for _name, s in screens:
            if s.sign_at > last:
                last, how = s.sign_at, ("face" if s.face_at >= s.sign_at else "tablet")
        sensor_on = any(x.state == "on" for x in sensors)
        for x in sensors:
            if x.state == "off":
                t = now - max(0.0, wall_now - x.last_changed.timestamp())
                if t > last:
                    last, how = t, "sensor"
        if sensor_on:
            last, how = now, "sensor"
        can_see = any(now - s.looked_at < LOOK_FRESH_S for _n, s in screens)
        faces = max((s.people for _n, s in screens if now - s.looked_at < LOOK_FRESH_S), default=0)
        on = sensor_on or faces > 0 or now - last < HOLD_S
        # A camera that sees nobody right now, and nothing moved for a while: the room is empty even if the hold
        # has not run out (someone walked out of the picture a minute ago and nothing else moves).
        if on and not sensor_on and can_see and faces == 0 and now - last >= HOLD_S / 2:
            on = False
        who: list[str] = []
        if on and self.pro:
            picked = [s.who for _n, s in screens if s.who and now - s.who_at < HOLD_S]
            if picked:
                who = sorted(set(picked))
            else:
                home = self._people_home()
                if len(home) == 1:
                    who = [home[0].entity_id]
        old = self.state.get(area)
        since = old.since if old and old.on == on else now
        return Room(area, on, max(faces, 1) if on else 0, can_see, who, since, how if on else "")

    @callback
    def update(self) -> None:
        now = time.monotonic()
        changed: list[str] = []
        soonest: float | None = None
        for area in self.areas:
            room = self.compute(area, now)
            old = self.state.get(area)
            self.state[area] = room
            if old != room:
                changed.append(area)
            if room.on or room.can_see:
                # Look again when the hold or the camera's last look runs out.
                soonest = min(soonest or 1e9, 15.0)
        if self._timer:
            self._timer()
            self._timer = None
        if soonest is not None:
            self._timer = async_call_later(self.hass, soonest, self._tick)
        for area in changed:
            for cb in list(self._listeners):
                cb(area)

    @callback
    def _tick(self, _now: Any) -> None:
        self._timer = None
        self.update()

    def room(self, area: str) -> Room | None:
        return self.state.get(area)

    def where(self) -> dict[str, Any]:
        """Where everyone is: each room with someone in it, and the people at home in no known room."""
        rooms = [
            {"area": r.area, "name": self.area_name(r.area), "people": r.people, "who": r.who, "can_see": r.can_see}
            for r in self.state.values()
            if r.on
        ]
        placed = {p for r in rooms for p in r["who"]}
        home = [p.entity_id for p in self._people_home()]
        return {"rooms": rooms, "home": [p for p in home if p not in placed], "pro": self.pro}


# ---- Voice: "where is everyone?" (Pro) ----

SENTENCES: dict[str, list[str]] = {
    "where": [
        "where is everyone",
        "where's everyone",
        "who is (home|here)",
        "(où|ou) est tout le monde",
        "qui est (à la maison|là)",
        "wo sind (alle|denn alle)",
        "wer ist (zu hause|zuhause|da)",
        "(dónde|donde) (están|estan) todos",
        "(quién|quien) (está|esta) en casa",
    ],
    "anyone": [
        "is (anyone|anybody|someone|somebody) in [the] {room}",
        "(y a-t-il|il y a) quelqu'un (dans|à) [la |le |l'] {room}",
        "ist jemand (im|in der|in) {room}",
        "hay alguien en [el |la ] {room}",
    ],
}

TEXT = {
    "in": {"en": "{who} in the {room}.", "fr": "{who} dans {room}.", "de": "{who} im Raum {room}.", "es": "{who} en {room}."},
    "someone": {"en": "Someone is in the {room}.", "fr": "Quelqu'un est dans {room}.", "de": "Jemand ist im Raum {room}.", "es": "Hay alguien en {room}."},
    "people": {
        "en": "{n} people are in the {room}.",
        "fr": "{n} personnes sont dans {room}.",
        "de": "{n} Personen sind im Raum {room}.",
        "es": "Hay {n} personas en {room}.",
    },
    "home": {"en": "{who} home.", "fr": "{who} à la maison.", "de": "{who} zu Hause.", "es": "{who} en casa."},
    "nobody_room": {
        "en": "Nobody is in the {room}.",
        "fr": "Personne n'est dans {room}.",
        "de": "Niemand ist im Raum {room}.",
        "es": "No hay nadie en {room}.",
    },
    "quiet_room": {
        "en": "I have not noticed anyone in the {room} for a while.",
        "fr": "Je n'ai remarqué personne dans {room} depuis un moment.",
        "de": "Im Raum {room} habe ich seit einer Weile niemanden bemerkt.",
        "es": "No he notado a nadie en {room} desde hace un rato.",
    },
    "no_room": {
        "en": "I do not know a room called {room}.",
        "fr": "Je ne connais pas de pièce nommée {room}.",
        "de": "Ich kenne keinen Raum namens {room}.",
        "es": "No conozco una habitación llamada {room}.",
    },
    "cannot_tell": {
        "en": "I cannot tell. There is no tablet or motion sensor in the {room}.",
        "fr": "Je ne peux pas le dire. Il n'y a ni tablette ni détecteur de mouvement dans {room}.",
        "de": "Das kann ich nicht sagen. Im Raum {room} gibt es kein Tablet und keinen Bewegungsmelder.",
        "es": "No lo sé. No hay tableta ni sensor de movimiento en {room}.",
    },
    "nobody_home": {"en": "Nobody is home.", "fr": "Personne n'est à la maison.", "de": "Niemand ist zu Hause.", "es": "No hay nadie en casa."},
    "is": {"en": "is", "fr": "est", "de": "ist", "es": "está"},
    "are": {"en": "are", "fr": "sont", "de": "sind", "es": "están"},
    "and": {"en": "and", "fr": "et", "de": "und", "es": "y"},
    "pro": {
        "en": "Knowing who is in each room is part of Dashboard Studio Pro.",
        "fr": "Savoir qui est dans chaque pièce fait partie de Dashboard Studio Pro.",
        "de": "Zu wissen, wer in welchem Raum ist, gehört zu Dashboard Studio Pro.",
        "es": "Saber quién está en cada habitación es parte de Dashboard Studio Pro.",
    },
}


def text(key: str, language: str | None, **values: Any) -> str:
    lang = (language or "en").split("-")[0].lower()
    return TEXT[key].get(lang, TEXT[key]["en"]).format(**values)


def _names(hass: HomeAssistant, people: list[str], language: str | None) -> tuple[str, bool]:
    """ "Radu and Angela", and whether that is more than one person."""
    names = []
    for p in people:
        state = hass.states.get(p)
        name = str(state.attributes.get("friendly_name") or p) if state else p
        names.append(name.split()[0] if name else p)
    if len(names) <= 1:
        return (names[0] if names else ""), False
    return f"{', '.join(names[:-1])} {text('and', language)} {names[-1]}", True


def answer_where(rooms: Rooms, language: str | None) -> str:
    if not rooms.pro:
        return text("pro", language)
    found = rooms.where()
    parts: list[str] = []
    for r in found["rooms"]:
        if r["who"]:
            names, many = _names(rooms.hass, r["who"], language)
            verb = text("are" if many else "is", language)
            parts.append(text("in", language, who=f"{names} {verb}", room=r["name"]))
        elif r["people"] > 1:
            parts.append(text("people", language, n=r["people"], room=r["name"]))
        else:
            parts.append(text("someone", language, room=r["name"]))
    if found["home"]:
        names, many = _names(rooms.hass, found["home"], language)
        parts.append(text("home", language, who=f"{names} {text('are' if many else 'is', language)}"))
    if not parts:
        return text("nobody_home", language)
    return " ".join(parts)


def answer_anyone(rooms: Rooms, spoken: str, language: str | None) -> str:
    areas = ar.async_get(rooms.hass)
    found = areas.async_get_area_by_name(spoken)
    if found is None:
        words = spoken.lower().strip()
        found = next((a for a in areas.async_list_areas() if a.name.lower() in words or words in a.name.lower()), None)
    if found is None:
        return text("no_room", language, room=spoken)
    room = rooms.room(found.id)
    if room is None:
        # No tablet there: the room's own sensors may still tell.
        sensors = rooms.sensors(found.id)
        if not sensors:
            return text("cannot_tell", language, room=found.name)
        if any(s.state == "on" for s in sensors):
            return text("someone", language, room=found.name)
        return text("quiet_room", language, room=found.name)
    if room.on:
        if room.who and rooms.pro:
            names, many = _names(rooms.hass, room.who, language)
            return text("in", language, who=f"{names} {text('are' if many else 'is', language)}", room=found.name)
        if room.people > 1:
            return text("people", language, n=room.people, room=found.name)
        return text("someone", language, room=found.name)
    return text("nobody_room" if room.can_see else "quiet_room", language, room=found.name)


def _register_sentences(hass: HomeAssistant, rooms: Rooms) -> None:
    try:
        from homeassistant.components.conversation.agent_manager import get_agent_manager
    except ImportError:
        return
    try:
        from homeassistant.components.conversation.trigger import TriggerDetails
    except ImportError:
        TriggerDetails = None

    async def where(user_input: Any, _result: Any) -> str | None:
        return answer_where(rooms, user_input.language)

    async def anyone(user_input: Any, result: Any) -> str | None:
        entity = result.entities.get("room")
        return answer_anyone(rooms, str(entity.text if entity is not None else "").strip(), user_input.language)

    try:
        manager = get_agent_manager(hass)
        for sentences, call in ((SENTENCES["where"], where), (SENTENCES["anyone"], anyone)):
            if TriggerDetails is not None:
                manager.register_trigger(TriggerDetails(sentences, call))
            else:
                manager.register_trigger(sentences, call)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Room presence: could not add the Assist sentences", exc_info=True)


# ---- Websocket ----


def _rooms(hass: HomeAssistant) -> Rooms:
    return hass.data[ROOMS]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/rooms/see",
        vol.Optional("screen", default=""): str,
        vol.Required("how"): vol.In(HOWS),
        vol.Optional("people"): vol.All(int, vol.Range(min=0, max=20)),
        vol.Optional("who", default=""): vol.All(str, vol.Length(max=80)),
    }
)
@callback
def ws_see(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    layout = hass.data[DOMAIN].layout or {}
    if isinstance(layout, dict) and layout.get("roomPresence") == "off":
        # Turned off in the studio: the tablets keep what they see to themselves.
        connection.send_result(msg["id"])
        return
    who = msg["who"] if msg["who"].startswith("person.") else ""
    _rooms(hass).see(msg["screen"], msg["how"], msg.get("people"), who)
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "hadash/rooms/where"})
@callback
def ws_where(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], _rooms(hass).where())


@callback
def async_setup(hass: HomeAssistant) -> None:
    rooms = hass.data[ROOMS] = Rooms(hass)
    rooms.async_start()
    for command in (ws_see, ws_where):
        websocket_api.async_register_command(hass, command)
    _register_sentences(hass, rooms)
