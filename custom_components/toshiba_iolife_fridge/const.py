"""定数."""

from datetime import timedelta

DOMAIN = "toshiba_iolife_fridge"
MANUFACTURER = "Toshiba Lifestyle"

CLOUD_NAME = "Toshiba Iolife"
DEVICE_TYPE_REFRIGERATOR = 0xCA

CONF_DEVICE_ID = "device_id"
CONF_TOKEN = "token"
CONF_KEY = "key"
CONF_ACCOUNT = "account"
CONF_MODEL = "model"

SCAN_INTERVAL = timedelta(seconds=60)
