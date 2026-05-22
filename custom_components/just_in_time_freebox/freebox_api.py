"""Async client for the Freebox local API.

Implements just enough of the API to:
- discover the api base URL and major version,
- request authorization (pairing) and track its status,
- open a session (HMAC-SHA1 challenge) and refresh it on 401,
- list and update port-forwarding redir rules.

Standard SSL verification is used; self-signed Freebox certs (e.g.
``mafreebox.freebox.fr``) will not work over HTTPS.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

AUTH_HEADER = "X-Fbx-App-Auth"


class FreeboxApiError(Exception):
    """Generic Freebox API error."""


class FreeboxAuthError(FreeboxApiError):
    """Authentication / pairing error."""


class FreeboxRuleNotFoundError(FreeboxApiError):
    """No port-forwarding rule matches the requested port + protocol."""


def _scheme(use_https: bool) -> str:
    return "https" if use_https else "http"


def _root(host: str, use_https: bool) -> str:
    return f"{_scheme(use_https)}://{host}"


async def discover(
    session: aiohttp.ClientSession, host: str, use_https: bool
) -> tuple[str, int]:
    """Return (api_base_url, api_major_version).

    api_base_url already includes scheme + host + ``api_base_url`` path
    fragment returned by the Freebox (typically ``/api/``).
    """
    url = f"{_root(host, use_https)}/api_version"
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"discover failed: {err}") from err

    api_base_path = data.get("api_base_url", "/api/")
    api_version_str = str(data.get("api_version", "8.0"))
    try:
        major = int(api_version_str.split(".", 1)[0])
    except ValueError as err:
        raise FreeboxApiError(f"invalid api_version: {api_version_str}") from err

    api_base_url = f"{_root(host, use_https)}{api_base_path}v{major}/"
    return api_base_url, major


def _check_success(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("success", False):
        msg = payload.get("msg") or payload.get("error_code") or "unknown error"
        code = payload.get("error_code", "")
        if code in ("auth_required", "invalid_token", "invalid_session_token"):
            raise FreeboxAuthError(f"{code}: {msg}")
        raise FreeboxApiError(f"{code}: {msg}")
    return payload.get("result", {})


async def request_authorization(
    session: aiohttp.ClientSession,
    api_base_url: str,
    app_id: str,
    app_name: str,
    app_version: str,
    device_name: str,
) -> tuple[str, int]:
    """Start pairing. Returns (app_token, track_id)."""
    url = f"{api_base_url}login/authorize/"
    body = {
        "app_id": app_id,
        "app_name": app_name,
        "app_version": app_version,
        "device_name": device_name,
    }
    try:
        async with session.post(url, json=body) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"request_authorization failed: {err}") from err
    result = _check_success(data)
    return result["app_token"], int(result["track_id"])


async def track_authorization(
    session: aiohttp.ClientSession, api_base_url: str, track_id: int
) -> str:
    """Return one of: ``pending``, ``granted``, ``denied``, ``timeout``, ``unknown``."""
    url = f"{api_base_url}login/authorize/{track_id}"
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"track_authorization failed: {err}") from err
    result = _check_success(data)
    return str(result.get("status", "unknown"))


async def _get_challenge(
    session: aiohttp.ClientSession, api_base_url: str
) -> str:
    url = f"{api_base_url}login/"
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return _check_success(data)["challenge"]


async def open_session(
    session: aiohttp.ClientSession,
    api_base_url: str,
    app_id: str,
    app_token: str,
) -> str:
    """Open a session and return the session token."""
    try:
        challenge = await _get_challenge(session, api_base_url)
        password = hmac.new(
            app_token.encode("ascii"),
            challenge.encode("ascii"),
            hashlib.sha1,
        ).hexdigest()
        url = f"{api_base_url}login/session/"
        async with session.post(url, json={"app_id": app_id, "password": password}) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"open_session failed: {err}") from err
    result = _check_success(data)
    return result["session_token"]


class FreeboxClient:
    """Stateful Freebox client that holds a session token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_base_url: str,
        app_id: str,
        app_token: str,
    ) -> None:
        self._session = session
        self._api_base_url = api_base_url
        self._app_id = app_id
        self._app_token = app_token
        self._session_token: str | None = None

    async def _ensure_session(self) -> None:
        if self._session_token is None:
            self._session_token = await open_session(
                self._session, self._api_base_url, self._app_id, self._app_token
            )

    def _auth_headers(self) -> dict[str, str]:
        assert self._session_token is not None
        return {AUTH_HEADER: self._session_token}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        _retry: bool = True,
    ) -> Any:
        await self._ensure_session()
        url = f"{self._api_base_url}{path}"
        try:
            async with self._session.request(
                method, url, json=json, headers=self._auth_headers()
            ) as resp:
                if resp.status == 401 and _retry:
                    self._session_token = None
                    return await self._request(method, path, json=json, _retry=False)
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise FreeboxApiError(f"{method} {path} failed: {err}") from err
        try:
            return _check_success(data)
        except FreeboxAuthError:
            if _retry:
                self._session_token = None
                return await self._request(method, path, json=json, _retry=False)
            raise

    async def list_redirs(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "fw/redir/")
        if not isinstance(result, list):
            return []
        return result

    async def set_redir_enabled(self, rule_id: int, enabled: bool) -> dict[str, Any]:
        return await self._request(
            "PUT", f"fw/redir/{rule_id}", json={"enabled": enabled}
        )

    @staticmethod
    def find_rule(
        redirs: list[dict[str, Any]], src_port: int, ip_proto: str
    ) -> dict[str, Any] | None:
        proto = ip_proto.lower()
        for rule in redirs:
            if (
                int(rule.get("src_port", -1)) == int(src_port)
                and str(rule.get("ip_proto", "")).lower() == proto
            ):
                return rule
        return None
