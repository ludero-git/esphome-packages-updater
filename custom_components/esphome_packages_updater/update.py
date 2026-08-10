from __future__ import annotations

import logging

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
    UpdateDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    manager.async_add_entities = async_add_entities
    if manager.devices:
        for slug in list(manager.devices):
            manager.ensure_entity(slug)


class ESPHomePackageUpdateEntity(UpdateEntity):
    _attr_has_entity_name = True
    _attr_name = "Package update"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, manager, slug: str) -> None:
        self._manager = manager
        self._slug = slug
        status = manager.devices[slug]
        self._attr_unique_id = f"{manager.entry.entry_id}_{slug}_package_update"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, slug)},
            name=status.name,
        )

    @property
    def installed_version(self) -> str | None:
        return self._manager.installed.get(self._slug)

    @property
    def latest_version(self) -> str | None:
        status = self._manager.devices.get(self._slug)
        return status.latest_version if status else None

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            await self._manager.async_install_device(self._slug)
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()