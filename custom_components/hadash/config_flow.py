"""Adding Dashboard Studio from Settings > Devices & services. There is nothing to fill in."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class DashboardStudioFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Dashboard Studio", data={})
        return self.async_show_form(step_id="user")
