"""Thin Massive (ex-Polygon) HTTP client for EOD breadth.

Paces requests to stay under the Basic plan's 5 calls/minute. Tests inject a
fake client; this module is the only place that talks to api.massive.com.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import httpx

BASE_URL = "https://api.massive.com"
MIN_INTERVAL_SEC = 12.1  # 5 calls/min with a little slack
MAX_TICKER_PAGES = 20  # safety cap so a bad next_url cannot loop for hours
UNPUBLISHED_STATUSES = {403, 404}
_APIKEY_RE = re.compile(r"(apiKey=)[^&\s]+", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Strip API keys from URLs/messages so they never hit the UI or logs."""
    return _APIKEY_RE.sub(r"\1REDACTED", text)


@dataclass(frozen=True)
class GroupedBar:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class MassiveClientProtocol(Protocol):
    def grouped_daily(self, session_date: date) -> list[GroupedBar]: ...

    def common_stock_tickers(
        self, on_page: Callable[[int], None] | None = None
    ) -> list[str]: ...


class MassiveClient:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=60.0)
        self._last_call = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def grouped_daily(self, session_date: date) -> list[GroupedBar]:
        path = f"/v2/aggs/grouped/locale/us/market/stocks/{session_date.isoformat()}"
        try:
            payload = self._get(path, params={"adjusted": "true"})
        except httpx.HTTPStatusError as exc:
            # Basic EOD is not entitled to today's unpublished file (403) and
            # holidays/unknown dates may 404. Skip and keep going.
            if exc.response is not None and exc.response.status_code in UNPUBLISHED_STATUSES:
                return []
            raise
        results = payload.get("results") or []
        bars: list[GroupedBar] = []
        for row in results:
            symbol = row.get("T")
            if not symbol:
                continue
            try:
                bars.append(
                    GroupedBar(
                        symbol=str(symbol).upper(),
                        open=float(row["o"]),
                        high=float(row["h"]),
                        low=float(row["l"]),
                        close=float(row["c"]),
                        volume=float(row["v"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return bars

    def common_stock_tickers(
        self, on_page: Callable[[int], None] | None = None
    ) -> list[str]:
        symbols: list[str] = []
        url = f"{BASE_URL}/v3/reference/tickers"
        params: dict[str, Any] | None = {
            "market": "stocks",
            "active": "true",
            "type": "CS",
            "limit": 1000,
        }
        attach_api_key = True
        page = 0
        while url:
            page += 1
            if page > MAX_TICKER_PAGES:
                break
            if on_page is not None:
                on_page(page)
            payload = self._get(url, params=params, attach_api_key=attach_api_key)
            params = None
            attach_api_key = False  # next_url already carries auth/cursor
            results = payload.get("results") or []
            if not results:
                break
            for row in results:
                ticker = row.get("ticker")
                if ticker:
                    symbols.append(str(ticker).upper())
            next_url = payload.get("next_url")
            url = next_url if isinstance(next_url, str) and next_url else ""
        return symbols

    def _get(
        self,
        url_or_path: str,
        params: dict[str, Any] | None = None,
        *,
        attach_api_key: bool = True,
    ) -> dict[str, Any]:
        self._pace()
        url = url_or_path if url_or_path.startswith("http") else f"{BASE_URL}{url_or_path}"
        query = dict(params or {})
        if attach_api_key:
            query["apiKey"] = self._api_key
        response = self._http.get(url, params=query or None)
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

    def _pace(self) -> None:
        wait = MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
