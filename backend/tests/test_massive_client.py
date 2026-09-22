"""Massive HTTP client pagination — no live network."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.services.massive import MAX_TICKER_PAGES, MassiveClient, redact_secrets


def _client(handler, monkeypatch) -> MassiveClient:
    monkeypatch.setattr("app.services.massive.MIN_INTERVAL_SEC", 0)
    transport = httpx.MockTransport(handler)
    return MassiveClient("test-key", client=httpx.Client(transport=transport, timeout=5.0))


def test_next_url_is_followed_as_is_without_extra_apikey(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "cursor=" not in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [{"ticker": "AAA"}],
                    "next_url": "https://api.massive.com/v3/reference/tickers?cursor=abc&apiKey=test-key",
                },
            )
        return httpx.Response(200, json={"results": [{"ticker": "BBB"}]})

    client = _client(handler, monkeypatch)
    try:
        symbols = client.common_stock_tickers()
    finally:
        client.close()

    assert symbols == ["AAA", "BBB"]
    assert seen[0].count("apiKey=") == 1
    assert "cursor=abc" in seen[1]
    assert seen[1].count("apiKey=") == 1


def test_pagination_stops_without_next_url(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": [{"ticker": "AAA"}]})

    client = _client(handler, monkeypatch)
    try:
        symbols = client.common_stock_tickers()
    finally:
        client.close()

    assert symbols == ["AAA"]
    assert calls["n"] == 1


def test_pagination_stops_at_page_cap(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "results": [{"ticker": f"T{calls['n']}"}],
                "next_url": f"https://api.massive.com/v3/reference/tickers?cursor={calls['n']}&apiKey=test-key",
            },
        )

    client = _client(handler, monkeypatch)
    try:
        symbols = client.common_stock_tickers()
    finally:
        client.close()

    assert calls["n"] == MAX_TICKER_PAGES
    assert len(symbols) == MAX_TICKER_PAGES


def test_redact_secrets_strips_apikey_query_param():
    raw = "Client error '403 Forbidden' for url 'https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-09-11?adjusted=true&apiKey=super-secret'"
    out = redact_secrets(raw)
    assert "super-secret" not in out
    assert "apiKey=REDACTED" in out


def test_grouped_daily_skips_unpublished_403(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"status": "NOT_AUTHORIZED"})

    client = _client(handler, monkeypatch)
    try:
        assert client.grouped_daily(date(2026, 9, 11)) == []
    finally:
        client.close()


def test_http_error_message_does_not_include_api_key(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "ERROR"})

    client = _client(handler, monkeypatch)
    try:
        with pytest.raises(httpx.HTTPStatusError) as err:
            client.grouped_daily(date(2026, 9, 10))
    finally:
        client.close()
    message = str(err.value)
    assert "test-key" not in message
    assert "apiKey=REDACTED" in message
