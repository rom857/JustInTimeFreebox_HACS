"""Config and options flow for Just-In-Time Freebox."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify

from .const import (
    APP_DESC,
    CONF_FREEBOX_API_VERSION,
    CONF_FREEBOX_HOST,
    CONF_FREEBOX_PORT,
    CONF_GRANTS_API_KEY,
    CONF_GRANTS_URL,
    CONF_POLL_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_POLL_INTERVAL,
    TOKEN_DIR,
)
from .freebox_api import (
    AuthorizationError,
    FreeboxApiError,
    Freepybox,
    HttpRequestError,
    discover_api,
)
from .grants_api import GrantsApiError, fetch_grant

_LOGGER = logging.getLogger(__name__)


def token_path(hass: HomeAssistant, host: str) -> str:
    """Return (and create) the per-host token file path used by Freepybox."""
    directory = Path(hass.config.path(TOKEN_DIR))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{slugify(host)}.conf")


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_GRANTS_URL, default=d.get(CONF_GRANTS_URL, "")): str,
            vol.Required(CONF_GRANTS_API_KEY, default=d.get(CONF_GRANTS_API_KEY, "")): str,
            vol.Required(
                CONF_FREEBOX_HOST, default=d.get(CONF_FREEBOX_HOST, DEFAULT_HOST)
            ): str,
            vol.Required(
                CONF_POLL_INTERVAL,
                default=d.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): vol.All(int, vol.Range(min=MIN_POLL_INTERVAL)),
        }
    )


class JitFreeboxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 2

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)

            # Validate grants API first (cheap, plain HTTP/HTTPS).
            try:
                await fetch_grant(
                    session,
                    user_input[CONF_GRANTS_URL],
                    user_input[CONF_GRANTS_API_KEY],
                )
            except GrantsApiError as err:
                _LOGGER.warning("Grants API validation failed: %s", err)
                errors["base"] = "grants_api_error"

            # Discover Freebox HTTPS port + API version via plain HTTP.
            # The user-entered host is enforced verbatim; ``api_domain`` from
            # the discovery payload is intentionally ignored so the user can
            # target a LAN IP or custom DNS name without being redirected to
            # the Freebox-assigned ``*.fbxos.fr`` hostname.
            https_port: int | None = None
            api_version: str | None = None
            if not errors:
                try:
                    info = await discover_api(session, user_input[CONF_FREEBOX_HOST])
                    https_port = int(info["https_port"])
                    major = str(info.get("api_version", "8.0")).split(".", 1)[0]
                    api_version = f"v{major}"
                except (FreeboxApiError, KeyError, ValueError, TypeError) as err:
                    _LOGGER.warning("Freebox discovery failed: %s", err)
                    errors["base"] = "freebox_discovery_error"

            if not errors:
                self._user_input = {
                    **user_input,
                    CONF_FREEBOX_PORT: https_port,
                    CONF_FREEBOX_API_VERSION: api_version,
                }
                return await self.async_step_pairing()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to press the front-panel button, then open a session."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = self._user_input[CONF_FREEBOX_HOST]
            port = self._user_input[CONF_FREEBOX_PORT]
            api_version = self._user_input[CONF_FREEBOX_API_VERSION]
            token_file = token_path(self.hass, host)
            fbx = Freepybox(APP_DESC, token_file, api_version=api_version)
            try:
                await fbx.open(host, port)
            except AuthorizationError as err:
                _LOGGER.warning("Freebox authorization failed: %s", err)
                errors["base"] = "pairing_denied"
            except HttpRequestError as err:
                _LOGGER.warning("Freebox connection failed: %s", err)
                errors["base"] = "freebox_connection_error"
            except Exception:  # pragma: no cover - safety net
                _LOGGER.exception("Unknown error opening Freebox session")
                errors["base"] = "unknown"
            else:
                # Close the temporary session; the coordinator will open
                # its own at runtime using the persisted token file.
                try:
                    await fbx.close()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Ignoring error while closing pairing session", exc_info=True)
                title = f"JIT Freebox ({host})"
                return self.async_create_entry(title=title, data=self._user_input)

        return self.async_show_form(
            step_id="pairing",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return JitFreeboxOptionsFlow(config_entry)


class JitFreeboxOptionsFlow(OptionsFlow):
    """Editable options (grants URL/key and poll interval only)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_GRANTS_URL, default=current.get(CONF_GRANTS_URL, "")
                ): str,
                vol.Required(
                    CONF_GRANTS_API_KEY, default=current.get(CONF_GRANTS_API_KEY, "")
                ): str,
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(int, vol.Range(min=MIN_POLL_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
