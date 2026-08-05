from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import yaml

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_INTERVAL,
    CONF_SKIP_NONEXISTENT,
    UPDATE_ENTITY_PREFIX,
    ESPHOME_CONFIG_DIR,
    ESPHOME_DOMAIN,
)

LOGGER = logging.getLogger(__name__)
PLATFORMS: list[str] = []

COMPILATION_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%b %d %Y, %H:%M:%S",
)

INSTALL_TIMEOUT = 300
INSTALL_POLL_INTERVAL = 5


@dataclass
class PackageRef:
    """A git package referenced by an ESPHome device config."""

    url: str
    username: str | None = None
    password: str | None = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    config = {**entry.data, **entry.options}

    async def _tick(_now=None) -> None:
        LOGGER.debug("Tick triggered")
        try:
            await _run_update_cycle(hass, config[CONF_SKIP_NONEXISTENT])
        except Exception:
            LOGGER.exception("Update cycle failed")

    hass.data.setdefault(DOMAIN, {})["config"] = config

    if hass.is_running:
        await _tick()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _tick)

    unsub = async_track_time_interval(
        hass, _tick, timedelta(minutes=config[CONF_INTERVAL])
    )
    entry.async_on_unload(unsub)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.pop(DOMAIN, None)
    return True


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _redact_url(text: str) -> str:
    """Strip credentials from a URL so it is safe to log."""
    return re.sub(r"://[^@/\s]+@", "://", text)


def _normalize_git_url(url: str) -> str:
    """Strip credentials and a trailing .git suffix from a git URL."""
    normalized = _redact_url(url).strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized


def _build_authenticated_url(url: str, username: str | None, password: str | None) -> str:
    """Insert credentials into an https URL for git operations."""
    if not username or not password or not url.startswith("https://"):
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{rest}"


def _slugify_device_name(name: str) -> str:
    """Convert a device name to the slug format Home Assistant uses."""
    value = name.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)

    return value.strip("-")


# Custom YAML loader that ignores ESPHome's custom tags (!secret, !lambda, etc.)
# instead of raising on them, since we only need plain data here.

class _ESPHomeYamlLoader(yaml.SafeLoader):
    """YAML loader that tolerates ESPHome's custom tags."""


def _construct_unknown_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    """Construct a value for any tag not otherwise recognized by the loader."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_ESPHomeYamlLoader.add_multi_constructor("!", _construct_unknown_tag)


def _parse_config(config_path: Path) -> dict | None:
    """Parse an ESPHome device configuration file."""
    try:
        with open(config_path, "r", encoding="utf-8") as conf:
            return yaml.load(conf, Loader=_ESPHomeYamlLoader)
    except Exception:
        LOGGER.exception("Failed to parse %s", config_path)
        return None


async def _get_configs(hass: HomeAssistant, esphome_path: Path) -> list[Path]:
    """Return the list of ESPHome device config files, excluding secrets.yaml."""
    def list_configs() -> list[Path]:
        if not esphome_path.is_dir():
            return []
        return [
            f
            for f in esphome_path.iterdir()
            if f.is_file() and f.suffix == ".yaml" and f.name != "secrets.yaml"
        ]

    configs = await hass.async_add_executor_job(list_configs)
    LOGGER.debug("Found %d esphome config file(s)", len(configs))
    return configs


async def _load_secrets(hass: HomeAssistant, esphome_path: Path) -> dict[str, str]:
    """Read and parse the ESPHome secrets file, if one exists."""
    secrets_path = esphome_path / "secrets.yaml"

    def read_secrets() -> dict[str, str]:
        if not secrets_path.is_file():
            LOGGER.debug("No secrets.yaml found at %s", secrets_path)
            return {}
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            LOGGER.exception("Failed to parse secrets.yaml at %s", secrets_path)
            return {}
        return data if isinstance(data, dict) else {}

    secrets = await hass.async_add_executor_job(read_secrets)
    LOGGER.debug("Loaded %d secret(s) from secrets.yaml", len(secrets))
    return secrets


def _resolve_secret(device_name: str, key: str | None, secrets: dict[str, str]) -> str | None:
    """Resolve a secret, preferring a device-specific value over a shared one."""
    if key is None:
        return None

    # Device-specific secret, e.g. "living-room__wifi_password"
    device_key = f"{device_name}__{key}"
    if device_key in secrets:
        return secrets[device_key]

    # Fall back to a shared secret; if it doesn't exist either, use the raw
    # key so the failure is at least visible instead of silently swallowed
    return secrets.get(key, key)

async def _get_config_data(
    hass: HomeAssistant, config_path: Path, secrets: dict[str, str]
) -> tuple[str, list[PackageRef]] | None:
    """Parse a config file and extract its device name and package references."""
    parsed = await hass.async_add_executor_job(_parse_config, config_path)
    if not parsed:
        LOGGER.warning("Failed to parse %s; skipping file", config_path)
        return None

    esphome_block = parsed.get("esphome")
    if not esphome_block:
        LOGGER.warning("No esphome block found in %s; skipping file", config_path)
        return None

    device_name = esphome_block.get("friendly_name")
    if not device_name:
        raw_name = esphome_block.get("device_name")
        device_name = _slugify_device_name(raw_name) if raw_name else None

    if not device_name:
        LOGGER.warning("No device name found in %s; skipping file", config_path)
        return None

    packages_block = parsed.get("packages")
    if not packages_block:
        LOGGER.warning("No packages block found in %s; skipping file", config_path)
        return None

    packages: list[PackageRef] = []
    for name, data in packages_block.items():
        if not isinstance(data, dict):
            continue

        url = data.get("url")
        if not url:
            continue

        username = _resolve_secret(device_name, data.get("username"), secrets)
        password = _resolve_secret(device_name, data.get("password"), secrets)
        packages.append(PackageRef(_normalize_git_url(url), username, password))

    if not packages:
        LOGGER.warning("No packages with valid URLs found in %s; skipping file", config_path)
        return None

    LOGGER.debug(
        "%s uses packages: %s", device_name, [_redact_url(p.url) for p in packages]
    )
    return device_name, packages


async def _get_all_configs_data(
    hass: HomeAssistant, esphome_path: Path
) -> dict[str, list[PackageRef]]:
    """Build a mapping of device name to its package references for every config file."""
    secrets = await _load_secrets(hass, esphome_path)
    configs = await _get_configs(hass, esphome_path)
    device_packages: dict[str, list[PackageRef]] = {}

    for config_path in configs:
        data = await _get_config_data(hass, config_path, secrets)
        if data:
            device_name, packages = data
            device_packages[device_name] = packages

    return device_packages


def _parse_compilation_time(raw: str) -> datetime | None:
    """Parse ESPHome's compilation_time string, trying each known format."""
    if not raw:
        return None
    normalized = re.sub(r"\s+", " ", raw.strip())

    for fmt in COMPILATION_TIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue

    LOGGER.debug("Could not parse compilation_time %r", raw)
    return None


