"""The two voice settings of each wall tablet, as with other Assist devices: which assistant, and how long a pause
ends a sentence. Home Assistant's own select entities, so they look and work the same as everywhere else."""

from __future__ import annotations

from homeassistant.components.assist_pipeline.select import (
    AssistPipelineSelect,
    VadSensitivitySelect,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .assist_satellite import device_info
from .const import DOMAIN, SATELLITES
from .satellite import SIGNAL_NEW, Satellites, Tablet


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    satellites: Satellites = hass.data[SATELLITES]

    @callback
    def add(tablet: Tablet) -> None:
        pipeline = AssistPipelineSelect(hass, DOMAIN, tablet.id)
        pause = VadSensitivitySelect(hass, tablet.id)
        for select in (pipeline, pause):
            select._attr_device_info = device_info(tablet)
            select._attr_has_entity_name = True
        async_add_entities([pipeline, pause])

    for tablet in list(satellites.tablets.values()):
        add(tablet)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW, add))
