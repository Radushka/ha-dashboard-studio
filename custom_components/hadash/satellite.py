"""Wall tablets as Assist satellites (Pro).

Each wall tablet that talks becomes a device in Home Assistant, with an Assist satellite entity and the two usual
settings (which assistant, how long a pause ends a sentence). Home Assistant then knows which tablet and which room
is talking, automations can make a tablet speak ("assist_satellite.announce") or ask a question
("assist_satellite.ask_question"), and the tablets show up with the other voice devices.

How a tablet talks to us, over the same websocket as everything else:
1. "hadash/satellite/subscribe": the tablet says who it is. It gets its device id, and later the things to play.
2. "hadash/satellite/run": the tablet heard the wake word (or a tap) and sends its sound. The sound goes to the
   satellite entity, which runs Home Assistant's own pipeline; its events go back to the tablet. Home Assistant
   gives the wake word phrase its usual 2 second rule, so when two tablets hear "Okay Nabu" only the first acts.
3. "hadash/satellite/done": an announcement has played. "hadash/satellite/state": what the tablet does between runs.

The wake word itself is still heard on the tablet (our wall app); nothing leaves it before that.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PRO, SATELLITES, SATELLITES_STORAGE_KEY, VERSION

if TYPE_CHECKING:
    from .assist_satellite import WallSatellite

_LOGGER = logging.getLogger(__name__)

# A new tablet: the platforms add its entities.
SIGNAL_NEW = f"{DOMAIN}_satellite_new"
# A tablet came or went: its entities show it.
SIGNAL_SEEN = f"{DOMAIN}_satellite_seen_{{}}"
# The longest an announcement may play before Home Assistant moves on.
PLAY_LIMIT = 180.0
# When one tablet hears the wake word, how long the others have to say they heard it too. The sound is kept
# meanwhile, so no word is lost; the answer only comes this much later.
CONTEST_WINDOW = 0.5
# After a winner, the same word from another tablet is the same wake-up (Home Assistant's own rule is 2 s).
CONTEST_COOLDOWN = 2.0
MANUFACTURER = "Lukaps"


def signal_seen(tablet: str) -> str:
    return SIGNAL_SEEN.format(tablet)


class Tablet:
    """One wall tablet: what it told us, and how to reach it while it is connected."""

    def __init__(self, tablet_id: str, name: str, model: str, listen: bool) -> None:
        self.id = tablet_id
        self.name = name
        self.model = model
        self.listen = listen
        self.send: Callable[[dict[str, Any]], None] | None = None
        self.satellite: WallSatellite | None = None

    @property
    def connected(self) -> bool:
        return self.send is not None


class Contest:
    """Tablets that heard the same wake word at about the same time. The clearest one, the closest, answers."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.claims: list[tuple[float, int, str]] = []
        self.winner: asyncio.Future[str] = hass.loop.create_future()
        self.decided_at: float | None = None

    def decide(self, now: float) -> None:
        if self.winner.done():
            return
        # The highest level wins; at the same level, the first one.
        _level, _order, tablet = max(self.claims, key=lambda c: (c[0], -c[1]))
        self.decided_at = now
        self.winner.set_result(tablet)


