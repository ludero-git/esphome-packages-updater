import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.components.hassio import get_addons_info
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
    ESPHOME_COMPILATION_TIME_FORMATS,
    ESPHOME_DEVICE_BUILDER_SLUG,
    ESPHOME_CONFIG_DIR,
    ESPHOME_DOMAIN,
    UPDATE_ENTITY_PREFIX,
    STORAGE_VERSION,
    INSTALL_TIMEOUT,
    INSTALL_POLL_INTERVAL,
)
from .config_parser import load_secrets, get_config_files, parse_config_file
from .fetcher import sync_repo, get_last_commit_hash, get_last_commit_time, GitError
from .update import ESPHomePackageUpdateEntity


_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["update"] # Enables update.py usage.


@dataclass
class DeviceStatus:
    name: str
    latest_version: str


def _parse_compilation_time(raw: str) -> datetime | None:
    if not raw:
        return None

    normalized = re.sub(r"\s+", " ", raw.strip())

    for fmt in ESPHOME_COMPILATION_TIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue

    return None


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


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
        """Load data from store."""
        data = await self.store.async_load() or {}
        self.installed = data.get("installed", {})

        current_selective = self.config.get(CONF_SELECTIVE_UPDATE_CHECK, False)
        stored_selective = data.get("selective")

        if self.installed and stored_selective is not None and stored_selective != current_selective:
            _LOGGER.info("Selective update check setting changed; resetting installed state")
            self.installed = {}

    async def async_save(self) -> None:
        """Save data to store."""
        await self.store.async_save({
            "installed": self.installed,
            "selective": self.config.get(CONF_SELECTIVE_UPDATE_CHECK, False),
        })

    def ensure_entity(self, name: str) -> None:
        if not self.config.get(CONF_EXPOSE_UPDATE_ENTITIES, True):
            return

        if name in self.entities or self.async_add_entities is None:
            return

        entity = ESPHomePackageUpdateEntity(self, name)
        self.entities[name] = entity
        self.async_add_entities([entity])

    def _esphome_compile_times(self) -> dict[str, datetime]:
        result: dict[str, datetime] = {}

        for entry in self.hass.config_entries.async_entries(ESPHOME_DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue

            device_info = getattr(getattr(entry, "runtime_data", None), "device_info", None)
            if device_info is None:
                continue

            compiled_at = _parse_compilation_time(device_info.compilation_time)
            if compiled_at is None:
                continue

            for candidate in (device_info.name, getattr(device_info, "friendly_name", None)):
                if candidate:
                    result[candidate] = compiled_at

        return result

    @staticmethod
    def _is_up_to_date(compiled_at: datetime | None, commit_times: list[datetime]) -> bool:
        if compiled_at is None:
            return False

        compiled_at = _naive(compiled_at)
        return all(_naive(commit_time) <= compiled_at for commit_time in commit_times)

    async def _collect_package_versions(self, device, selective: bool):
        """Get commit hashes and times of packages."""
        pkg_hashes = []
        pkg_commit_times = []

        for pkg in device.packages:
            try:
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

            paths = pkg.files if selective else [None]

            for path in paths:
                commit_hash = await get_last_commit_hash(self.hass, repo_path, path=path)
                if commit_hash:
                    pkg_hashes.append(commit_hash)

                commit_time = await get_last_commit_time(self.hass, repo_path, path=path)
                if commit_time:
                    pkg_commit_times.append(commit_time)

        return pkg_hashes, pkg_commit_times

    async def _process_device_file(self, file, secrets, selective: bool, compile_times: dict) -> str | None:
        device = await parse_config_file(self.hass, file, secrets)
        if not device:
            return None

        pkg_hashes, pkg_commit_times = await self._collect_package_versions(device, selective)
        if not pkg_hashes:
            return None

        latest_version = hashlib.sha256(
            "|".join(sorted(pkg_hashes)).encode("utf-8")
        ).hexdigest()[:12]

        is_new_device = device.friendly_name not in self.installed
        ha_device = await self._find_esphome_device(device.friendly_name) if is_new_device else None

        if is_new_device and ha_device:
            compiled_at = compile_times.get(device.friendly_name)
            up_to_date = self._is_up_to_date(compiled_at, pkg_commit_times)

            _LOGGER.debug(
                "New device %s is %s. used compilation time.",
                device.friendly_name,
                "up to date" if up_to_date else "outdated",
            )

            self.installed[device.friendly_name] = latest_version if up_to_date else ""

        self.devices[device.friendly_name] = DeviceStatus(device.name, latest_version)
        self.ensure_entity(device.friendly_name)

        return device.friendly_name

    async def async_run_cycle(self) -> None:
        _LOGGER.debug("Starting cycle")

        esphome_path = Path(ESPHOME_CONFIG_DIR)
        secrets = await load_secrets(self.hass, esphome_path)
        config_files = await get_config_files(self.hass, esphome_path)
        selective = self.config.get(CONF_SELECTIVE_UPDATE_CHECK, False)
        compile_times = self._esphome_compile_times()

        seen_devices = []
        installed_before = dict(self.installed)

        for file in config_files:
            name = await self._process_device_file(file, secrets, selective, compile_times)
            if name:
                seen_devices.append(name)

        if self.installed != installed_before:
            await self.async_save()

        for name in seen_devices:
            entity = self.entities.get(name)
            if entity:
                entity.async_write_ha_state()

        if self.config.get(CONF_AUTO_INSTALL, False):
            for name in seen_devices:
                if self.installed.get(name) != self.devices[name].latest_version:
                    await self.async_install_device(name)

    async def _find_esphome_device(self, name: str):
        device_registry = dr.async_get(self.hass)

        for device in device_registry.devices.values():
            if device.name and str(device.name).lower() == name:
                return device

        return None

    def _entity_device_name(self, entity_id: str) -> str | None:
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.device_id is None:
            return None

        device = dev_reg.async_get(entry.device_id)
        return str(device.name).lower() if device and device.name else None

    def _find_esphome_update_entity(self, name: str):
        for platform in entity_platform.async_get_platforms(self.hass, ESPHOME_DOMAIN):
            for entity_id, entity in platform.entities.items():
                if not entity_id.startswith(UPDATE_ENTITY_PREFIX):
                    continue

                if self._entity_device_name(entity_id) == name:
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
            await entity.async_install(version=None, backup=False)
        except Exception as exc:
            _LOGGER.exception("Install call failed for %s: %s", entity.entity_id, str(exc))
            return False

        if not await self._wait_for_install(entity):
            _LOGGER.warning(
                "%s install still in progress after %ss timeout", entity.entity_id, INSTALL_TIMEOUT
            )
            return False

        self.installed[name] = status.latest_version
        await self.async_save()

        update_entity = self.entities.get(name)
        if update_entity:
            update_entity.async_write_ha_state()

        _LOGGER.info("%s install finished", entity.entity_id)
        return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    addons = get_addons_info(hass)
    if not addons or ESPHOME_DEVICE_BUILDER_SLUG not in addons:
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
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _tick)

    unsub = async_track_time_interval(hass, _tick, timedelta(minutes=config[CONF_INTERVAL]))
    entry.async_on_unload(unsub)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unloaded


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)