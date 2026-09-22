"""Daily report generation — fake UW client + fake LLM, no live network."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import DailyReport
from app.db.session import Base
from app.services.report import run_generate, without_zero_dte_unusual_options


class FakeUW:
    def __init__(self, data: dict):
        self.data = data
        self.calls = 0

    def daily_report(self, report_date=None, limit: int = 25) -> dict:
        self.calls += 1
        return dict(self.data)


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'report.db'}",
        connect_args={"check_same_thread": False},
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


def test_run_generate_persists_report(db_session, monkeypatch):
    class FakeSettings:
        llm_model = "test-model"

    monkeypatch.setattr("app.services.report.get_settings", lambda: FakeSettings())

    uw = FakeUW({"date": "2026-09-18", "tide": {"data": []}})
    captured: dict = {}

    def fake_analyze(data: dict) -> str:
        captured["data"] = data
        return "- bullet one\n- bullet two"

    row = run_generate(db_session, uw, analyze=fake_analyze)

    assert uw.calls == 1
    assert captured["data"]["date"] == "2026-09-18"
    assert row.session_date == date(2026, 9, 18)
    assert row.model == "test-model"
    assert row.analysis == "- bullet one\n- bullet two"
    assert "tide" in row.raw_json

    rows = list(db_session.scalars(select(DailyReport)).all())
    assert len(rows) == 1


def test_run_generate_upserts_same_session_date(db_session, monkeypatch):
    class FakeSettings:
        llm_model = "test-model"

    monkeypatch.setattr("app.services.report.get_settings", lambda: FakeSettings())

    uw = FakeUW({"date": "2026-09-18"})
    run_generate(db_session, uw, analyze=lambda d: "first")
    run_generate(db_session, uw, analyze=lambda d: "second")

    rows = list(db_session.scalars(select(DailyReport)).all())
    assert len(rows) == 1
    assert rows[0].analysis == "second"


def test_run_generate_errors_on_empty_digest(db_session, monkeypatch):
    class FakeSettings:
        llm_model = "test-model"

    monkeypatch.setattr("app.services.report.get_settings", lambda: FakeSettings())

    uw = FakeUW({})
    with pytest.raises(RuntimeError, match="empty daily report"):
        run_generate(db_session, uw, analyze=lambda d: "unused")


def test_without_zero_dte_drops_zero_keeps_others():
    original = {
        "date": "2026-09-18",
        "unusual_options": [
            {"ticker": "SPY", "dte": 0},
            {"ticker": "AAPL", "dte": 21},
            {"ticker": "NVDA", "dte": "0"},
            {"ticker": "MSFT"},
            {"ticker": "QQQ", "dte": 0.0},
        ],
        "tide": {"data": []},
    }
    filtered = without_zero_dte_unusual_options(original)
    assert [row["ticker"] for row in filtered["unusual_options"]] == ["AAPL", "MSFT"]
    assert original["unusual_options"][0]["ticker"] == "SPY"
    assert original is not filtered


def test_run_generate_strips_zero_dte_before_llm_and_persist(db_session, monkeypatch):
    class FakeSettings:
        llm_model = "test-model"

    monkeypatch.setattr("app.services.report.get_settings", lambda: FakeSettings())

    uw = FakeUW(
        {
            "date": "2026-09-18",
            "unusual_options": [
                {"ticker": "SPY", "dte": 0},
                {"ticker": "AAPL", "dte": 30},
            ],
        }
    )
    captured: dict = {}

    def fake_analyze(data: dict) -> str:
        captured["data"] = data
        return "ok"

    row = run_generate(db_session, uw, analyze=fake_analyze)
    tickers = [item["ticker"] for item in captured["data"]["unusual_options"]]
    assert tickers == ["AAPL"]
    stored = json.loads(row.raw_json)
    assert [item["ticker"] for item in stored["unusual_options"]] == ["AAPL"]
    assert uw.data["unusual_options"][0]["ticker"] == "SPY"
