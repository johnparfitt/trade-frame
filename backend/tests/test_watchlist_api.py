"""Watchlist CRUD via the HTTP API. Market data is mocked — no yfinance, no live network."""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import SecFilingBrief, SecSignificanceScore
from app.db.session import Base, get_db
from app.main import app
from app.services import edgar, llm, market_data, sec_significance
from app.services.edgar import FilingInfo, FilingRef
from app.services.market_data import SymbolSnapshot
from app.services.sec_significance import SecFiling

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
        sec_filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/y-index.htm",
        sec_filing_form="8-K",
        sec_filing_date="2026-09-30",
        sec_significance=82,
        sec_significance_note="Significance 82/100 (bearish)\n8-K filed 2026-09-30\nPriced offering",
        sec_significance_direction="bearish",
        sec_filings=(
            SecFiling(
                score=82,
                direction="bearish",
                reason="Priced offering",
                form="8-K",
                date="2026-09-30",
                url="https://www.sec.gov/Archives/edgar/data/320193/x/y-index.htm",
            ),
            SecFiling(
                score=70,
                direction="bearish",
                reason="Shelf registration",
                form="S-3",
                date="2026-09-20",
                url="https://www.sec.gov/Archives/edgar/data/320193/x/shelf-index.htm",
            ),
        ),
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
    assert [f["score"] for f in body["sec_filings"]] == [82, 70]
    assert body["sec_filings"][1]["form"] == "S-3"

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


def _filing_info(*pairs: tuple[str, int, str]) -> FilingInfo:
    today = date.today()
    refs = tuple(
        FilingRef(
            form="8-K",
            date=(today - timedelta(days=days)).isoformat(),
            url=f"https://www.sec.gov/{accession}-index.htm",
            accession=accession,
            primary_doc="x.htm",
            doc_url=url,
        )
        for accession, days, url in pairs
    )
    return FilingInfo(ok=True, found=True, candidates=refs, cik=1)


def _patch_filings(monkeypatch, info: FilingInfo, run) -> None:
    sec_significance.clear_cache()
    monkeypatch.setattr(sec_significance.edgar, "get_filings", lambda symbol, session=None: info)
    monkeypatch.setattr(
        sec_significance.edgar,
        "fetch_document_text",
        lambda url, *, session, max_chars=edgar.DOC_MAX_CHARS: edgar.DocText(text=f"body {url}"),
    )
    monkeypatch.setattr(sec_significance.llm, "run", run)


def _brief_rows(significance_store) -> list[SecFilingBrief]:
    with significance_store() as db:
        return list(db.execute(select(SecFilingBrief)).scalars().all())


def test_sec_brief_persists_and_second_call_skips_llm(client, monkeypatch, significance_store):
    calls: list[str] = []

    def run(prompt: str, *, model=None, client=None) -> str:  # noqa: ARG001
        calls.append(prompt)
        if "Filing 1" in prompt:
            return "Shelf last week, takedown today."
        if "http://d1" in prompt:
            return '{"score": 85, "direction": "bearish", "reason": "takedown"}'
        return '{"score": 80, "direction": "bearish", "reason": "shelf"}'

    _patch_filings(
        monkeypatch,
        _filing_info(("acc-a", 1, "http://d1"), ("acc-b", 8, "http://d2")),
        run,
    )

    first = client.post("/api/watchlist/META/sec-brief")
    assert first.status_code == 200
    assert first.json()["text"] == "Shelf last week, takedown today."
    assert len(calls) == 3  # two scores, then the combined read
    assert len(_brief_rows(significance_store)) == 1

    second = client.post("/api/watchlist/META/sec-brief")
    assert second.status_code == 200
    assert second.json()["text"] == first.json()["text"]
    assert len(calls) == 3


def test_sec_brief_one_qualifier_is_404(client, monkeypatch, significance_store):
    def run(prompt: str, *, model=None, client=None) -> str:  # noqa: ARG001
        return '{"score": 80, "direction": "bearish", "reason": "offering"}'

    _patch_filings(monkeypatch, _filing_info(("acc-only", 1, "http://only")), run)

    res = client.post("/api/watchlist/META/sec-brief")
    assert res.status_code == 404
    assert _brief_rows(significance_store) == []


