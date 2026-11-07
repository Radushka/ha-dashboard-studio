"""The Assist satellite entity of each wall tablet (satellite.py has the tablets and the websocket side)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Callable
from typing import Any

from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
)
from homeassistant.components.assist_satellite.entity import AssistSatelliteState
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, PRO, SATELLITES
from .satellite import SIGNAL_NEW, Satellites, Tablet, signal_seen

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    satellites: Satellites = hass.data[SATELLITES]

    @callback
    def add(tablet: Tablet) -> None:
        async_add_entities([WallSatellite(satellites, tablet)])

    for tablet in list(satellites.tablets.values()):
        add(tablet)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW, add))


def device_info(tablet: Tablet) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, tablet.id)})


class WallSatellite(AssistSatelliteEntity):
    """A wall tablet: Home Assistant hears it, and it plays what Home Assistant says."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, satellites: Satellites, tablet: Tablet) -> None:
        self._satellites = satellites
        self._tablet = tablet
        self._attr_unique_id = f"{tablet.id}-assist_satellite"
        self._attr_device_info = device_info(tablet)
        # The run that listens now: where its events go, and the number its sound comes with.
        self._send: Callable[[dict[str, Any]], None] | None = None
        self._handler = 0

    @property
    def available(self) -> bool:
        return self._tablet.connected and self._satellites.pro

    @property
    def supported_features(self) -> AssistSatelliteEntityFeature:
        # Any tablet can speak. Only a tablet with a microphone that works (our wall app, or https) can listen.
        features = AssistSatelliteEntityFeature.ANNOUNCE
        if self._tablet.listen:
            features |= AssistSatelliteEntityFeature.START_CONVERSATION
        return features

    def _select(self, key: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id(Platform.SELECT, DOMAIN, f"{self._tablet.id}-{key}")

    @property
    def pipeline_entity_id(self) -> str | None:
        return self._select("pipeline")

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        return self._select("vad_sensitivity")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._tablet.satellite = self
        self.async_on_remove(async_dispatcher_connect(self.hass, signal_seen(self._tablet.id), self.async_write_ha_state))
        self.async_on_remove(self.hass.data[PRO].async_listen(lambda *_: self.async_write_ha_state()))

    async def async_will_remove_from_hass(self) -> None:
        if self._tablet.satellite is self:
            self._tablet.satellite = None
        await super().async_will_remove_from_hass()

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        # The wake word is chosen for each screen in the studio, not here.
        raise NotImplementedError

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        raise NotImplementedError

    # Listening

    async def run(
        self,
        stream: AsyncIterable[bytes],
        send: Callable[[dict[str, Any]], None],
        handler: int,
        phrase: str | None,
        level: float | None,
        answer: bool,
    ) -> None:
        """One question from the tablet: its sound goes through Home Assistant's pipeline, the events go back."""
        if phrase and not await self._satellites.claim(self._tablet, phrase, level):
            # Another tablet heard the word more clearly, so it is closer: this one stops quietly.
            send({"type": "error", "data": {"code": "duplicate_wake_up_detected", "message": "Another tablet answers"}})
            return
        self._send, self._handler = send, handler
        try:
            await self.async_accept_pipeline_from_satellite(
                stream,
                start_stage=PipelineStage.STT,
                # Only the words: the tablet tries its own wall commands first, then asks Assist with its device.
                # A conversation Home Assistant started is answered by Home Assistant.
                end_stage=PipelineStage.TTS if answer else PipelineStage.STT,
                wake_word_phrase=phrase,
            )
        finally:
            if self._send is send:
                self._send = None

    @callback
    def on_pipeline_event(self, event: PipelineEvent) -> None:
        send = self._send
        if send is None:
            return
        data = dict(event.data or {})
        if event.type is PipelineEventType.RUN_START:
            data["runner_data"] = {"stt_binary_handler_id": self._handler}
            # A question from an automation: the tablet shows the words it heard and leaves the rest to it.
            data["question"] = self._ask_question_future is not None
        send({"type": str(event.type), "data": data, "timestamp": event.timestamp})

    @callback
    def connection_lost(self) -> None:
        self._send = None
        self._set_state(AssistSatelliteState.IDLE)

    @callback
    def tablet_state(self, state: str) -> None:
        """The tablet works out an answer or speaks it. Done: back to idle, as after a spoken answer."""
        if state == "processing":
            self._set_state(AssistSatelliteState.PROCESSING)
        elif state == "responding":
            self._set_state(AssistSatelliteState.RESPONDING)
        else:
            self.tts_response_finished()

    # Speaking

    def _play(self, announcement: AssistSatelliteAnnouncement, listen: bool) -> dict[str, Any]:
        return {
            "action": "announce",
            "url": announcement.media_id,
            "pre": announcement.preannounce_media_id or "",
            "message": announcement.message,
            "listen": listen,
            "question": listen and self._ask_question_future is not None,
        }

    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        await self._satellites.play(self._tablet, self._play(announcement, False))

    async def async_start_conversation(self, start_announcement: AssistSatelliteAnnouncement) -> None:
        # The tablet plays the first sentence, then listens; its words come back through run().
        await self._satellites.play(self._tablet, self._play(start_announcement, True))
