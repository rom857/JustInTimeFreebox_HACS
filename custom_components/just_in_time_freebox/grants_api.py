"""Async client for the external grants API."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiohttp


class GrantsApiError(Exception):
    """Error talking to the grants API."""


def _parse_iso(value: str) -> datetime:
    # Python 3.11+ fromisoformat supports offsets like +00:00 and fractional seconds.
    return datetime.fromisoformat(value)


async def fetch_grant(
    session: aiohttp.ClientSession, url: str, api_key: str
) -> dict[str, Any]:
    """Fetch the current grant.

    Returns a dict with normalised keys:
        granted (bool), port (int), protocol (str, lowercase),
        started_utc (datetime|None), expires_utc (datetime|None),
        remaining_seconds (int|None).
    """
    headers = {"X-Access-Key": api_key, "Accept": "application/json"}
    try:
        async with session.get(url, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise GrantsApiError(
                    f"grants API HTTP {resp.status}: {text[:200]}"
                )
            content_type = resp.headers.get("Content-Type", "")
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

    if not isinstance(data, dict):
        raise GrantsApiError(f"grants API returned non-object payload: {type(data)}")

    started = data.get("startedUtc")
    expires = data.get("expiresUtc")
    try:
        started_dt = _parse_iso(started) if isinstance(started, str) else None
        expires_dt = _parse_iso(expires) if isinstance(expires, str) else None
    except ValueError as err:
        raise GrantsApiError(f"invalid timestamp in grants payload: {err}") from err

    protocol = data.get("protocol")
    port = data.get("port")
    remaining = data.get("remainingSeconds")

    return {
        "granted": bool(data.get("granted", False)),
        "port": int(port) if isinstance(port, (int, str)) and str(port).isdigit() else None,
        "protocol": str(protocol).lower() if isinstance(protocol, str) else None,
        "started_utc": started_dt,
        "expires_utc": expires_dt,
        "remaining_seconds": int(remaining) if isinstance(remaining, (int, float)) else None,
    }
