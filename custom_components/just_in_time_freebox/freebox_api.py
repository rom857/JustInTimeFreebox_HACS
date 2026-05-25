"""Helpers around the freebox-api library.

This module is a thin wrapper that:
- re-exports the library's public types so the rest of the integration
  doesn't import ``freebox_api`` directly,
- adds a plain-HTTP discovery helper (`discover_api`) used by the config
  flow to learn ``api_domain`` / ``https_port`` / ``api_version`` before
  asking the library to open a TLS session,
- provides a `find_rule` helper for matching grant ``(port, protocol)``
  against the Freebox port-forwarding list.

The library handles SSL (ships the Freebox CA), pairing, token persistence,
and the HMAC-SHA1 session handshake.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

# Re-exported library symbols. Keep these imports here so the rest of
# the integration can ``from .freebox_api import Freepybox, ...``.
from freebox_api import Freepybox
from freebox_api.exceptions import (
    AuthorizationError,
    HttpRequestError,
    InvalidTokenError,
    NotOpenError,
)

_LOGGER = logging.getLogger(__name__)


class FreeboxApiError(Exception):
    """Local discovery / matching errors (separate from library exceptions)."""


async def discover_api(
    session: aiohttp.ClientSession, host: str
) -> dict[str, Any]:
    """Probe ``http://<host>/api_version`` (no auth, plain HTTP).

    Returns the JSON payload. Keys of interest:
    - ``api_version`` (e.g. ``"15.0"``)
    - ``api_domain``  (HTTPS-routable domain like ``<token>.fbxos.fr``)
    - ``https_port``  (TLS port for the Freebox API)
    - ``https_available`` (bool)
    """
    url = f"http://{host}/api_version"
    _LOGGER.debug("Freebox discover: GET %s", url)
    try:
        async with session.get(url) as resp:
            text = await resp.text()
            _LOGGER.debug(
                "Freebox discover response: status=%s body=%s",
                resp.status,
                text[:500],
            )
            if resp.status >= 400:
                raise FreeboxApiError(
                    f"discover HTTP {resp.status}: {text[:200]}"
                )
            data = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise FreeboxApiError(f"discover failed: {err}") from err

    if not isinstance(data, dict):
        raise FreeboxApiError(f"discover returned non-object: {type(data)}")
    return data


def find_rule(
    redirs: list[dict[str, Any]], lan_port: int, ip_proto: str
) -> dict[str, Any] | None:
    """Return the first redir whose ``lan_port`` + ``ip_proto`` match.

    Freebox redir objects expose ``lan_port`` (destination on the LAN)
    and ``ip_proto``; the WAN-side port is ``wan_port`` / ``wan_port_start``
    and is not used for matching here.
    """
    proto = ip_proto.lower()
    for rule in redirs:
        if (
            int(rule.get("lan_port", -1)) == int(lan_port)
            and str(rule.get("ip_proto", "")).lower() == proto
        ):
            return rule
    return None


__all__ = [
    "AuthorizationError",
    "FreeboxApiError",
    "Freepybox",
    "HttpRequestError",
    "InvalidTokenError",
    "NotOpenError",
    "discover_api",
    "find_rule",
]
