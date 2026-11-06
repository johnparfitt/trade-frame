"""Thin Unusual Whales HTTP client.

Covers the Daily Market Report digest (``/api/market/daily-report``) and the
per-ticker gamma levels (``/api/stock/{ticker}/gex-levels``) behind the
watchlist Gamma Magnet column. Tests inject a fake
``httpx.Client``; this module is the only place that talks to
api.unusualwhales.com.

Before extending this, read ``prompts/unusual-whales/SKILL.md`` — do not invent
endpoints. Auth is ``Authorization: Bearer`` plus ``UW-CLIENT-API-ID: 100001``.
The token comes from config/env, never hardcoded.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Protocol

import httpx

BASE_URL = "https://api.unusualwhales.com"
DAILY_REPORT_PATH = "/api/market/daily-report"
GEX_LEVELS_PATH = "/api/stock/{ticker}/gex-levels"
UW_CLIENT_API_ID = "100001"
# Redact bearer tokens if they ever surface in an error string.
_BEARER_RE = re.compile(r"(Bearer\s+)[^\s'\"]+", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Strip bearer tokens from messages so they never hit the UI or logs."""
    return _BEARER_RE.sub(r"\1REDACTED", text)


class UnusualWhalesClientProtocol(Protocol):
    def daily_report(
        self, report_date: date | None = None, limit: int = 25
    ) -> dict[str, Any]: ...

    def gex_levels(self, ticker: str, *, source: str = "oi") -> dict[str, Any]: ...


class UnusualWhalesClient:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=60.0)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def daily_report(
        self, report_date: date | None = None, limit: int = 25
    ) -> dict[str, Any]:
        """Return UW's daily digest ``data`` object (defaults to last market day)."""
        params: dict[str, Any] = {"limit": limit}
        if report_date is not None:
            params["date"] = report_date.isoformat()
        payload = self._get(DAILY_REPORT_PATH, params=params)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def gex_levels(self, ticker: str, *, source: str = "oi") -> dict[str, Any]:
        """Return UW's precomputed gamma levels from ``/gex-levels``.

        The watchlist surfaces ``gamma_magnet`` only; UW also returns
        ``call_wall``, ``put_wall``, and ``gamma_flip`` in the same object.
        ``source`` picks the exposure basis: ``oi`` is open interest, ``vol`` is
        the day's directionalized volume. UW defaults to ``vol``.
        """
        payload = self._get(
            GEX_LEVELS_PATH.format(ticker=ticker.upper()), params={"source": source}
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "UW-CLIENT-API-ID": UW_CLIENT_API_ID,
            "Accept": "application/json",
        }
        response = self._http.get(url, params=params or None, headers=headers)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                redact_secrets(str(exc)),
                request=exc.request,
                response=exc.response,
            ) from None
        data = response.json()
        if not isinstance(data, dict):
            return {}
        return data
