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
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

AUTH_HEADER = "X-Fbx-App-Auth"

# Max characters of any response body emitted to DEBUG logs.
_DEBUG_BODY_LIMIT = 1000

# Keys in JSON bodies that must be redacted before being logged.
_SECRET_KEYS = frozenset({"app_token", "password", "session_token", "challenge"})


def _truncate(text: str, limit: int = _DEBUG_BODY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def _redact(obj: Any) -> Any:
    """Return a copy of *obj* with secret-like values masked."""
    if isinstance(obj, dict):
        return {
            k: ("***redacted***" if k in _SECRET_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


async def _request_and_log(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Issue an HTTP request with uniform DEBUG logging.

    Returns (status, body_text). Raises aiohttp.ClientError on transport
    failure. The caller is responsible for interpreting status + body.
    """
    log_headers = (
        {k: ("***redacted***" if k == AUTH_HEADER else v) for k, v in headers.items()}
        if headers
        else None
    )
    _LOGGER.debug(
        "Freebox request: %s %s body=%s headers=%s",
        method,
        url,
        _redact(json_body) if json_body is not None else None,
        log_headers,
    )
    async with session.request(method, url, json=json_body, headers=headers) as resp:
        text = await resp.text()
        _LOGGER.debug(
            "Freebox response: %s %s -> status=%s content_type=%r body=%s",
            method,
            url,
            resp.status,
            resp.headers.get("Content-Type", ""),
            _truncate(text),
        )
        return resp.status, text


def _parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as err:
        raise FreeboxApiError(f"invalid JSON from Freebox: {err}: {text[:200]!r}") from err
    if not isinstance(data, dict):
        raise FreeboxApiError(f"unexpected payload type from Freebox: {type(data)}")
    return data



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
        status, text = await _request_and_log(session, "GET", url)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"discover failed: {err}") from err
    if status >= 400:
        raise FreeboxApiError(f"discover HTTP {status}: {text[:200]}")
    data = _parse_json(text)

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
        status, text = await _request_and_log(session, "POST", url, json_body=body)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"request_authorization failed: {err}") from err
    if status >= 400:
        raise FreeboxApiError(f"request_authorization HTTP {status}: {text[:200]}")
    data = _parse_json(text)
    result = _check_success(data)
    return result["app_token"], int(result["track_id"])


async def track_authorization(
    session: aiohttp.ClientSession, api_base_url: str, track_id: int
) -> str:
    """Return one of: ``pending``, ``granted``, ``denied``, ``timeout``, ``unknown``."""
    url = f"{api_base_url}login/authorize/{track_id}"
    try:
        status, text = await _request_and_log(session, "GET", url)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"track_authorization failed: {err}") from err
    if status >= 400:
        raise FreeboxApiError(f"track_authorization HTTP {status}: {text[:200]}")
    data = _parse_json(text)
    result = _check_success(data)
    return str(result.get("status", "unknown"))


async def _get_challenge(
    session: aiohttp.ClientSession, api_base_url: str
) -> str:
    url = f"{api_base_url}login/"
    status, text = await _request_and_log(session, "GET", url)
    if status >= 400:
        raise FreeboxApiError(f"_get_challenge HTTP {status}: {text[:200]}")
    data = _parse_json(text)
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
        status, text = await _request_and_log(
            session, "POST", url, json_body={"app_id": app_id, "password": password}
        )
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"open_session failed: {err}") from err
    if status >= 400:
        raise FreeboxApiError(f"open_session HTTP {status}: {text[:200]}")
    data = _parse_json(text)
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
            status, text = await _request_and_log(
                self._session, method, url,
                json_body=json, headers=self._auth_headers(),
            )
        except aiohttp.ClientError as err:
            raise FreeboxApiError(f"{method} {path} failed: {err}") from err
        if status == 401 and _retry:
            _LOGGER.debug("Freebox 401 on %s %s -> dropping session token and retrying", method, path)
            self._session_token = None
            return await self._request(method, path, json=json, _retry=False)
        if status >= 400:
            raise FreeboxApiError(f"{method} {path} HTTP {status}: {text[:200]}")
        data = _parse_json(text)
        try:
            return _check_success(data)
        except FreeboxAuthError:
            if _retry:
                _LOGGER.debug("Freebox auth error on %s %s -> dropping session token and retrying", method, path)
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
        redirs: list[dict[str, Any]], lan_port: int, ip_proto: str
    ) -> dict[str, Any] | None:
        """Find the redir rule whose LAN-side port + protocol match.

        Freebox redir objects expose ``lan_port`` (destination on the LAN)
        and ``ip_proto``; the WAN-side port is ``wan_port`` /
        ``wan_port_start`` and is not used for matching here.
        """
        proto = ip_proto.lower()
        for rule in redirs:
            if (
                int(rule.get("lan_port", -1)) == int(lan_port)
                and str(rule.get("ip_proto", "")).lower() == proto
            ):
                return rule
        return None
