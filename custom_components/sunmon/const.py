"""Constants for SunMon."""

from __future__ import annotations

import logging

DOMAIN = "sunmon"
MODEL = "SolarCount"

LOGGER = logging.getLogger(__package__)

CONF_SERIAL_PORT = "serial_port"
CONF_CHANNELS = "channels"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TRACE_SERIAL = "trace_serial"

DEFAULT_TITLE = "SolarCount"
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_CHANNELS = 3  # the firmware queries channels 0 to 5
DEFAULT_SCAN_INTERVAL = 10  # seconds
MIN_SCAN_INTERVAL = 5  # one cycle of six channels takes about 3 seconds
MAX_SCAN_INTERVAL = 3600
