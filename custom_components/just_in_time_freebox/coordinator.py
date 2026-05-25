"""DataUpdateCoordinator for the Just-In-Time Freebox integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ACTION_DISABLED,
    ACTION_ENABLED,
    ACTION_FREEBOX_ERROR,
    ACTION_IDLE,
    ACTION_RULE_NOT_FOUND,
    APP_ID,
    BACKOFF_CAP_SECONDS,
    CONF_FREEBOX_API_BASE,
    CONF_FREEBOX_APP_TOKEN,
    CONF_GRANTS_API_KEY,
    CONF_GRANTS_URL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .freebox_api import FreeboxApiError, FreeboxClient
from .grants_api import GrantsApiError, fetch_grant

_LOGGER = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JitFreeboxCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls grants API and reconciles a single Freebox port-forward rule."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        poll = int(entry.options.get(CONF_POLL_INTERVAL, entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=poll),
        )
        self._poll_interval = poll
        session = async_get_clientsession(hass)
        api_base = entry.data[CONF_FREEBOX_API_BASE]
        app_token = entry.data[CONF_FREEBOX_APP_TOKEN]
        self._freebox = FreeboxClient(session, api_base, APP_ID, app_token)
        self._http = session

        # State
        self._active_rule_id: int | None = None
        self._active_expires_utc: datetime | None = None
        self._active_port: int | None = None
        self._active_protocol: str | None = None
        self._last_action: str = ACTION_IDLE

        # Backoff state for Freebox failures
        self._freebox_failures: int = 0
        self._freebox_skip_until: datetime | None = None

    def update_poll_interval(self, seconds: int) -> None:
        self._poll_interval = max(1, int(seconds))
        self.update_interval = timedelta(seconds=self._poll_interval)

    def _backoff_seconds(self) -> int:
        # base * 2^(n-1), capped
        base = max(self._poll_interval, 1)
        delay = base * (2 ** max(self._freebox_failures - 1, 0))
        return min(delay, BACKOFF_CAP_SECONDS)

    def _freebox_in_backoff(self) -> bool:
        if self._freebox_skip_until is None:
            return False
        return _utcnow() < self._freebox_skip_until

    def _record_freebox_failure(self, err: Exception) -> None:
        self._freebox_failures += 1
        delay = self._backoff_seconds()
        self._freebox_skip_until = _utcnow() + timedelta(seconds=delay)
        self._last_action = ACTION_FREEBOX_ERROR
        _LOGGER.warning(
            "Freebox call failed (%s); backing off for %s seconds (failure #%d)",
            err,
            delay,
            self._freebox_failures,
        )

    def _record_freebox_success(self) -> None:
        if self._freebox_failures:
            _LOGGER.info("Freebox call succeeded; clearing backoff")
        self._freebox_failures = 0
        self._freebox_skip_until = None

    def _clear_active(self) -> None:
        self._active_rule_id = None
        self._active_port = None
        self._active_protocol = None
        self._active_expires_utc = None

    async def _reconcile_rule(
        self,
        port: int,
        protocol: str,
        desired_enabled: bool,
        expires_utc: datetime | None,
    ) -> None:
        """Look up the rule matching (port, protocol) and force its
        ``enabled`` flag to ``desired_enabled``. Runs every poll, so manual
        changes on Freebox OS are corrected on the next cycle.
        """
        try:
            redirs = await self._freebox.list_redirs()
            rule = FreeboxClient.find_rule(redirs, port, protocol)
            if rule is None:
                _LOGGER.warning(
                    "No Freebox port-forward rule matches port=%s proto=%s",
                    port,
                    protocol,
                )
                self._last_action = ACTION_RULE_NOT_FOUND
                self._record_freebox_success()
                if not desired_enabled:
                    self._clear_active()
                return

            rule_id = int(rule["id"])
            currently_enabled = bool(rule.get("enabled", False))

            if currently_enabled != desired_enabled:
                await self._freebox.set_redir_enabled(rule_id, desired_enabled)
                if desired_enabled:
                    _LOGGER.info(
                        "Enabled Freebox redir rule id=%s (port=%s proto=%s) until %s",
                        rule_id,
                        port,
                        protocol,
                        expires_utc.isoformat() if expires_utc else "?",
                    )
                    self._last_action = ACTION_ENABLED
                else:
                    _LOGGER.info(
                        "Disabled Freebox redir rule id=%s (port=%s proto=%s)",
                        rule_id,
                        port,
                        protocol,
                    )
                    self._last_action = ACTION_DISABLED
            else:
                _LOGGER.debug(
                    "Freebox redir rule id=%s already %s; no change",
                    rule_id,
                    "enabled" if desired_enabled else "disabled",
                )

            if desired_enabled:
                self._active_rule_id = rule_id
                self._active_port = port
                self._active_protocol = protocol
                self._active_expires_utc = expires_utc
            else:
                self._clear_active()
            self._record_freebox_success()
        except FreeboxApiError as err:
            self._record_freebox_failure(err)

    async def _disable_rule_by_id(self, rule_id: int, reason: str) -> None:
        """Disable a specific rule id (used when the grant target drifts to
        a different port/protocol and we need to clean up the old rule)."""
        try:
            await self._freebox.set_redir_enabled(rule_id, False)
            _LOGGER.info(
                "Disabled Freebox redir rule id=%s (%s)", rule_id, reason
            )
            self._last_action = ACTION_DISABLED
            self._record_freebox_success()
        except FreeboxApiError as err:
            self._record_freebox_failure(err)

    async def _async_update_data(self) -> dict[str, Any]:
        url = self.entry.options.get(
            CONF_GRANTS_URL, self.entry.data.get(CONF_GRANTS_URL)
        )
        api_key = self.entry.options.get(
            CONF_GRANTS_API_KEY, self.entry.data.get(CONF_GRANTS_API_KEY)
        )
        try:
            grant = await fetch_grant(self._http, url, api_key)
        except GrantsApiError as err:
            raise UpdateFailed(str(err)) from err

        granted = grant["granted"]
        port = grant["port"]
        protocol = grant["protocol"]
        expires_utc = grant["expires_utc"]

        in_backoff = self._freebox_in_backoff()

        if not in_backoff:
            now = _utcnow()
            expired = expires_utc is not None and now >= expires_utc
            desired_enabled = bool(granted) and not expired

            # Target drift: previously activated rule differs from current
            # grant target. Disable the stale rule before touching the new one.
            if (
                self._active_rule_id is not None
                and self._active_port is not None
                and self._active_protocol is not None
                and port is not None
                and protocol is not None
                and (port != self._active_port or protocol != self._active_protocol)
            ):
                await self._disable_rule_by_id(
                    self._active_rule_id, "grant target changed"
                )
                self._clear_active()

            if port is not None and protocol is not None:
                # Reconcile every poll: granted=True → rule enabled,
                # granted=False (or expired) → rule disabled.
                await self._reconcile_rule(port, protocol, desired_enabled, expires_utc)
            elif self._active_rule_id is not None and not desired_enabled:
                # No port/proto in payload, but we previously activated
                # something — make sure it gets disabled.
                await self._disable_rule_by_id(
                    self._active_rule_id,
                    "grant revoked" if not granted else "expired",
                )
                self._clear_active()
        else:
            _LOGGER.debug(
                "Skipping Freebox actions; in backoff until %s",
                self._freebox_skip_until,
            )

        return {
            "granted": granted,
            "port": port,
            "protocol": protocol,
            "started_utc": grant["started_utc"],
            "expires_utc": expires_utc,
            "remaining_seconds": grant["remaining_seconds"],
            "active_rule_id": self._active_rule_id,
            "active_expires_utc": self._active_expires_utc,
            "last_action": self._last_action,
            "freebox_failures": self._freebox_failures,
            "freebox_backoff_until": self._freebox_skip_until,
        }