def test_sec_brief_failure_is_not_stored(client, monkeypatch, significance_store):
    def run(prompt: str, *, model=None, client=None) -> str:  # noqa: ARG001
        if "Filing 1" in prompt:
            raise llm.LLMError("down")
        return '{"score": 80, "direction": "bearish", "reason": "offering"}'

    _patch_filings(
        monkeypatch,
        _filing_info(("acc-x", 1, "http://x"), ("acc-y", 6, "http://y")),
        run,
    )

    res = client.post("/api/watchlist/META/sec-brief")
    assert res.status_code == 503
    assert _brief_rows(significance_store) == []


def _score_rows(significance_store) -> list[SecSignificanceScore]:
    with significance_store() as db:
        return list(db.execute(select(SecSignificanceScore)).scalars().all())


def _review_run(prompt: str, *, model=None, client=None) -> str:  # noqa: ARG001
    if "agree with the current analysis" in prompt:
        return (
            '{"agrees": false, "score": 28, "direction": "bullish",'
            ' "reason": "It is a buyback"}'
        )
    return '{"score": 82, "direction": "bearish", "reason": "treasury dilution"}'


def test_sec_review_does_not_change_the_stored_score(client, monkeypatch, significance_store):
    calls: list[str] = []

    def run(prompt: str, *, model=None, client=None) -> str:  # noqa: ARG001
        calls.append(prompt)
        return _review_run(prompt)

    _patch_filings(monkeypatch, _filing_info(("acc-a", 1, "http://d1")), run)
    url = "https://www.sec.gov/acc-a-index.htm"

    res = client.post("/api/watchlist/META/sec-review", json={"url": url})

    assert res.status_code == 200
    body = res.json()
    assert body["agrees"] is False
    assert body["score"] == 28
    assert body["direction"] == "bullish"
    assert body["will_show"] is False
    assert "treasury dilution" in calls[-1]
    rows = _score_rows(significance_store)
    assert len(rows) == 1
    assert rows[0].score == 82
    assert rows[0].direction == "bearish"


def test_sec_review_apply_replaces_the_score(client, monkeypatch, significance_store):
    _patch_filings(monkeypatch, _filing_info(("acc-a", 1, "http://d1")), _review_run)
    url = "https://www.sec.gov/acc-a-index.htm"
    market_data._cache["META"] = (time.monotonic() + 30, SymbolSnapshot(symbol="META"))

    res = client.post(
        "/api/watchlist/META/sec-review/apply",
        json={"url": url, "score": 28, "direction": "bullish", "reason": "It is a buyback"},
    )

    assert res.status_code == 200
    assert res.json()["will_show"] is False
    rows = _score_rows(significance_store)
    assert len(rows) == 1
    assert rows[0].accession == "acc-a"
    assert rows[0].score == 28
    assert rows[0].direction == "bullish"
    assert rows[0].reason == "It is a buyback"
    assert "META" not in market_data._cache


def test_sec_review_unknown_filing_is_404(client, monkeypatch, significance_store):
    _patch_filings(monkeypatch, _filing_info(("acc-a", 1, "http://d1")), _review_run)

    res = client.post(
        "/api/watchlist/META/sec-review",
        json={"url": "https://www.sec.gov/missing-index.htm"},
    )

    assert res.status_code == 404
    assert _score_rows(significance_store) == []


def test_sec_review_failure_leaves_the_score(client, monkeypatch, significance_store):
    def run(prompt: str, *, model=None, client=None) -> str:  # noqa: ARG001
        if "agree with the current analysis" in prompt:
            raise llm.LLMError("down")
        return '{"score": 82, "direction": "bearish", "reason": "treasury dilution"}'

    _patch_filings(monkeypatch, _filing_info(("acc-a", 1, "http://d1")), run)
    url = "https://www.sec.gov/acc-a-index.htm"

    res = client.post("/api/watchlist/META/sec-review", json={"url": url})

    assert res.status_code == 503
    rows = _score_rows(significance_store)
    assert len(rows) == 1
    assert rows[0].score == 82
