"""Config and options flow for Just-In-Time Freebox."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    APP_ID,
    APP_NAME,
    APP_VERSION,
    CONF_FREEBOX_API_BASE,
    CONF_FREEBOX_API_VERSION,
    CONF_FREEBOX_APP_TOKEN,
    CONF_FREEBOX_HOST,
    CONF_FREEBOX_USE_HTTPS,
    CONF_GRANTS_API_KEY,
    CONF_GRANTS_URL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_USE_HTTPS,
    DEVICE_NAME,
    DOMAIN,
    MIN_POLL_INTERVAL,
)
from .freebox_api import (
    FreeboxApiError,
    discover,
    request_authorization,
    track_authorization,
)
from .grants_api import GrantsApiError, fetch_grant

_LOGGER = logging.getLogger(__name__)

PAIRING_POLL_INTERVAL = 2.0
PAIRING_TIMEOUT_SECONDS = 60.0


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_GRANTS_URL, default=d.get(CONF_GRANTS_URL, "")): str,
            vol.Required(CONF_GRANTS_API_KEY, default=d.get(CONF_GRANTS_API_KEY, "")): str,
            vol.Required(
                CONF_FREEBOX_HOST,
                default=d.get(CONF_FREEBOX_HOST, "mafreebox.freebox.fr"),
            ): str,
            vol.Required(
                CONF_FREEBOX_USE_HTTPS,
                default=d.get(CONF_FREEBOX_USE_HTTPS, DEFAULT_USE_HTTPS),
            ): bool,
            vol.Required(
                CONF_POLL_INTERVAL,
                default=d.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): vol.All(int, vol.Range(min=MIN_POLL_INTERVAL)),
        }
    )


class JitFreeboxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}
        self._api_base: str | None = None
        self._api_version: int | None = None
        self._app_token: str | None = None
        self._track_id: int | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            # Validate grants API
            try:
                await fetch_grant(
                    session, user_input[CONF_GRANTS_URL], user_input[CONF_GRANTS_API_KEY]
                )
            except GrantsApiError as err:
                _LOGGER.warning("Grants API validation failed: %s", err)
                errors["base"] = "grants_api_error"

            # Discover Freebox
            if not errors:
                try:
                    api_base, api_version = await discover(
                        session,
                        user_input[CONF_FREEBOX_HOST],
                        user_input[CONF_FREEBOX_USE_HTTPS],
                    )
                except FreeboxApiError as err:
                    _LOGGER.warning("Freebox discovery failed: %s", err)
                    errors["base"] = "freebox_discovery_error"
                else:
                    self._api_base = api_base
                    self._api_version = api_version

            if not errors:
                # Start pairing
                try:
                    app_token, track_id = await request_authorization(
                        session,
                        self._api_base,
                        APP_ID,
                        APP_NAME,
                        APP_VERSION,
                        DEVICE_NAME,
                    )
                except FreeboxApiError as err:
                    _LOGGER.warning("Freebox request_authorization failed: %s", err)
                    errors["base"] = "freebox_auth_error"
                else:
                    self._user_input = user_input
                    self._app_token = app_token
                    self._track_id = track_id
                    return await self.async_step_pairing()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wait for the user to press the button on the Freebox front panel."""
        session = async_get_clientsession(self.hass)
        assert self._api_base is not None and self._track_id is not None

        deadline = asyncio.get_event_loop().time() + PAIRING_TIMEOUT_SECONDS
        last_status = "pending"
        while asyncio.get_event_loop().time() < deadline:
            try:
                last_status = await track_authorization(
                    session, self._api_base, self._track_id
                )
            except FreeboxApiError as err:
                _LOGGER.warning("Freebox track_authorization failed: %s", err)
                return self.async_show_form(
                    step_id="user",
                    data_schema=_user_schema(self._user_input),
                    errors={"base": "freebox_auth_error"},
                )
            if last_status == "granted":
                break
            if last_status in ("denied", "timeout", "unknown"):
                break
            await asyncio.sleep(PAIRING_POLL_INTERVAL)

        if last_status != "granted":
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(self._user_input),
                errors={"base": f"pairing_{last_status}"},
            )

        data = {
            CONF_GRANTS_URL: self._user_input[CONF_GRANTS_URL],
            CONF_GRANTS_API_KEY: self._user_input[CONF_GRANTS_API_KEY],
            CONF_FREEBOX_HOST: self._user_input[CONF_FREEBOX_HOST],
            CONF_FREEBOX_USE_HTTPS: self._user_input[CONF_FREEBOX_USE_HTTPS],
            CONF_POLL_INTERVAL: self._user_input[CONF_POLL_INTERVAL],
            CONF_FREEBOX_APP_TOKEN: self._app_token,
            CONF_FREEBOX_API_BASE: self._api_base,
            CONF_FREEBOX_API_VERSION: self._api_version,
        }
        title = f"JIT Freebox ({self._user_input[CONF_FREEBOX_HOST]})"
        return self.async_create_entry(title=title, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return JitFreeboxOptionsFlow(config_entry)


class JitFreeboxOptionsFlow(OptionsFlow):
    """Editable options. Does NOT re-pair; if host/https change, advise user."""

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
                    CONF_FREEBOX_HOST,
                    default=current.get(CONF_FREEBOX_HOST, "mafreebox.freebox.fr"),
                ): str,
                vol.Required(
                    CONF_FREEBOX_USE_HTTPS,
                    default=current.get(CONF_FREEBOX_USE_HTTPS, DEFAULT_USE_HTTPS),
                ): bool,
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(int, vol.Range(min=MIN_POLL_INTERVAL)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
