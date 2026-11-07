"""Constants for SunMon."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry

DOMAIN = "sunmon"
MODEL = "SolarCount"

LOGGER = logging.getLogger(__package__)

CONF_SERIAL_PORT = "serial_port"
CONF_CHANNELS = "channels"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TRACE_SERIAL = "trace_serial"
CONF_READ_VOLTAGE = "read_voltage"
CONF_READ_YIELDS = "read_yields"

DEFAULT_TITLE = "SolarCount"
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_CHANNELS = 3  # the firmware queries channels 0 to 5
DEFAULT_SCAN_INTERVAL = 10  # seconds
MIN_SCAN_INTERVAL = 5  # one cycle of six channels takes about 3 seconds
MAX_SCAN_INTERVAL = 3600
DEFAULT_READ_VOLTAGE = True
DEFAULT_READ_YIELDS = True


def option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Return a setting: from the options if changed there, else from the setup."""
    return entry.options.get(key, entry.data.get(key, default))
