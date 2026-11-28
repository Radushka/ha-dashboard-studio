"""Intercom between the wall tablets (Pro): "tell the kitchen dinner is ready", "dinner bell", "reply ...".

Each tablet subscribes with its id and the name of its place ("Kitchen"). Home Assistant passes a message on at
once to the tablets it is for and keeps nothing: the words, or a short recording of the sender's own voice
(8 kHz mu-law, base64, at most about 15 seconds), or the dinner bell. Every subscriber also gets the list of tablets
that are online, so the sender can say "tell the bedroom" and the reply can go back to the right tablet.

Live talk (Pro): "call the kitchen". The caller rings ("hadash/intercom/call", action "ring"); the kitchen knocks and
plays the caller's voice. Nothing from the kitchen goes out until someone there speaks: their tablet then answers
("answer") and its sound goes back. Sound ("hadash/intercom/talk") is 16 kHz mu-law in small base64 pieces and is
passed only between the two ends of the call, never kept. A call ends with "end" or "decline", or when one end
leaves Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import PRO

INTERCOM = "hadash_intercom"
EVENT = "hadash_intercom"
# 15 seconds of 8 kHz mu-law is 120 kB, 160 kB in base64.
MAX_AUDIO = 170_000
# One piece of live sound: a tenth of a second is about 2 kB as base64; allow up to a second.
MAX_PIECE = 24_000


class Call:
    def __init__(self, call_id: str, caller: str, callees: set[str], info: dict[str, Any]) -> None:
        self.id = call_id
        self.caller = caller
        self.callees = callees
        self.info = info
        # The tablet that answered, once someone there spoke.
        self.answered: str | None = None

    def parties(self) -> set[str]:
        return {self.caller, self.answered} if self.answered else {self.caller, *self.callees}


class Intercom:
    def __init__(self) -> None:
        # Subscription key -> (tablet id, place name, send function).
        self.tablets: dict[int, tuple[str, str, Callable[[dict[str, Any]], None]]] = {}
        self._next = 0
        self.calls: dict[str, Call] = {}

    def online(self) -> list[dict[str, str]]:
        seen: dict[str, dict[str, str]] = {}
        for tablet, where, _ in self.tablets.values():
            seen[tablet] = {"tablet": tablet, "where": where}
        return list(seen.values())

    def join(self, tablet: str, where: str, send: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        key = self._next
        self._next += 1
        self.tablets[key] = (tablet, where, send)
        self._roster()

        @callback
        def leave() -> None:
            self.tablets.pop(key, None)
            self._roster()
            if tablet not in {t for t, _, _ in self.tablets.values()}:
                for call in [c for c in self.calls.values() if tablet in c.parties()]:
                    self.end(call.id, tablet, "gone")

        return leave

    def _roster(self) -> None:
        roster = {"kind": "tablets", "tablets": self.online()}
        for _, _, send in list(self.tablets.values()):
            send(roster)

    def send(self, message: dict[str, Any]) -> int:
        """Passes a message to the tablets it is for (all but the sender when "to" is empty). Returns how many."""
        to = set(message.get("to") or [])
        count = 0
        for tablet, _, send in list(self.tablets.values()):
            if tablet == message["from_tablet"] and not message.get("echo"):
                continue
            if to and tablet not in to:
                continue
            send({"kind": "message", "message": message})
            count += 1
        return count


    def _to(self, tablets: set[str], data: dict[str, Any]) -> int:
        count = 0
        for tablet, _, send in list(self.tablets.values()):
            if tablet in tablets:
                send(data)
                count += 1
        return count

    def busy(self, tablet: str) -> bool:
        return any(tablet in c.parties() for c in self.calls.values())

    def ring(self, call_id: str, caller: str, to: list[str], info: dict[str, Any]) -> str:
        """Starts a call. Returns "ringing", "busy" (everyone there is in another call) or "nobody"."""
        online = {t for t, _, _ in self.tablets.values()}
        callees = {t for t in to if t in online and t != caller}
        if not callees:
            return "nobody"
        free = {t for t in callees if not self.busy(t)}
        if not free:
            return "busy"
        call = Call(call_id, caller, free, info)
        self.calls[call_id] = call
        self._to(free, {"kind": "call", "action": "ring", "call": call_id, "from_tablet": caller, **info})
        return "ringing"

    def answer(self, call_id: str, tablet: str) -> bool:
        call = self.calls.get(call_id)
        if call is None or tablet not in call.callees or call.answered not in (None, tablet):
            return False
        call.answered = tablet
        self._to({call.caller}, {"kind": "call", "action": "answer", "call": call_id, "by": tablet})
        # Other tablets in the same room stop knocking.
        self._to(call.callees - {tablet}, {"kind": "call", "action": "end", "call": call_id, "why": "taken"})
        return True

    def end(self, call_id: str, tablet: str, why: str = "end") -> bool:
        call = self.calls.get(call_id)
        if call is None or tablet not in {call.caller, *call.callees}:
            return False
        if why == "decline" and tablet != call.caller:
            # One tablet says "not now": the call ends only when every tablet it rang said so.
            call.callees.discard(tablet)
            if call.callees and not call.answered:
                return True
        self.calls.pop(call_id, None)
        self._to({call.caller, *call.callees} - {tablet}, {"kind": "call", "action": "end", "call": call_id, "why": why})
        return True

    def talk(self, call_id: str, tablet: str, audio: str, seq: int) -> bool:
        """Passes a piece of sound to the other end. A tablet that was rang sends only after it answered."""
        call = self.calls.get(call_id)
        if call is None:
            return False
        if tablet == call.caller:
            to = {call.answered} if call.answered else call.callees
        elif tablet == call.answered:
            to = {call.caller}
        else:
            return False
        self._to(to, {"kind": "talk", "call": call_id, "from_tablet": tablet, "audio": audio, "seq": seq})
        return True


MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("to", default=list): [vol.All(str, vol.Length(max=80))],
        vol.Required("from_tablet"): vol.All(str, vol.Length(max=80)),
        vol.Optional("from_where", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("from_name", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("text", default=""): vol.All(str, vol.Length(max=300)),
        vol.Optional("audio"): vol.Any(None, vol.All(str, vol.Length(max=MAX_AUDIO))),
        vol.Optional("bell", default=False): bool,
        vol.Optional("reply_to"): vol.Any(None, vol.All(str, vol.Length(max=64))),
    }
)


async def async_setup(hass: HomeAssistant) -> None:
    hass.data[INTERCOM] = Intercom()
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_send)
    websocket_api.async_register_command(hass, ws_call)
    websocket_api.async_register_command(hass, ws_talk)


# Like the layout, these work for non-admin logins: wall tablets often use a plain user.


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/intercom/subscribe",
        vol.Required("tablet"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Optional("where", default=""): vol.All(str, vol.Length(max=80)),
    }
)
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    intercom: Intercom = hass.data[INTERCOM]

    @callback
    def forward(data: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], data))

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = intercom.join(msg["tablet"], msg["where"], forward)


@websocket_api.websocket_command({vol.Required("type"): "hadash/intercom/send", vol.Required("message"): MESSAGE_SCHEMA})
@callback
def ws_send(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    message = {**msg["message"], "at": time.time()}
    if not message["text"] and not message.get("audio") and not message["bell"]:
        connection.send_error(msg["id"], "empty", "A message needs words, a recording or the bell.")
        return
    count = hass.data[INTERCOM].send(message)
    # Automations can follow along (without the recording).
    hass.bus.async_fire(EVENT, {k: v for k, v in message.items() if k != "audio"})
    connection.send_result(msg["id"], {"delivered": count})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/intercom/call",
        vol.Required("call"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("action"): vol.In(["ring", "answer", "decline", "end"]),
        vol.Required("tablet"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Optional("to", default=list): [vol.All(str, vol.Length(max=80))],
        vol.Optional("from_where", default=""): vol.All(str, vol.Length(max=80)),
        vol.Optional("from_name", default=""): vol.All(str, vol.Length(max=80)),
    }
)
@callback
def ws_call(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Live talk: ring, answer (someone at the far end spoke), decline (not now) or end."""
    intercom: Intercom = hass.data[INTERCOM]
    action = msg["action"]
    if action == "ring":
        if hass.data[PRO].state()["plan"] == "free":
            connection.send_error(msg["id"], "pro", "Live talk is part of Dashboard Studio Pro.")
            return
        info = {"from_where": msg["from_where"], "from_name": msg["from_name"]}
        state = intercom.ring(msg["call"], msg["tablet"], msg["to"], info)
        if state == "ringing":
            hass.bus.async_fire(EVENT, {"call": msg["call"], "from_tablet": msg["tablet"], "to": msg["to"], **info, "at": time.time()})
        connection.send_result(msg["id"], {"state": state})
        return
    if action == "answer":
        ok = intercom.answer(msg["call"], msg["tablet"])
    else:
        ok = intercom.end(msg["call"], msg["tablet"], action)
    connection.send_result(msg["id"], {"ok": ok})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/intercom/talk",
        vol.Required("call"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("tablet"): vol.All(str, vol.Length(min=1, max=80)),
        vol.Required("audio"): vol.All(str, vol.Length(min=1, max=MAX_PIECE)),
        vol.Optional("seq", default=0): int,
    }
)
@callback
def ws_talk(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    ok = hass.data[INTERCOM].talk(msg["call"], msg["tablet"], msg["audio"], msg["seq"])
    if ok:
        connection.send_result(msg["id"])
    else:
        connection.send_error(msg["id"], "no_call", "That call has ended.")
