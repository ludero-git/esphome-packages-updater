DOMAIN = "esphome_packages_updater"

CONF_INTERVAL = "interval"
CONF_EXPOSE_UPDATE_ENTITIES = "expose_update_entities"
CONF_AUTO_INSTALL = "auto_install"
CONF_SELECTIVE_UPDATE_CHECK = "selective_update_check"
CONF_REMOVE_STALE_DEVICES = "remove_stale_devices"
CONF_STALE_REMOVAL_DELAY = "stale_removal_delay"

DEFAULT_INTERVAL = 30
MIN_INTERVAL = 15

DEFAULT_REMOVE_STALE_DEVICES = True
DEFAULT_STALE_REMOVAL_DELAY = 30
MIN_STALE_REMOVAL_DELAY = 0

UPDATE_ENTITY_PREFIX = "update."

ESPHOME_COMPILATION_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%b %d %Y, %H:%M:%S",
)
ESPHOME_DEVICE_BUILDER_SLUG = "5c53de3b_esphome"
ESPHOME_ALLOWED_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"
ESPHOME_CONFIG_DIR = "/config/esphome"
ESPHOME_DOMAIN = "esphome"

STORAGE_VERSION = 1
INSTALL_TIMEOUT = 1800
INSTALL_POLL_INTERVAL = 5