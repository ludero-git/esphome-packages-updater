from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass, field
import yaml
import logging
import re

from homeassistant.core import HomeAssistant

from .const import ESPHOME_ALLOWED_NAME_CHARS

_LOGGER = logging.getLogger(__name__)


@dataclass
class ESPHomePackage:
    """A remote git ESPHome package."""

    url: str
    files: list[str]
    reference: str | None = None
    username: str | None = None
    password: str | None = None


@dataclass
class ESPHomeDeviceConfig:
    path: Path
    name: str
    friendly_name: str
    packages: list[ESPHomePackage] = field(default_factory=list)


def _strip_accents(value: str) -> str:
    """Remove accents from a string."""
    import unicodedata

    return "".join(
        c
        for c in unicodedata.normalize("NFD", str(value))
        if unicodedata.category(c) != "Mn"
    )


def _slugify_device_name(name: str) -> str:
    """Convert a device name to the slug format Home Assistant uses."""

    # Same as the method ESPHome uses.
    value = (
        _strip_accents(name)
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("__", "_")
        .strip("_")
    )

    return "".join(c for c in value if c in ESPHOME_ALLOWED_NAME_CHARS).replace("_", "-")


async def get_config_files(hass: HomeAssistant, esphome_path: Path) -> list[Path]:
    """Get list of device configuration files."""
    def get_configs() -> list[Path]:
        if not esphome_path.is_dir():
            return []

        return [
            path
            for path in esphome_path.iterdir()
            if path.is_file()
            and path.suffix in [".yml", ".yaml"]
            and path.name != "secrets.yaml"
        ]

    configs = await hass.async_add_executor_job(get_configs)
    return configs


async def load_secrets(hass: HomeAssistant, esphome_path: Path) -> dict[str, str]:
    """Read and parse the ESPHome secrets file, if one exists."""
    secrets_path = esphome_path / "secrets.yaml"

    def read_secrets() -> dict[str, str]:
        if not secrets_path.is_file():
            _LOGGER.debug("No secrets.yaml found at %s", secrets_path)
            return {}
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            _LOGGER.exception("Failed to parse secrets.yaml at %s", secrets_path)
            return {}
        return data if isinstance(data, dict) else {}

    secrets = await hass.async_add_executor_job(read_secrets)
    _LOGGER.debug("Loaded %d secret(s) from secrets.yaml", len(secrets))
    return secrets


class _ESPHomeYamlLoader(yaml.SafeLoader):
    """YAML loader that resolves ESPHome tags."""

    def __init__(
        self,
        stream,
        device_name: str,
        secrets: dict[str, str],
    ):
        super().__init__(stream)
        self.device_name = device_name
        self.secrets = secrets


def _construct_secret(loader: _ESPHomeYamlLoader, node: yaml.Node):
    """Resolve ESPHome !secret tags."""
    name = loader.construct_scalar(node)

    # Device specific secret
    device_key = f"{loader.device_name}__{name}"

    # Fallback to shared secret
    return loader.secrets.get(
        device_key,
        loader.secrets.get(name, name),
    )


def _construct_unknown_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    """Allow ESPHome custom tags."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_ESPHomeYamlLoader.add_constructor("!secret", _construct_secret)
_ESPHomeYamlLoader.add_multi_constructor("!", _construct_unknown_tag)


def _parse_config(
    config_path: Path,
    secrets: dict[str, str],
) -> dict | None:
    """Parse an ESPHome configuration file."""
    try:
        with open(config_path, "r", encoding="utf-8") as conf:
            return _ESPHomeYamlLoader(
                conf,
                config_path.stem,
                secrets,
            ).get_single_data()
    except Exception:
        _LOGGER.exception("Failed to parse %s", config_path)
        return None


def _extract_packages(package_list: dict):
    """Extracts repositories from packages list and normalizes to github URL."""
    packages = []

    if not isinstance(package_list, dict):
        _LOGGER.warning("Invalid packages block")
        return []

    for name, data in package_list.items():
        if isinstance(data, str):
            # Parse format "github://username/repository/[folder/]file-path.yml[@branch-or-tag]".

            if not data.startswith("github://"):
                _LOGGER.warning("Only github shorthand is supported; skipping package %s", data)
                continue

            pattern = r"^github://([^/]+)/([^/]+)/([^@]+)(?:@(.+))?$"
            matched = re.match(pattern, data)

            if matched:
                user, repo, path, branch = matched.groups()
                package = ESPHomePackage(f"https://github.com/{user}/{repo}", [path], branch)
                packages.append(package)
            else:
                _LOGGER.warning("Failed to extract repo details; skipping package %s", name)
                continue
        else:
            # Parse format "https://github.com/username/repository".

            url = data.get("url", None)
            if not url:
                _LOGGER.warning("Failed to extract url; skipping package %s", name)
                continue

            paths = []
            files = data.get("files", [])
            for file in files:
                if isinstance(file, str):
                    paths.append(file)
                    continue

                path = file.get("path", None)
                if path:
                    paths.append(path)
                else:
                    _LOGGER.warning("Failed to extract path; skipping file %s", file)
                    continue

            package = ESPHomePackage(
                url.replace(".git", ""),
                paths,
                data.get("ref", None),
                data.get("username", None),
                data.get("password", None)
            )
            packages.append(package)

    return packages


async def parse_config_file(hass: HomeAssistant, config_path: Path, secrets):
    """Parse configuration, extract device name and packages."""
    parsed = await hass.async_add_executor_job(
        _parse_config,
        config_path,
        secrets,
    )

    if not parsed:
        _LOGGER.exception("Failed to parse %s; skipping file", config_path)
        return None

    esphome_block = parsed.get("esphome")
    if not esphome_block:
        _LOGGER.warning("No esphome block found in %s; skipping file", config_path)
        return None

    raw_name = esphome_block.get("name")

    if not raw_name:
        _LOGGER.warning("No device name found in %s; skipping file", config_path)
        return None

    friendly_name = esphome_block.get("friendly_name")
    if not friendly_name:
        friendly_name = _slugify_device_name(raw_name)

    packages_block = parsed.get("packages")
    if not packages_block:
        _LOGGER.warning("No packages block found in %s; skipping file", config_path)
        return None

    packages = _extract_packages(packages_block)
    return ESPHomeDeviceConfig(
        config_path,
        raw_name,
        friendly_name,
        packages
    )