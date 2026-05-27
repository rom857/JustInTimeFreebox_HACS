"""Async client for the external grants API."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Max characters of any response body emitted to DEBUG logs.
_DEBUG_BODY_LIMIT = 1000


class GrantsApiError(Exception):
    """Error talking to the grants API."""


def _parse_iso(value: str) -> datetime:
    # Python 3.11+ fromisoformat supports offsets like +00:00 and fractional seconds.
    return datetime.fromisoformat(value)


def _truncate(text: str, limit: int = _DEBUG_BODY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


async def fetch_grants(
    session: aiohttp.ClientSession, url: str, api_key: str
) -> list[dict[str, Any]]:
    """Fetch the current grants (array or single object).

    Returns a list of dicts, each with normalised keys:
        targetId (str|None), granted (bool), port (int), protocol (str, lowercase),
        started_utc (datetime|None), expires_utc (datetime|None),
        remaining_seconds (int|None).

    Supports both old format (single dict) and new format (array of dicts)
    for backward compatibility during transition.
    """
    headers = {"X-Access-Key": api_key, "Accept": "application/json"}
    _LOGGER.debug(
        "Grants API request: GET %s (headers=%s)",
        url,
        # never log the key itself
        {"X-Access-Key": "***redacted***", "Accept": "application/json"},
    )
    try:
        async with session.get(url, headers=headers) as resp:
            text = await resp.text()
            content_type = resp.headers.get("Content-Type", "")
            _LOGGER.debug(
                "Grants API response: status=%s content_type=%r body=%s",
                resp.status,
                content_type,
                _truncate(text),
            )
            if resp.status >= 400:
                raise GrantsApiError(
                    f"grants API HTTP {resp.status}: {text[:200]}"
                )
            try:
                data = json.loads(text)
            except ValueError as err:
                raise GrantsApiError(
                    f"grants API returned non-JSON body "
                    f"(Content-Type={content_type!r}, status={resp.status}): "
                    f"{text[:200]!r}"
                ) from err
    except aiohttp.ClientError as err:
        raise GrantsApiError(f"grants API request failed: {err}") from err

    # Normalize to array: detect old format (dict) vs new format (list)
    if isinstance(data, dict):
        # Old format: single grant object
        data = [data]
    elif not isinstance(data, list):
        raise GrantsApiError(f"grants API returned non-object/non-array payload: {type(data)}")

    # Parse each grant
    grants = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            _LOGGER.warning(
                "Grants API: item #%d is not a dict, skipping: %s", idx, type(item)
            )
            continue

        started = item.get("startedUtc")
        expires = item.get("expiresUtc")
        try:
            started_dt = _parse_iso(started) if isinstance(started, str) else None
            expires_dt = _parse_iso(expires) if isinstance(expires, str) else None
        except ValueError as err:
            _LOGGER.warning(
                "Grants API: invalid timestamp in item #%d: %s", idx, err
            )
            started_dt = None
            expires_dt = None

        protocol = item.get("protocol")
        port = item.get("port")
        remaining = item.get("remainingSeconds")

        grant = {
            "targetId": item.get("targetId") or f"grant_{idx}",  # default if missing
            "granted": bool(item.get("granted", False)),
            "port": int(port) if isinstance(port, (int, str)) and str(port).isdigit() else None,
            "protocol": str(protocol).lower() if isinstance(protocol, str) else None,
            "started_utc": started_dt,
            "expires_utc": expires_dt,
            "remaining_seconds": int(remaining) if isinstance(remaining, (int, float)) else None,
        }
        grants.append(grant)

    return grants


# Keep old function name for backward compatibility
async def fetch_grant(
    session: aiohttp.ClientSession, url: str, api_key: str
) -> dict[str, Any]:
    """Deprecated: use fetch_grants() instead.

    Returns the first grant for backward compatibility.
    """
    grants = await fetch_grants(session, url, api_key)
    if not grants:
        raise GrantsApiError("No grants in API response")
    return grants[0]
