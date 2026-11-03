"""Watchlist CRUD via the HTTP API. Market data is mocked — no yfinance, no live network."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, get_db
from app.main import app
from app.services.market_data import SymbolSnapshot

KNOWN = {"AAPL", "MSFT"}


def _snapshot(symbol: str) -> SymbolSnapshot:
    return SymbolSnapshot(
        symbol=symbol,
        company="Apple Inc.",
        industry="Consumer Electronics",
        last=190.0,
        pct_change=1.23,
        rvol=1.5,
        range_pct_52w=80.0,
        next_earnings="2026-10-01",
        avg_volume=50_000_000,
        atr_pct_50=0.5,
        adr_pct=2.1,
        float_shares=15_000_000_000,
        float_pct=99.0,
        short_pct_float=0.8,
        days_to_cover=1.2,
        gamma_magnet=195.0,
        dilution_risk=35.0,
        dilution_note="Shares +10% YoY; active S-3 shelf",
        sec_filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/y-index.htm",
        sec_filing_form="8-K",
        sec_filing_date="2026-09-30",
        sec_significance=82,
        sec_significance_note="Significance 82/100 (bearish)\n8-K filed 2026-09-30\nPriced offering",
        sec_significance_direction="bearish",
        error=None,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(
        "app.services.market_data.symbol_exists",
        lambda symbol: symbol.strip().upper() in KNOWN,
    )
    monkeypatch.setattr(
        "app.services.market_data.get_snapshot",
        lambda symbol: _snapshot(symbol.strip().upper()),
    )

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_get_empty_watchlist(client: TestClient):
    res = client.get("/api/watchlist")
    assert res.status_code == 200
    assert res.json() == []


def test_add_then_list(client: TestClient):
    res = client.post("/api/watchlist", json={"symbol": "AAPL"})
    assert res.status_code == 201
    body = res.json()
    assert body["symbol"] == "AAPL"
    assert body["company"] == "Apple Inc."

    listed = client.get("/api/watchlist")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["gamma_magnet"] == 195.0


def test_gamma_magnet_is_served_on_the_row(client: TestClient):
    client.post("/api/watchlist", json={"symbol": "AAPL"})

    row = client.get("/api/watchlist").json()[0]

    assert row["gamma_magnet"] == 195.0


def test_add_unknown_symbol_not_stored(client: TestClient):
    res = client.post("/api/watchlist", json={"symbol": "ZZZZZ"})
    assert res.status_code == 404
    listed = client.get("/api/watchlist")
    assert listed.json() == []


def test_add_duplicate_is_idempotent(client: TestClient):
    first = client.post("/api/watchlist", json={"symbol": "AAPL"})
    second = client.post("/api/watchlist", json={"symbol": "AAPL"})
    assert first.status_code == 201
    assert second.status_code == 201
    listed = client.get("/api/watchlist")
    assert len(listed.json()) == 1


def test_delete_then_missing(client: TestClient):
    client.post("/api/watchlist", json={"symbol": "AAPL"})
    removed = client.delete("/api/watchlist/AAPL")
    assert removed.status_code == 204
    assert client.get("/api/watchlist").json() == []

    missing = client.delete("/api/watchlist/AAPL")
    assert missing.status_code == 404


def test_blank_and_invalid_body_are_422(client: TestClient):
    assert client.post("/api/watchlist", json={"symbol": ""}).status_code == 422
    assert client.post("/api/watchlist", json={"symbol": "   "}).status_code == 422
    assert client.post("/api/watchlist", json={}).status_code == 422


def test_symbols_load_concurrently_in_order(client: TestClient, monkeypatch):
    client.post("/api/watchlist", json={"symbol": "MSFT"})
    client.post("/api/watchlist", json={"symbol": "AAPL"})
    # Each lookup waits for the other: a serial loop would break the barrier.
    barrier = threading.Barrier(2, timeout=5)

    def slow_snapshot(symbol: str) -> SymbolSnapshot:
        barrier.wait()
        return _snapshot(symbol)

    monkeypatch.setattr("app.services.market_data.get_snapshot", slow_snapshot)

    rows = client.get("/api/watchlist").json()

    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]
    assert all(r["error"] is None for r in rows)


def test_one_failing_lookup_does_not_fail_the_list(client: TestClient, monkeypatch):
    client.post("/api/watchlist", json={"symbol": "AAPL"})
    client.post("/api/watchlist", json={"symbol": "MSFT"})

    def flaky(symbol: str) -> SymbolSnapshot:
        if symbol == "MSFT":
            raise RuntimeError("boom")
        return _snapshot(symbol)

    monkeypatch.setattr("app.services.market_data.get_snapshot", flaky)

    rows = {r["symbol"]: r for r in client.get("/api/watchlist").json()}

    assert rows["AAPL"]["error"] is None
    assert rows["MSFT"]["error"] == "Lookup failed: boom"


def test_symbol_stored_uppercase(client: TestClient):
    res = client.post("/api/watchlist", json={"symbol": "aapl"})
    assert res.status_code == 201
    assert res.json()["symbol"] == "AAPL"
    assert client.get("/api/watchlist").json()[0]["symbol"] == "AAPL"
