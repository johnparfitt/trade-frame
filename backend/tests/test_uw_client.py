"""Unusual Whales HTTP client — no live network."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.services.uw import (
    DAILY_REPORT_PATH,
    UW_CLIENT_API_ID,
    UnusualWhalesClient,
    redact_secrets,
)


def _client(handler) -> UnusualWhalesClient:
    transport = httpx.MockTransport(handler)
    return UnusualWhalesClient("secret-token", client=httpx.Client(transport=transport, timeout=5.0))


def test_daily_report_sends_auth_headers_and_returns_data():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["client_id"] = request.headers.get("UW-CLIENT-API-ID")
        return httpx.Response(200, json={"data": {"date": "2026-09-18", "tide": {}}})

    client = _client(handler)
    try:
        data = client.daily_report()
    finally:
        client.close()

    assert data == {"date": "2026-09-18", "tide": {}}
    assert DAILY_REPORT_PATH in seen["url"]
    assert seen["auth"] == "Bearer secret-token"
    assert seen["client_id"] == UW_CLIENT_API_ID


def test_daily_report_passes_date_and_limit():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {}})

    client = _client(handler)
    try:
        client.daily_report(report_date=date(2026, 9, 2), limit=50)
    finally:
        client.close()

    assert "date=2026-09-02" in seen["url"]
    assert "limit=50" in seen["url"]


def test_daily_report_returns_empty_dict_when_data_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"something_else": 1})

    client = _client(handler)
    try:
        assert client.daily_report() == {}
    finally:
        client.close()


def test_gex_levels_returns_data_object_from_the_oi_source():
    seen: dict = {}
    levels = {"call_wall": "210", "put_wall": "190", "gamma_magnet": "200"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": levels})

    client = _client(handler)
    try:
        assert client.gex_levels("mrna") == levels
    finally:
        client.close()

    assert "/api/stock/MRNA/gex-levels" in seen["url"]
    # UW defaults to the volume basis; the watchlist wants open interest.
    assert "source=oi" in seen["url"]


def test_gex_levels_returns_empty_dict_when_data_is_not_an_object():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["not", "an object"]})

    client = _client(handler)
    try:
        assert client.gex_levels("MRNA") == {}
    finally:
        client.close()


def test_financials_sends_report_type_and_returns_data():
    seen: dict = {}
    data = {"balance_sheets": [{"ticker": "AAPL"}], "cash_flows": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": data})

    client = _client(handler)
    try:
        assert client.financials("aapl") == data
    finally:
        client.close()

    assert "/api/stock/AAPL/financials" in seen["url"]
    assert "report_type=quarterly" in seen["url"]


def test_financials_returns_empty_dict_when_data_is_not_an_object():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    client = _client(handler)
    try:
        assert client.financials("AAPL") == {}
    finally:
        client.close()


def test_redact_secrets_strips_bearer_token():
    raw = "error for request with header Authorization: Bearer super-secret-token"
    out = redact_secrets(raw)
    assert "super-secret-token" not in out
    assert "Bearer REDACTED" in out


def test_http_error_message_does_not_include_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "ERROR"})

    client = _client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError) as err:
            client.daily_report()
    finally:
        client.close()
    assert "secret-token" not in str(err.value)