class Satellites:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.entry: ConfigEntry | None = None
        self.tablets: dict[str, Tablet] = {}
        self.played: dict[int, asyncio.Future[None]] = {}
        self._ids = itertools.count(1)
        self.contests: dict[str, Contest] = {}
        # Tablets waiting to become satellites: told when the plan changes or Dashboard Studio has started.
        self._listeners: set[Callable[[], None]] = set()
        self._store: Store[dict[str, Any]] = Store(hass, 1, SATELLITES_STORAGE_KEY)

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        for tid, t in (data.get("tablets") or {}).items():
            self.tablets[tid] = Tablet(tid, t.get("name") or "", t.get("model") or "", bool(t.get("listen")))

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"tablets": {t.id: {"name": t.name, "model": t.model, "listen": t.listen} for t in self.tablets.values()}},
            1,
        )

    @callback
    def async_start(self) -> None:
        self.hass.data[PRO].async_listen(lambda *_: self.changed())

    @callback
    def async_listen(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def set_entry(self, entry: ConfigEntry | None) -> None:
        self.entry = entry
        self.changed()

    @property
    def pro(self) -> bool:
        return self.hass.data[PRO].state()["plan"] != "free"

    @callback
    def device_id(self, tablet: Tablet) -> str | None:
        """The tablet's device, made the first time it is seen."""
        if self.entry is None:
            return None
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, tablet.id)},
            name=tablet.name or "Wall tablet",
            manufacturer=MANUFACTURER,
            model=f"Dashboard Studio Wall · {tablet.model}" if tablet.model else "Dashboard Studio Wall",
            sw_version=VERSION,
        )
        return device.id

    @callback
    def connect(self, tablet_id: str, name: str, model: str, listen: bool, send: Callable[[dict[str, Any]], None]) -> Tablet:
        tablet = self.tablets.get(tablet_id)
        if tablet is None:
            tablet = self.tablets[tablet_id] = Tablet(tablet_id, name, model, listen)
            self._save()
            if self.entry is not None:
                async_dispatcher_send(self.hass, SIGNAL_NEW, tablet)
        elif (tablet.model, tablet.listen) != (model, listen):
            tablet.model, tablet.listen = model, listen
            self._save()
        # The name follows the screen the tablet shows; a name given in Home Assistant stays (name_by_user).
        if name and tablet.name != name:
            tablet.name = name
            self._save()
        tablet.send = send
        async_dispatcher_send(self.hass, signal_seen(tablet.id))
        return tablet

    @callback
    def disconnect(self, tablet: Tablet, send: Callable[[dict[str, Any]], None]) -> None:
        # A tablet that reconnected already has a new connection; only the one that went away ends.
        if tablet.send is not send:
            return
        tablet.send = None
        if tablet.satellite is not None:
            tablet.satellite.connection_lost()
        async_dispatcher_send(self.hass, signal_seen(tablet.id))
        for future in list(self.played.values()):
            if not future.done():
                future.set_result(None)

    @callback
    def forget(self, tablet_id: str) -> None:
        """The device was deleted in Home Assistant."""
        if self.tablets.pop(tablet_id, None) is not None:
            self._save()

    async def play(self, tablet: Tablet, command: dict[str, Any]) -> None:
        """Sends something to play, and waits until the tablet has played it."""
        if tablet.send is None:
            raise ConnectionError(f"{tablet.name or tablet.id} is not connected")
        request = next(self._ids)
        future: asyncio.Future[None] = self.hass.loop.create_future()
        self.played[request] = future
        try:
            tablet.send({**command, "request": request})
            async with asyncio.timeout(PLAY_LIMIT):
                await future
        except TimeoutError:
            _LOGGER.debug("%s did not say when it finished playing", tablet.name)
        finally:
            self.played.pop(request, None)

    async def claim(self, tablet: Tablet, phrase: str, level: float | None) -> bool:
        """Whether this tablet should answer the wake word it heard.

        Tablets close to each other often both hear it. Each one says how clearly it heard the word (in dB above
        its room's own sound); the others have half a second to say the same, and the clearest one answers. A tablet with an older wall app
        sends no level: it goes ahead, and Home Assistant's own rule (the first one wins) still applies.
        """
        if level is None:
            return True
        rivals = [t for t in self.tablets.values() if t is not tablet and t.connected and t.listen and t.satellite]
        key = phrase.casefold()
        now = self.hass.loop.time()
        contest = self.contests.get(key)
        if contest is not None and contest.decided_at is not None:
            if now - contest.decided_at < CONTEST_COOLDOWN and contest.winner.result() != tablet.id:
                return False
            contest = None
        if contest is None:
            if not rivals:
                return True
            contest = self.contests[key] = Contest(self.hass)
            self.hass.loop.call_later(CONTEST_WINDOW, lambda: contest.decide(self.hass.loop.time()))
        contest.claims.append((level, len(contest.claims), tablet.id))
        winner = await contest.winner
        if len(contest.claims) > 1:
            _LOGGER.debug("Wake word %s: %s answers (%s)", phrase, winner, contest.claims)
        return winner == tablet.id

    @callback
    def done(self, request: int) -> None:
        future = self.played.get(request)
        if future and not future.done():
            future.set_result(None)


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_run)
    websocket_api.async_register_command(hass, ws_done)
    websocket_api.async_register_command(hass, ws_state)


