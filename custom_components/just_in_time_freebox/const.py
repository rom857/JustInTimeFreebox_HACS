"""Constants for the Just-In-Time Freebox integration."""
from __future__ import annotations

DOMAIN = "just_in_time_freebox"
PLATFORMS = ["sensor", "binary_sensor"]

# Config / options keys
CONF_GRANTS_URL = "grants_url"
CONF_GRANTS_API_KEY = "grants_api_key"
CONF_POLL_INTERVAL = "poll_interval"
CONF_FREEBOX_HOST = "freebox_host"
CONF_FREEBOX_USE_HTTPS = "freebox_use_https"

# Stored after pairing / discovery
CONF_FREEBOX_APP_TOKEN = "freebox_app_token"
CONF_FREEBOX_API_BASE = "freebox_api_base"
CONF_FREEBOX_API_VERSION = "freebox_api_version"

# Defaults
DEFAULT_POLL_INTERVAL = 30
MIN_POLL_INTERVAL = 5
DEFAULT_USE_HTTPS = False

# Freebox app identity
APP_ID = "just_in_time_freebox"
APP_NAME = "JIT Freebox"
APP_VERSION = "0.1.0"
DEVICE_NAME = "Home Assistant"

# Backoff (Freebox failures)
BACKOFF_CAP_SECONDS = 15 * 60

# Last action enum values
ACTION_IDLE = "idle"
ACTION_ENABLED = "enabled_rule"
ACTION_DISABLED = "disabled_rule"
ACTION_RULE_NOT_FOUND = "rule_not_found"
ACTION_FREEBOX_ERROR = "freebox_error"