def _get_device_compilation_times(hass: HomeAssistant) -> dict[str, datetime]:
    """Return a mapping of device slug to firmware compilation time for loaded ESPHome entries."""
    compile_times: dict[str, datetime] = {}

    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            LOGGER.debug("Skipping esphome entry %s, not loaded", entry.title)
            continue

        entry_data = getattr(entry, "runtime_data", None)
        device_info = getattr(entry_data, "device_info", None) if entry_data else None
        if device_info is None:
            LOGGER.debug("No device_info yet for esphome entry %s", entry.title)
            continue

        compiled_at = _parse_compilation_time(device_info.compilation_time)
        if compiled_at is None:
            continue

        for candidate in (device_info.name, getattr(device_info, "friendly_name", None)):
            if candidate:
                compile_times[_slugify_device_name(candidate)] = compiled_at

    return compile_times


def _remote_is_newer(remote_time: datetime, compiled_at: datetime) -> bool:
    """Check whether a remote commit is newer than the device's firmware."""
    if compiled_at.tzinfo is not None:
        return remote_time > compiled_at
    return remote_time.replace(tzinfo=None) > compiled_at


async def _get_remote_commit_time(hass: HomeAssistant, package: PackageRef) -> datetime | None:
    """Clone a package's repo and return the timestamp of its latest commit."""
    auth_url = _build_authenticated_url(package.url, package.username, package.password)

    def clone_and_read() -> datetime | None:
        # Disable git's interactive prompt so a bad or missing credential fails
        # right away instead of hanging.
        env = {"GIT_TERMINAL_PROMPT": "0"}

        with tempfile.TemporaryDirectory() as tmp:
            try:
                # Depth 1 is enough since only the latest commit date is needed
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--quiet", auth_url, tmp],
                    check=True,
                    capture_output=True,
                    timeout=30,
                    env=env,
                )
                result = subprocess.run(
                    ["git", "-C", tmp, "log", "-1", "--format=%cI"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                LOGGER.warning("Timed out cloning %s", _redact_url(package.url))
                return None
            except subprocess.CalledProcessError as err:
                stderr = err.stderr
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors="replace")
                LOGGER.warning(
                    "git failed checking %s: %s",
                    _redact_url(package.url),
                    _redact_url(stderr.strip()),
                )
                return None

            raw = result.stdout.strip()
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                LOGGER.debug("Could not parse commit date %r for %s", raw, package.url)
                return None

    try:
        return await hass.async_add_executor_job(clone_and_read)
    except Exception:
        LOGGER.exception("Unexpected error checking remote commit date for %s", _redact_url(package.url))
        return None


async def _find_devices_to_update(
    hass: HomeAssistant,
    device_packages: dict[str, list[PackageRef]],
    skip_nonexistent: bool,
) -> set[str]:
    """Return the names of devices whose packages have commits newer than their firmware."""
    compile_times = _get_device_compilation_times(hass)

    # Check each unique package URL once, even if several devices share it
    unique_packages: dict[str, PackageRef] = {}
    for packages in device_packages.values():
        for package in packages:
            unique_packages.setdefault(package.url, package)

    remote_times: dict[str, datetime | None] = {}
    for url, package in unique_packages.items():
        remote_times[url] = await _get_remote_commit_time(hass, package)
        LOGGER.debug("Remote commit time for %s: %s", _redact_url(url), remote_times[url])

    devices_to_update: set[str] = set()

    for device_name, packages in device_packages.items():
        compiled_at = compile_times.get(_slugify_device_name(device_name))

        if compiled_at is None:
            if skip_nonexistent:
                LOGGER.info("Skipping %s: compilation time unknown", device_name)
                continue
            LOGGER.info(
                "%s has no known compilation time, triggering an update to be safe",
                device_name,
            )
            devices_to_update.add(device_name)
            continue

        LOGGER.debug("%s was compiled at %s", device_name, compiled_at)

        for package in packages:
            remote_time = remote_times.get(package.url)
            if remote_time and _remote_is_newer(remote_time, compiled_at):
                LOGGER.info(
                    "%s: package %s has a commit newer than this device's firmware",
                    device_name,
                    _redact_url(package.url),
                )
                devices_to_update.add(device_name)
                break

    return devices_to_update


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


async def _wait_for_in_progress_done(hass: HomeAssistant, entity) -> bool:
    """Poll an update entity until its install is no longer in progress."""
    deadline = hass.loop.time() + INSTALL_TIMEOUT
    while hass.loop.time() < deadline:
        if not getattr(entity, "in_progress", False):
            return True
        await asyncio.sleep(INSTALL_POLL_INTERVAL)
    return False


async def _install_update(hass: HomeAssistant, entity) -> bool:
    """Trigger an install on an update entity and wait for it to finish."""
    try:
        await entity.async_install(version=None, backup=False)
    except Exception:
        LOGGER.exception("Install call failed for %s", entity.entity_id)
        return False

    if not await _wait_for_in_progress_done(hass, entity):
        LOGGER.warning("%s install still in progress after %ss timeout", entity.entity_id, INSTALL_TIMEOUT)
        return False

    LOGGER.info("%s install finished", entity.entity_id)
    return True


async def _run_update_cycle(hass: HomeAssistant, skip_nonexistent: bool) -> None:
    """Check all devices for package updates and install them one at a time."""
    esphome_path = Path(ESPHOME_CONFIG_DIR)

    try:
        device_packages = await _get_all_configs_data(hass, esphome_path)
    except Exception:
        LOGGER.exception("Failed to read esphome configs from %s", esphome_path)
        return

    if not device_packages:
        LOGGER.debug("No device configs with packages found, nothing to do")
        return

    try:
        devices_to_update = await _find_devices_to_update(
            hass, device_packages, skip_nonexistent
        )
    except Exception:
        LOGGER.exception("Failed while checking package/compilation status")
        return

    if not devices_to_update:
        LOGGER.info("No devices need an update this cycle")
        return

    LOGGER.info("Devices to update: %s", sorted(devices_to_update))

    platforms = entity_platform.async_get_platforms(hass, ESPHOME_DOMAIN)
    if not platforms:
        LOGGER.warning("No esphome platforms are currently loaded; skipping this cycle")
        return

    update_entities = [
        entity
        for platform in platforms
        for entity_id, entity in platform.entities.items()
        if entity_id.startswith(UPDATE_ENTITY_PREFIX)
    ]

    if not update_entities:
        LOGGER.warning("No update entities found on the esphome platform; skipping this cycle")
        return

    target_slugs = {_slugify_device_name(name) for name in devices_to_update}

    entities_to_update = []
    for entity in update_entities:
        dev_name = _get_entity_device_name(hass, entity.entity_id)
        if dev_name and _slugify_device_name(dev_name) in target_slugs:
            entities_to_update.append((entity, dev_name))

    if not entities_to_update:
        LOGGER.warning("Devices need an update but no matching update entity was found for them")
        return

    installed_count = 0
    for entity, dev_name in entities_to_update:
        LOGGER.info("Installing update for %s (%s)", dev_name, entity.entity_id)
        if await _install_update(hass, entity):
            installed_count += 1

    LOGGER.info(
        "Update cycle completed for %d entity(ies), %d succeeded",
        len(entities_to_update),
        installed_count,
    )