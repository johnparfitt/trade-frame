"""Daily report HTTP API — no live network, generation is faked."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import DailyReport
from app.db.session import Base, get_db
from app.main import app
from app.services.report import reset_job_state_for_tests


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
        yield db, TestingSession
    finally:
        db.close()


@pytest.fixture
def client(db_session, monkeypatch):
    _db, TestingSession = db_session

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    reset_job_state_for_tests()
    monkeypatch.setattr("app.services.report.has_uw_key", lambda: False)
    monkeypatch.setattr("app.services.report.has_llm_key", lambda: False)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_job_state_for_tests()


def test_get_empty_reports(client: TestClient):
    res = client.get("/api/report")
    assert res.status_code == 200
    assert res.json() == []


def test_status_without_keys(client: TestClient):
    res = client.get("/api/report/status")
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "idle"
    assert body["has_uw_key"] is False
    assert body["has_llm_key"] is False
    assert body["as_of"] is None


def test_generate_without_uw_key_is_503(client: TestClient):
    res = client.post("/api/report/generate")
    assert res.status_code == 503
    assert "TF_UW_API_KEY" in res.json()["detail"]


def test_generate_without_llm_key_is_503(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.services.report.has_uw_key", lambda: True)
    res = client.post("/api/report/generate")
    assert res.status_code == 503
    assert "TF_ANTHROPIC_API_KEY" in res.json()["detail"]


def test_generate_starts_when_keys_configured(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.services.report.has_uw_key", lambda: True)
    monkeypatch.setattr("app.services.report.has_llm_key", lambda: True)

    def fake_start(**kwargs) -> bool:
        from app.services.report import _set_status

        _set_status(state="running", detail="Starting")
        return True

    monkeypatch.setattr("app.services.report.try_start_generate", fake_start)
    res = client.post("/api/report/generate")
    assert res.status_code == 202
    assert res.json()["state"] == "running"


def test_generate_conflict_when_already_running(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.services.report.has_uw_key", lambda: True)
    monkeypatch.setattr("app.services.report.has_llm_key", lambda: True)
    monkeypatch.setattr("app.services.report.try_start_generate", lambda **kwargs: False)
    res = client.post("/api/report/generate")
    assert res.status_code == 409


def test_get_reports_returns_stored_rows(client: TestClient, db_session):
    db, _ = db_session
    db.add(
        DailyReport(
            session_date=date(2026, 9, 18),
            generated_at=datetime.now(timezone.utc),
            model="test-model",
            analysis="- an anomaly",
            raw_json="{}",
        )
    )
    db.commit()

    res = client.get("/api/report")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["session_date"] == "2026-09-18"
    assert body[0]["analysis"] == "- an anomaly"
    assert body[0]["model"] == "test-model"


def test_get_report_raw_returns_stored_json(client: TestClient, db_session):
    db, _ = db_session
    db.add(
        DailyReport(
            session_date=date(2026, 9, 18),
            generated_at=datetime.now(timezone.utc),
            model="test-model",
            analysis="- an anomaly",
            raw_json='{"date":"2026-09-18","unusual_options":[]}',
        )
    )
    db.commit()

    res = client.get("/api/report/2026-09-18/raw")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {"date": "2026-09-18", "unusual_options": []}


def test_get_report_raw_unknown_date_is_404(client: TestClient):
    res = client.get("/api/report/2026-01-01/raw")
    assert res.status_code == 404
