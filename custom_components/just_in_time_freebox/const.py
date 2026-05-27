"""Constants for the Just-In-Time Freebox integration."""
from __future__ import annotations

DOMAIN = "just_in_time_freebox"
PLATFORMS = ["sensor", "binary_sensor"]

# Config / options keys
CONF_GRANTS_URL = "grants_url"
CONF_GRANTS_API_KEY = "grants_api_key"
CONF_PROFILE_NAME = "profile_name"
CONF_REUSE_EXISTING = "reuse_existing"
CONF_POLL_INTERVAL = "poll_interval"
CONF_FREEBOX_HOST = "freebox_host"          # api_domain returned by /api_version
CONF_FREEBOX_PORT = "freebox_port"          # https_port returned by /api_version
CONF_FREEBOX_API_VERSION = "freebox_api_version"  # e.g. "v15"
CONF_INSTANCE_KEY = "instance_key"

# Defaults
DEFAULT_POLL_INTERVAL = 30
MIN_POLL_INTERVAL = 5
DEFAULT_HOST = "mafreebox.freebox.fr"

# Freebox app identity (used by the freebox-api library)
APP_ID = "just_in_time_freebox"
APP_NAME = "JIT Freebox"
APP_VERSION = "0.3.0"
DEVICE_NAME = "Home Assistant"

APP_DESC = {
    "app_id": APP_ID,
    "app_name": APP_NAME,
    "app_version": APP_VERSION,
    "device_name": DEVICE_NAME,
}

# Subdirectory under hass.config.path() for per-host app_token files.
TOKEN_DIR = "just_in_time_freebox"

# Backoff (Freebox failures)
BACKOFF_CAP_SECONDS = 15 * 60

# Last action enum values
ACTION_IDLE = "idle"
ACTION_ENABLED = "enabled_rule"
ACTION_DISABLED = "disabled_rule"
ACTION_RULE_NOT_FOUND = "rule_not_found"
ACTION_FREEBOX_ERROR = "freebox_error"
