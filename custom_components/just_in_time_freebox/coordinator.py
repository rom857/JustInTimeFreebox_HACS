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
    ACTION_PARTIAL_SUCCESS,
    ACTION_RULE_NOT_FOUND,
    APP_DESC,
    BACKOFF_CAP_SECONDS,
    CONF_FREEBOX_API_VERSION,
    CONF_FREEBOX_HOST,
    CONF_INSTANCE_KEY,
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
from .grants_api import GrantsApiError, fetch_grants

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

        # Track which (port, protocol) pairs we've enabled, per targetId.
        # Used to detect drift (stale rules from old/removed grants).
        self._managed_rules: dict[str, int] = {}  # {targetId: rule_id}

    # -- lifecycle ------------------------------------------------------

    async def async_open(self) -> None:
        """Open the Freebox session. Raises library exceptions on failure."""
        from .config_flow import make_instance_key, token_path  # local import to avoid cycle

        host = self._merged[CONF_FREEBOX_HOST]
        port = int(self._merged[CONF_FREEBOX_PORT])
        api_version = self._merged[CONF_FREEBOX_API_VERSION]
        token_key = self._merged.get(CONF_INSTANCE_KEY)
        if not token_key:
            token_key = make_instance_key(
                str(host),
                int(port),
                str(self._merged.get(CONF_GRANTS_URL, "")),
            )
        token_file = token_path(self._hass, host, token_key=token_key)
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

        # 1) Fetch grants from external API (array)
        try:
            grants = await fetch_grants(
                session,
                self._merged[CONF_GRANTS_URL],
                self._merged[CONF_GRANTS_API_KEY],
            )
        except GrantsApiError as err:
            raise UpdateFailed(f"Grants API error: {err}") from err

        # Initialize tracking
        active_grants: list[dict[str, Any]] = []
        failed_targets: list[str] = []
        actions_taken: list[str] = []

        # 2) Build desired_open set: {(port, protocol) for all granted and not_expired}
        desired_open: dict[tuple[int, str], dict[str, Any]] = {}
        for grant in grants:
            target_id = grant.get("targetId", "unknown")
            granted = bool(grant.get("granted", False))
            port = grant.get("port")
            protocol = grant.get("protocol")
            expires_utc = grant.get("expires_utc")

            # Check expiration
            not_expired = expires_utc is None or expires_utc > _now_utc()

            # Only include in desired_open if granted and not expired
            if granted and not_expired and port is not None and protocol is not None:
                key = (int(port), protocol)
                desired_open[key] = grant

            # Track for reporting
            active_grants.append({
                "targetId": target_id,
                "port": port,
                "protocol": protocol,
                "granted": granted,
                "expires_utc": expires_utc.isoformat() if expires_utc else None,
                "remaining_seconds": grant.get("remaining_seconds"),
            })

        # 3) Respect backoff window
        if self._next_attempt is not None and _now_utc() < self._next_attempt:
            data: dict[str, Any] = {
                "active_grants": active_grants,
                "last_action": ACTION_FREEBOX_ERROR,
                "timestamp": _now_utc().isoformat(),
            }
            return data

        if self._fbx is None:
            data = {
                "active_grants": active_grants,
                "last_action": ACTION_FREEBOX_ERROR,
                "timestamp": _now_utc().isoformat(),
            }
            return data

        # 4) Fetch current Freebox rules
        try:
            redirs = await self._list_redirs()
        except (HttpRequestError, NotOpenError, AuthorizationError) as err:
            _LOGGER.warning("Freebox error while fetching rules: %s", err)
            self._schedule_backoff()
            data = {
                "active_grants": active_grants,
                "last_action": ACTION_FREEBOX_ERROR,
                "timestamp": _now_utc().isoformat(),
            }
            return data

        # Build current_open set from Freebox: {(port, protocol): rule_id}
        current_open: dict[tuple[int, str], int] = {}
        for rule in redirs:
            if not rule.get("enabled", False):
                continue
            lan_port = rule.get("lan_port")
            protocol = rule.get("protocol", "").lower()
            if lan_port is not None and protocol:
                key = (int(lan_port), protocol)
                current_open[key] = int(rule["id"])

        # 5) Reconcile: enable rules for desired_open that aren't current, disable stale rules
        rules_to_enable = desired_open.keys() - current_open.keys()
        rules_to_disable = current_open.keys() - desired_open.keys()

        # Disable stale rules
        for port_proto in rules_to_disable:
            rule_id = current_open[port_proto]
            try:
                await self._set_enabled(rule_id, False)
                port, proto = port_proto
                actions_taken.append(f"disabled_rule:{port}/{proto}")
                _LOGGER.info(
                    "Disabled stale rule id=%s (port=%s, proto=%s)",
                    rule_id,
                    port,
                    proto,
                )
            except (HttpRequestError, NotOpenError, AuthorizationError) as err:
                _LOGGER.warning("Freebox error while disabling rule %s: %s", rule_id, err)
                self._schedule_backoff()
                data = {
                    "active_grants": active_grants,
                    "last_action": ACTION_FREEBOX_ERROR,
                    "timestamp": _now_utc().isoformat(),
                }
                return data

        # Enable rules for new grants
        for port_proto in rules_to_enable:
            port, protocol = port_proto
            grant = desired_open[port_proto]
            target_id = grant.get("targetId", "unknown")

            # Find matching rule on Freebox
            rule = find_rule(redirs, port, protocol)
            if rule is None:
                failed_targets.append(target_id)
                _LOGGER.debug(
                    "No Freebox rule found for targetId=%s (port=%s, proto=%s)",
                    target_id,
                    port,
                    protocol,
                )
                actions_taken.append(f"rule_not_found:{port}/{protocol}")
                continue

            rule_id = int(rule["id"])
            try:
                await self._set_enabled(rule_id, True)
                self._managed_rules[target_id] = rule_id
                actions_taken.append(f"enabled_rule:{port}/{protocol}")
                _LOGGER.info(
                    "Enabled rule id=%s for targetId=%s (port=%s, proto=%s)",
                    rule_id,
                    target_id,
                    port,
                    protocol,
                )
            except (HttpRequestError, NotOpenError, AuthorizationError) as err:
                _LOGGER.warning(
                    "Freebox error while enabling rule for targetId=%s: %s",
                    target_id,
                    err,
                )
                self._schedule_backoff()
                data = {
                    "active_grants": active_grants,
                    "last_action": ACTION_FREEBOX_ERROR,
                    "timestamp": _now_utc().isoformat(),
                }
                return data

        # 6) Determine summary action
        if not active_grants:
            summary_action = ACTION_IDLE
        elif failed_targets and actions_taken:
            summary_action = ACTION_PARTIAL_SUCCESS
        elif actions_taken:
            summary_action = ACTION_ENABLED
        else:
            summary_action = ACTION_IDLE

        self._reset_backoff()

        # 7) Build final data dict
        data = {
            "active_grants": active_grants,
            "failed_targets": failed_targets,
            "actions_taken": actions_taken,
            "last_action": summary_action,
            "timestamp": _now_utc().isoformat(),
        }

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
