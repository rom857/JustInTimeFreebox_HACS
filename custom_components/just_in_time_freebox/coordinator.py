"""DataUpdateCoordinator for Just-In-Time Freebox.

Every poll cycle:
1. Fetch the current grant from the external grants API.
2. Reconcile the matching Freebox port-forwarding rule's ``enabled`` flag
   so it always reflects ``granted AND not expired`` (even after restarts
   or out-of-band edits).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
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
    APP_DESC,
    BACKOFF_CAP_SECONDS,
    CONF_FREEBOX_API_VERSION,
    CONF_FREEBOX_HOST,
    CONF_FREEBOX_PORT,
    CONF_GRANTS_API_KEY,
    CONF_GRANTS_URL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .freebox_api import (
    AuthorizationError,
    Freepybox,
    HttpRequestError,
    NotOpenError,
    find_rule,
)
from .grants_api import GrantsApiError, fetch_grant

_LOGGER = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # Python parses trailing 'Z' only since 3.11; normalize defensively.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class JitFreeboxCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls grants API and reconciles Freebox rule on every tick."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        merged = {**entry.data, **entry.options}
        poll = int(merged.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id}",
            update_interval=timedelta(seconds=poll),
        )
        self._hass = hass
        self._entry = entry
        self._merged = merged

        # Build at async_open() time (off-loop file IO is done inside the lib).
        self._fbx: Freepybox | None = None

        # Backoff state for Freebox errors.
        self._consecutive_failures = 0
        self._next_attempt: datetime | None = None

        # Track the rule we last operated on so we can disable it on drift
        # (e.g. when the grant moves to a different port).
        self._active_rule_id: int | None = None
        self._active_port: int | None = None
        self._active_proto: str | None = None

    # -- lifecycle ------------------------------------------------------

    async def async_open(self) -> None:
        """Open the Freebox session. Raises library exceptions on failure."""
        from .config_flow import token_path  # local import to avoid cycle

        host = self._merged[CONF_FREEBOX_HOST]
        port = int(self._merged[CONF_FREEBOX_PORT])
        api_version = self._merged[CONF_FREEBOX_API_VERSION]
        token_file = token_path(self._hass, host)
        self._fbx = Freepybox(APP_DESC, token_file, api_version=api_version)
        _LOGGER.debug(
            "Opening Freebox session host=%s port=%s api=%s", host, port, api_version
        )
        await self._fbx.open(host, port)

    async def async_close(self) -> None:
        """Best-effort close."""
        if self._fbx is None:
            return
        try:
            await self._fbx.close()
        except NotOpenError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error while closing Freebox session", exc_info=True)
        finally:
            self._fbx = None

    # -- backoff helpers ------------------------------------------------

    def _schedule_backoff(self) -> None:
        self._consecutive_failures += 1
        delay = min(
            BACKOFF_CAP_SECONDS,
            (self.update_interval.total_seconds() if self.update_interval else 30)
            * (2 ** (self._consecutive_failures - 1)),
        )
        self._next_attempt = _now_utc() + timedelta(seconds=delay)
        _LOGGER.debug(
            "Freebox backoff: failure #%d, next attempt at %s",
            self._consecutive_failures,
            self._next_attempt.isoformat(),
        )

    def _reset_backoff(self) -> None:
        if self._consecutive_failures:
            _LOGGER.debug("Freebox backoff reset")
        self._consecutive_failures = 0
        self._next_attempt = None

    # -- main update ----------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        session = async_get_clientsession(self._hass)
        prev = self.data or {}
        last_action = prev.get("last_action", ACTION_IDLE)

        # 1) Grants API
        try:
            grant = await fetch_grant(
                session,
                self._merged[CONF_GRANTS_URL],
                self._merged[CONF_GRANTS_API_KEY],
            )
        except GrantsApiError as err:
            raise UpdateFailed(f"Grants API error: {err}") from err

        granted = bool(grant.get("granted"))
        port = grant.get("port")
        protocol = (grant.get("protocol") or "").lower() or None
        expires_utc = grant.get("expiresUtc")
        started_utc = grant.get("startedUtc")
        remaining_seconds = grant.get("remainingSeconds")

        expires_dt = _parse_iso(expires_utc)
        not_expired = expires_dt is None or expires_dt > _now_utc()
        desired_enabled = granted and not_expired

        data: dict[str, Any] = {
            "granted": granted,
            "port": port,
            "protocol": protocol,
            "started_utc": started_utc,
            "expires_utc": expires_utc,
            "remaining_seconds": remaining_seconds,
            "desired_enabled": desired_enabled,
            "last_action": last_action,
        }

        # 2) Respect backoff window.
        if self._next_attempt is not None and _now_utc() < self._next_attempt:
            data["last_action"] = ACTION_FREEBOX_ERROR
            return data

        if self._fbx is None:
            data["last_action"] = ACTION_FREEBOX_ERROR
            return data

        # 3) Handle rule drift (port/proto changed) -> disable previous rule.
        if (
            self._active_rule_id is not None
            and (self._active_port != port or self._active_proto != protocol)
        ):
            try:
                await self._set_enabled(self._active_rule_id, False)
                _LOGGER.debug(
                    "Disabled previous active rule id=%s (port/proto drift)",
                    self._active_rule_id,
                )
            except (HttpRequestError, NotOpenError, AuthorizationError) as err:
                _LOGGER.warning("Freebox error while disabling stale rule: %s", err)
                self._schedule_backoff()
                data["last_action"] = ACTION_FREEBOX_ERROR
                return data
            self._active_rule_id = None
            self._active_port = None
            self._active_proto = None

        if port is None or protocol is None:
            self._reset_backoff()
            return data

        # 4) Find and reconcile the matching rule.
        try:
            redirs = await self._list_redirs()
            rule = find_rule(redirs, int(port), protocol)
            if rule is None:
                data["last_action"] = ACTION_RULE_NOT_FOUND
                _LOGGER.debug(
                    "No Freebox redir matches lan_port=%s proto=%s", port, protocol
                )
                self._reset_backoff()
                return data

            rule_id = int(rule["id"])
            current_enabled = bool(rule.get("enabled", False))

            if current_enabled != desired_enabled:
                await self._set_enabled(rule_id, desired_enabled)
                data["last_action"] = (
                    ACTION_ENABLED if desired_enabled else ACTION_DISABLED
                )
                _LOGGER.info(
                    "Reconciled Freebox rule id=%s enabled %s -> %s",
                    rule_id,
                    current_enabled,
                    desired_enabled,
                )
            else:
                data["last_action"] = ACTION_IDLE

            self._active_rule_id = rule_id
            self._active_port = int(port)
            self._active_proto = protocol
            self._reset_backoff()
        except (HttpRequestError, NotOpenError, AuthorizationError) as err:
            _LOGGER.warning("Freebox error during reconcile: %s", err)
            self._schedule_backoff()
            data["last_action"] = ACTION_FREEBOX_ERROR

        return data

    # -- thin wrappers around freebox-api -------------------------------

    async def _list_redirs(self) -> list[dict[str, Any]]:
        assert self._fbx is not None
        result = await self._fbx.fw.get_all_port_forwarding_configuration()
        return result if isinstance(result, list) else []

    async def _set_enabled(self, rule_id: int, enabled: bool) -> None:
        assert self._fbx is not None
        await self._fbx.fw.edit_port_forwarding_configuration(
            rule_id, {"enabled": enabled}
        )
