# ESPHome Packages Updater

Home Assistant integration that automatically updates ESPHome devices when their packages update.

## Requirements

### Home Assistant

* ESPHome integration (with device entries)
* ESPHome Device Builder

### Device configuration

* Enable OTA updates (add `ota:` block)
* Use packages

## Installation

Make sure you have [HACS](https://hacs.xyz) installed. Follow [these steps](https://hacs.xyz/docs/faq/custom_repositories) and use `ludero-git/esphome-packages-updater` as the Repository URL. Search for `ESPHome Packages Updater` and download. Restart, go to devices and add `ESPHome Packages Updater`. Follow

## License

[MIT](LICENSE)