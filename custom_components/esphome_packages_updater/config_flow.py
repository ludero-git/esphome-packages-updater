from __future__ import annotations
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_INTERVAL,
    CONF_EXPOSE_UPDATE_ENTITIES,
    CONF_AUTO_INSTALL,
    CONF_SELECTIVE_UPDATE_CHECK,

    DEFAULT_INTERVAL,
    MIN_INTERVAL,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_INTERVAL, default=defaults.get(CONF_INTERVAL, DEFAULT_INTERVAL)
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_INTERVAL)),
            vol.Required(
                CONF_EXPOSE_UPDATE_ENTITIES,
                default=defaults.get(CONF_EXPOSE_UPDATE_ENTITIES, True),
            ): bool,
            vol.Required(
                CONF_AUTO_INSTALL, default=defaults.get(CONF_AUTO_INSTALL, False)
            ): bool,
            vol.Required(
                CONF_SELECTIVE_UPDATE_CHECK,
                default=defaults.get(CONF_SELECTIVE_UPDATE_CHECK, True),
            ): bool,
        }
    )


class ESPHomePackagesUpdaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:

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