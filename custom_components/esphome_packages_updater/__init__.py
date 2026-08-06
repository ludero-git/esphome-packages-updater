import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.components.hassio import get_addons_info # May get deprecated?
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_platform, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,

    CONF_INTERVAL,
    CONF_EXPOSE_UPDATE_ENTITIES,
    CONF_AUTO_INSTALL,
    CONF_SELECTIVE_UPDATE_CHECK,

    ESPHOME_DEVICE_BUILDER_SLUG,
    ESPHOME_CONFIG_DIR,
    ESPHOME_DOMAIN,
    UPDATE_ENTITY_PREFIX,
    STORAGE_VERSION,
    INSTALL_TIMEOUT,
    INSTALL_POLL_INTERVAL,
)
from .config_parser import (
    load_secrets,
    get_config_files,
    parse_config_file,
)
from .fetcher import sync_repo, get_last_commit_hash, GitError
from .update import ESPHomePackageUpdateEntity


_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["update"]


@dataclass
class DeviceStatus:
    name: str
    friendly_name: str
    latest_version: str


def _get_entity_device_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the display name of the device an entity belongs to."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    
    entry = ent_reg.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return None
    
    device = dev_reg.async_get(entry.device_id)
    if device is None:
        return None
    
    return device.name_by_user or device.name


class ESPHomePackagesUpdaterManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, config: dict) -> None:
        self.hass = hass
        self.entry = entry
        self.config = config
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self.installed: dict[str, str] = {}
        self.devices: dict[str, DeviceStatus] = {}
        self.entities: dict[str, ESPHomePackageUpdateEntity] = {}
        self.async_add_entities: AddEntitiesCallback | None = None

    async def async_load(self) -> None:
        data = await self.store.async_load()
        if data:
            self.installed = data.get("installed", {})

    async def async_save(self) -> None:
        await self.store.async_save({"installed": self.installed})

    def ensure_entity(self, name: str) -> None:
        if not self.config.get(CONF_EXPOSE_UPDATE_ENTITIES, True):
            return
        if name in self.entities or self.async_add_entities is None:
            return
        
        entity = ESPHomePackageUpdateEntity(self, name)
        self.entities[name] = entity
        self.async_add_entities([entity])

    async def async_run_cycle(self) -> None:
        _LOGGER.debug("Starting cycle")

        esphome_path = Path(ESPHOME_CONFIG_DIR)
        secrets = await load_secrets(self.hass, esphome_path)
        config_files = await get_config_files(self.hass, esphome_path)

        selective = self.config.get(CONF_SELECTIVE_UPDATE_CHECK, False)
        seen_devices = []

        for file in config_files:
            device = await parse_config_file(self.hass, file, secrets)
            if not device:
                continue

            pkg_hashes = []

            for pkg in device.packages:
                try:
                    # Clone or update the package repo.
                    repo_path = await sync_repo(
                        self.hass,
                        pkg.url,
                        reference=pkg.reference,
                        username=pkg.username,
                        password=pkg.password,
                        shallow=not selective,
                    )
                except GitError:
                    _LOGGER.error(
                        "Failed to sync package repo for device %s (%s); skipping",
                        device.name,
                        pkg.url,
                    )
                    continue

                if selective:
                    for pkg_file in pkg.files:
                        commit_hash = await get_last_commit_hash(self.hass, repo_path, path=pkg_file)
                        if commit_hash:
                            pkg_hashes.append(commit_hash)
                else:
                    commit_hash = await get_last_commit_hash(self.hass, repo_path)
                    if commit_hash:
                        pkg_hashes.append(commit_hash)

            if not pkg_hashes:
                continue

            # Create hash of each package hash for versioning.
            latest_version = hashlib.sha256(
                "|".join(sorted(pkg_hashes)).encode("utf-8")
            ).hexdigest()[:12]

            self.devices[device.friendly_name] = DeviceStatus(device.name, device.friendly_name, latest_version)
            self.ensure_entity(device.friendly_name)
            seen_devices.append(device.friendly_name)

        for name in seen_devices:
            entity = self.entities.get(name)
            if entity:
                entity.async_write_ha_state()

        if self.config.get(CONF_AUTO_INSTALL, False):
            for name in seen_devices:
                status = self.devices[name]
                if self.installed.get(name) != status.latest_version:
                    await self.async_install_device(name)

    def _find_esphome_update_entity(self, name: str):
        platforms = entity_platform.async_get_platforms(self.hass, ESPHOME_DOMAIN)
        for platform in platforms:
            for entity_id, entity in platform.entities.items():
                if not entity_id.startswith(UPDATE_ENTITY_PREFIX):
                    continue

                dev_name = _get_entity_device_name(self.hass, entity_id)
                if dev_name and dev_name == name:
                    return entity
        return None

    async def _wait_for_install(self, entity) -> bool:
        deadline = self.hass.loop.time() + INSTALL_TIMEOUT
        while self.hass.loop.time() < deadline:
            if not getattr(entity, "in_progress", False):
                return True
            await asyncio.sleep(INSTALL_POLL_INTERVAL)
        return False

    async def async_install_device(self, name: str) -> bool:
        status = self.devices.get(name)
        if status is None:
            return False

        entity = self._find_esphome_update_entity(name)
        if entity is None:
            _LOGGER.warning("No esphome update entity found for %s; skipping install", status.name)
            return False

        _LOGGER.info("Installing update for %s (%s)", status.name, entity.entity_id)
        try:
            # Call the internal install function (forcing an update).
            # Normally this is only used for firmware updates.
            await entity.async_install(version=None, backup=False)
        except Exception as exc:
            _LOGGER.exception("Install call failed for %s: %s", entity.entity_id, str(exc))
            return False

        if not await self._wait_for_install(entity):
            _LOGGER.warning(
                "%s install still in progress after %ss timeout", entity.entity_id, INSTALL_TIMEOUT
            )
            return False

        # Save the status (avoiding repeat installs).
        self.installed[name] = status.latest_version
        await self.async_save()

        # Publish update status to Home Assistant.
        update_entity = self.entities.get(name)
        if update_entity:
            update_entity.async_write_ha_state()

        _LOGGER.info("%s install finished", entity.entity_id)
        return True


def _is_device_builder_installed(hass: HomeAssistant):
    """Check if the ESPHome Device Builder app is installed."""
    addons = get_addons_info(hass)
    return ESPHOME_DEVICE_BUILDER_SLUG in addons if addons else False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    if not _is_device_builder_installed(hass):
        _LOGGER.warning("ESPHome Device Builder was not found!")

    config = {**entry.data, **entry.options}

    manager = ESPHomePackagesUpdaterManager(hass, entry, config)
    await manager.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _tick(_now=None) -> None:
        _LOGGER.debug("Tick triggered")
        try:
            await manager.async_run_cycle()
        except Exception:
            _LOGGER.exception("Update cycle failed")

    if hass.is_running:
        await _tick()
    else:
        # Start tick when HA starts.
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _tick)

    unsub = async_track_time_interval(
        hass, _tick, timedelta(minutes=config[CONF_INTERVAL])
    )
    entry.async_on_unload(unsub)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)