def _tablet(hass: HomeAssistant, tablet_id: str) -> Tablet | None:
    satellites: Satellites | None = hass.data.get(SATELLITES)
    return satellites.tablets.get(tablet_id) if satellites else None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/satellite/subscribe",
        vol.Required("tablet"): vol.All(str, vol.Length(min=8, max=64)),
        vol.Optional("name", default=""): vol.All(str, vol.Length(max=60)),
        vol.Optional("model", default=""): vol.All(str, vol.Length(max=60)),
        vol.Optional("listen", default=False): bool,
    }
)
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A wall tablet becomes a satellite. Works for non-admin logins, like the layout."""
    satellites: Satellites = hass.data[SATELLITES]

    @callback
    def send(command: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], command))

    connection.send_result(msg["id"])
    tablet: Tablet | None = None

    @callback
    def update(*_: Any) -> None:
        """Becomes a satellite as soon as it may: on a Pro plan, once Dashboard Studio has started.

        After a restart, tablets reconnect while Home Assistant is still starting, before the satellites are
        ready. Their request waits here instead of being turned down, so they never stay without a satellite.
        """
        nonlocal tablet
        ready = satellites.pro and satellites.entry is not None
        if ready and tablet is None:
            tablet = satellites.connect(msg["tablet"], msg["name"].strip(), msg["model"].strip(), msg["listen"], send)
            send({"action": "ready", "device_id": satellites.device_id(tablet)})
        elif not ready and tablet is not None:
            satellites.disconnect(tablet, send)
            tablet = None
            send({"action": "off"})
        elif not ready:
            # A free plan, or still starting: the tablet talks to Assist as before until then.
            send({"action": "off"})

    stop_watching = satellites.async_listen(update)

    @callback
    def gone() -> None:
        stop_watching()
        if tablet is not None:
            satellites.disconnect(tablet, send)

    connection.subscriptions[msg["id"]] = gone
    update()


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/satellite/run",
        vol.Required("tablet"): str,
        vol.Optional("wake_word_phrase"): vol.All(str, vol.Length(min=1, max=60)),
        # How clearly the tablet heard the wake word: dB above the room's own sound (wall app 0.5.8 and newer).
        vol.Optional("wake_level"): vol.All(vol.Coerce(float), vol.Range(min=0, max=120)),
        # True: Home Assistant also answers (a conversation it started). False: only the words come back,
        # and the tablet decides: its own wall commands first, then Assist.
        vol.Optional("answer", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The tablet's sound, as binary messages, through its satellite entity into Home Assistant's pipeline."""
    tablet = _tablet(hass, msg["tablet"])
    satellite = tablet.satellite if tablet else None
    if satellite is None or not satellite.available:
        connection.send_error(msg["id"], "not_found", "This tablet is not a satellite")
        return

    audio: asyncio.Queue[bytes] = asyncio.Queue()

    @callback
    def on_audio(_hass: HomeAssistant, _connection: websocket_api.ActiveConnection, data: bytes) -> None:
        audio.put_nowait(data)

    handler, unregister = connection.async_register_binary_handler(on_audio)

    async def stream() -> AsyncGenerator[bytes]:
        # An empty message ends the sound.
        while chunk := await audio.get():
            yield chunk

    @callback
    def send(event: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    # The result first: the run starts at once, and its first events must not come before it.
    connection.send_result(msg["id"])
    task = hass.async_create_task(
        satellite.run(stream(), send, handler, msg.get("wake_word_phrase"), msg.get("wake_level"), msg["answer"]),
        f"{DOMAIN}_satellite_run",
    )

    @callback
    def cancel() -> None:
        task.cancel()
        audio.put_nowait(b"")

    connection.subscriptions[msg["id"]] = cancel
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Assist on %s stopped: %s", satellite.entity_id, err)
        send({"type": "error", "data": {"code": "satellite", "message": str(err)}})
    finally:
        unregister()
        if connection.subscriptions.get(msg["id"]) is cancel:
            connection.subscriptions.pop(msg["id"], None)


@websocket_api.websocket_command({vol.Required("type"): "hadash/satellite/done", vol.Required("request"): int})
@callback
def ws_done(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """An announcement, or a conversation's first sentence, has played."""
    satellites: Satellites = hass.data[SATELLITES]
    satellites.done(msg["request"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/satellite/state",
        vol.Required("tablet"): str,
        vol.Required("state"): vol.In(["idle", "processing", "responding"]),
    }
)
@callback
def ws_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """What the tablet does after the words: works out the answer, speaks it, or is done."""
    tablet = _tablet(hass, msg["tablet"])
    if tablet and tablet.satellite:
        tablet.satellite.tablet_state(msg["state"])
    connection.send_result(msg["id"])
