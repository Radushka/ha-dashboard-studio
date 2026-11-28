"""Talk at the front door from any tablet (Pro): your voice comes out of the Eufy doorbell.

The eufy_security integration has no way to talk back, but the Eufy add-on (eufy-security-ws) does: while the
doorbell's live video runs, "device.start_talkback" opens the doorbell's speaker and "device.talkback_audio_data"
carries the sound, as AAC (ADTS frames, 16 kHz mono). So:
1. A tablet asks to talk ("hadash/door/talk", a subscription). The Eufy live owner (eufy_live.py) keeps the video
   on while the talk lasts, so the doorbell's own sound comes to the tablet with the picture, as usual.
2. The tablet's microphone sound comes in small pieces ("hadash/door/audio", 16 kHz mu-law, base64). Home
   Assistant's own ffmpeg turns it into AAC, and each whole AAC frame goes on to the add-on at once.
3. Quick replies ("Please leave it at the door") are spoken by the tablet's voice and sent the same way.
4. One talk per door: a newer tablet takes over. When the tablet leaves, the speaker closes and the video may sleep.
Nothing is recorded or kept.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import PRO
from .eufy_live import lives, serial_of

_LOGGER = logging.getLogger(__name__)

KEY = "hadash_door_talk"
# The video must be live before the speaker can open.
LIVE_WAIT_S = 25.0
ANSWER_S = 10.0
# One piece of sound from the tablet: about a tenth of a second (2 kB as base64); allow up to a second.
MAX_PIECE = 24_000


def adts_frames(buf: bytearray) -> list[bytes]:
    """Takes whole ADTS frames from the front of buf (and removes them). A half frame stays for next time."""
    frames: list[bytes] = []
    while len(buf) >= 7:
        if buf[0] != 0xFF or (buf[1] & 0xF0) != 0xF0:
            # Out of step: skip to the next frame start.
            nxt = buf.find(b"\xff", 1)
            del buf[: nxt if nxt > 0 else len(buf)]
            continue
        size = ((buf[3] & 0x03) << 11) | (buf[4] << 3) | (buf[5] >> 5)
        if size < 7:
            del buf[:1]
            continue
        if len(buf) < size:
            break
        frames.append(bytes(buf[:size]))
        del buf[:size]
    return frames


def _ffmpeg(hass: HomeAssistant) -> str:
    with contextlib.suppress(Exception):
        from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415

        return get_ffmpeg_manager(hass).binary
    return "ffmpeg"


class Talk:
    def __init__(self, hass: HomeAssistant, talk_id: str, entity_id: str, send: Callable[[dict[str, Any]], None]) -> None:
        self.hass = hass
        self.id = talk_id
        self.entity_id = entity_id
        self.send = send
        self.serial = serial_of(hass, entity_id)
        self.step = "waking"
        self.proc: asyncio.subprocess.Process | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.tasks: list[asyncio.Task] = []
        self.answers: dict[str, asyncio.Future] = {}
        self.n = 0
        self.leave_video: Callable[[], None] | None = None
        self.closed = False

    def _say(self, step: str, why: str = "") -> None:
        self.step = step
        msg: dict[str, Any] = {"step": step}
        if why:
            msg["why"] = why
        if not self.closed or step == "ended":
            self.send(msg)

    async def _ask(self, command: dict[str, Any]) -> dict[str, Any]:
        assert self.ws is not None
        self.n += 1
        mid = f"hadash-talk-{self.n}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.answers[mid] = fut
        await self.ws.send_str(json.dumps({"messageId": mid, **command}))
        try:
            return await asyncio.wait_for(fut, ANSWER_S)
        finally:
            self.answers.pop(mid, None)

    async def _read(self) -> None:
        assert self.ws is not None
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            if data.get("type") == "result":
                fut = self.answers.get(data.get("messageId"))
                if fut is not None and not fut.done():
                    fut.set_result(data)
            elif data.get("type") == "event":
                event = data.get("event", {})
                if event.get("serialNumber") == self.serial and event.get("event") == "talkback stopped" and self.step == "open":
                    self._say("ended", "the doorbell closed its speaker")
        if not self.closed:
            self._say("ended", "the Eufy add-on closed the link")

    async def start(self) -> None:
        try:
            await self._start()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the tablet hears why, in plain words
            _LOGGER.debug("Door talk %s: %s", self.entity_id, err)
            self._say("failed", str(err)[:160] or "the doorbell did not answer")
            await self.stop()

    async def _start(self) -> None:
        if not self.serial:
            self._say("failed", "not_eufy")
            return
        owner = lives(self.hass)
        url = owner.links._addon()  # noqa: SLF001 - the same add-on address the live owner uses
        if url is None:
            self._say("failed", "no_addon")
            return
        # Keep the live video on while we talk: the doorbell only opens its speaker during a live video.
        live = owner.get(self.entity_id)
        self.leave_video = live.join(lambda _m: None)
        self._say("waking")
        for _ in range(int(LIVE_WAIT_S * 2)):
            if live.streaming() or self.closed:
                break
            await asyncio.sleep(0.5)
        if self.closed:
            return
        if not live.streaming():
            self._say("failed", "the video did not start")
            await self.stop()
            return
        self.ws = await async_get_clientsession(self.hass).ws_connect(url, heartbeat=10)
        self.tasks.append(self.hass.async_create_background_task(self._read(), f"hadash door talk read {self.entity_id}"))
        await self._ask({"command": "set_api_schema", "schemaVersion": 21})
        answer = await self._ask({"command": "device.start_talkback", "serialNumber": self.serial})
        if not answer.get("success") and "ongoing" not in json.dumps(answer).lower():
            self._say("failed", str(answer.get("errorCode") or "talk is not possible on this doorbell"))
            await self.stop()
            return
        self.proc = await asyncio.create_subprocess_exec(
            _ffmpeg(self.hass), "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
            "-f", "mulaw", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
            "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "32k", "-ar", "16000", "-ac", "1",
            "-flush_packets", "1", "-f", "adts", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self.tasks.append(self.hass.async_create_background_task(self._pump(), f"hadash door talk {self.entity_id}"))
        if self.closed:
            await self.stop()
            return
        self._say("open")

    async def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        buf = bytearray()
        while True:
            chunk = await self.proc.stdout.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            for frame in adts_frames(buf):
                if self.ws is None or self.ws.closed:
                    return
                # Sent without waiting for each answer: the sound must not queue up behind the replies.
                self.n += 1
                await self.ws.send_str(json.dumps({
                    "messageId": f"hadash-talk-{self.n}", "command": "device.talkback_audio_data",
                    "serialNumber": self.serial, "buffer": {"type": "Buffer", "data": list(frame)},
                }))

    def feed(self, audio: bytes) -> None:
        if self.step != "open" or self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
            return
        self.proc.stdin.write(audio)

    async def say(self, text: str) -> None:
        """A quick reply: Home Assistant's own voice (the preferred Assist voice) speaks it at the door."""
        engine = language = None
        options: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            from homeassistant.components.assist_pipeline import async_get_pipeline  # noqa: PLC0415

            pipeline = async_get_pipeline(self.hass)
            engine, language = pipeline.tts_engine, pipeline.tts_language
            if pipeline.tts_voice:
                options = {"voice": pipeline.tts_voice}
        from homeassistant.components import tts  # noqa: PLC0415

        source = tts.generate_media_source_id(self.hass, text, engine=engine, language=language, options=options)
        _ext, data = await tts.async_get_media_source_audio(self.hass, source)
        proc = await asyncio.create_subprocess_exec(
            _ffmpeg(self.hass), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-f", "mulaw", "-ar", "16000", "-ac", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        pcm, _ = await proc.communicate(data)
        # At the pace of speech, so the doorbell's buffer never overflows: a tenth of a second each time.
        for at in range(0, len(pcm), 1600):
            if self.closed:
                return
            self.feed(pcm[at : at + 1600])
            await asyncio.sleep(0.1)

    async def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.ws is not None and not self.ws.closed and self.serial:
            with contextlib.suppress(Exception):
                await self._ask({"command": "device.stop_talkback", "serialNumber": self.serial})
        if self.proc is not None:
            with contextlib.suppress(Exception):
                self.proc.kill()
                await self.proc.wait()
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        for task in self.tasks:
            task.cancel()
        if self.leave_video is not None:
            self.leave_video()
            self.leave_video = None


class DoorTalks:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.talks: dict[str, Talk] = {}

    def begin(self, talk_id: str, entity_id: str, send: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        # One talk per door: the newest tablet takes over.
        for other in [t for t in self.talks.values() if t.entity_id == entity_id]:
            other._say("ended", "taken")
            self._end(other)
        talk = Talk(self.hass, talk_id, entity_id, send)
        self.talks[talk_id] = talk
        self.hass.async_create_background_task(talk.start(), f"hadash door talk start {entity_id}")

        @callback
        def leave() -> None:
            if self.talks.get(talk_id) is talk:
                self._end(talk)

        return leave

    def _end(self, talk: Talk) -> None:
        self.talks.pop(talk.id, None)
        self.hass.async_create_background_task(talk.stop(), f"hadash door talk stop {talk.entity_id}")


def talks(hass: HomeAssistant) -> DoorTalks:
    if KEY not in hass.data:
        hass.data[KEY] = DoorTalks(hass)
    return hass.data[KEY]


async def async_setup(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_door_talk)
    websocket_api.async_register_command(hass, ws_door_audio)
    websocket_api.async_register_command(hass, ws_door_say)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/door/talk",
        vol.Required("talk"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("entity_id"): cv.entity_id,
    }
)
@callback
def ws_door_talk(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Open the doorbell's speaker. Events: {"step": "waking"|"open"|"failed"|"ended", "why"?: text}."""
    if hass.data[PRO].state()["plan"] == "free":
        connection.send_error(msg["id"], "pro", "Talk at the door is part of Dashboard Studio Pro.")
        return

    @callback
    def send(event: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = talks(hass).begin(msg["talk"], msg["entity_id"], send)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/door/audio",
        vol.Required("talk"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("audio"): vol.All(str, vol.Length(min=1, max=MAX_PIECE)),
    }
)
@callback
def ws_door_audio(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    talk = talks(hass).talks.get(msg["talk"])
    if talk is None:
        connection.send_error(msg["id"], "no_talk", "That talk has ended.")
        return
    try:
        talk.feed(base64.b64decode(msg["audio"]))
    except (ValueError, OSError):
        pass
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/door/say",
        vol.Required("talk"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("text"): vol.All(str, vol.Length(min=1, max=200)),
    }
)
@websocket_api.async_response
async def ws_door_say(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A quick reply at the door, in the home's own voice. The result comes once it has been said."""
    talk = talks(hass).talks.get(msg["talk"])
    if talk is None or talk.step != "open":
        connection.send_error(msg["id"], "no_talk", "The door is not open for talk.")
        return
    try:
        await talk.say(msg["text"])
    except Exception as err:  # noqa: BLE001 - the tablet shows that it did not work
        connection.send_error(msg["id"], "say_failed", str(err)[:160])
        return
    connection.send_result(msg["id"])
