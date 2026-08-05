"""Config flow for ESPHome Packages Updater."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_INTERVAL,
    CONF_SKIP_NONEXISTENT,
    
    DEFAULT_INTERVAL,
    MIN_INTERVAL
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_INTERVAL, default=defaults.get(CONF_INTERVAL, DEFAULT_INTERVAL)
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_INTERVAL)),
            vol.Required(
                CONF_SKIP_NONEXISTENT, default=defaults.get(CONF_SKIP_NONEXISTENT, False)
            ): bool,
        }
    )


class ESPHomePackagesUpdaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="ESPHome Packages Updater", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ESPHomePackagesUpdaterOptionsFlow:
        return ESPHomePackagesUpdaterOptionsFlow()


class ESPHomePackagesUpdaterOptionsFlow(config_entries.OptionsFlow):
    """Handle updates to the options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))