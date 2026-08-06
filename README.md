# ESPHome Packages Updater

Home Assistant integration that detects new ESPHome package versions and updates devices automatically or with one click.

## Requirements

### Home Assistant

* [ESPHome Device Builder](https://my.home-assistant.io/redirect/supervisor_addon/?addon=5c53de3b_esphome&repository_url=https%3A%2F%2Fgithub.com%2Fesphome%2Fhome-assistant-addon)
* [ESPHome integration](https://my.home-assistant.io/redirect/config_flow_start?domain=esphome) set up for all devices. Make sure to enable the `firmware` sensors.

### Device configuration

* Enable OTA updates (`ota:` block in config)
* `name` key directly in config file (not via `!include` or a package import)
* [remote packages](https://esphome.io/components/packages/#remotegit-packages) (github only)

## Installation

Make sure you have [HACS](https://hacs.xyz) installed. Follow [these steps](https://hacs.xyz/docs/faq/custom_repositories) and use `ludero-git/esphome-packages-updater` as the Repository URL. Search for `ESPHome Packages Updater` and download. Restart, go to devices and add `ESPHome Packages Updater`. Follow

## License

[MIT](LICENSE)