# <img width="50" height="50" align="absmiddle" alt="logo" src="https://raw.githubusercontent.com/ludero-git/esphome-packages-updater/master/custom_components/esphome_packages_updater/brand/icon.png" /> ESPHome Packages Updater

Home Assistant custom integration that detects new ESPHome package versions and updates devices automatically or with one click.

[![Latest Release](https://img.shields.io/github/v/release/ludero-git/esphome-packages-updater?display_name=tag\&sort=semver)](https://github.com/ludero-git/esphome-packages-updater/releases/latest)

## Requirements

### Home Assistant

* [ESPHome Device Builder](https://my.home-assistant.io/redirect/supervisor_addon/?addon=5c53de3b_esphome&repository_url=https%3A%2F%2Fgithub.com%2Fesphome%2Fhome-assistant-addon)
* [ESPHome integration](https://my.home-assistant.io/redirect/config_flow_start?domain=esphome) set up for all devices. Make sure to enable the `firmware` sensors.

### Device configuration

* Enable OTA updates (`ota:` block in config)
* `name` key directly in config file (not via `!include` or a package import)
* [remote packages](https://esphome.io/components/packages/#remotegit-packages) (github only)

## Installation

### Step 1: Install

#### Via HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ludero-git&repository=esphome-packages-updater&category=integration)

1. Click "Download" to install.
2. Restart Home Assistant.

#### Manually

1. Copy `custom_components/esphome_packages_updater` to `<config>/custom_components/esphome_packages_updater`.
2. Restart Home Assistant.

### Step 2: Configure

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=esphome_packages_updater)

Or manually: Go to **Settings > Devices & services > Add integration > ESPHome Packages Updater**.

1. Configure options
2. Done!

## License

[MIT](/LICENSE)