"""Breadth HTTP API and refresh job. Massive is faked — no live network."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, get_db
from app.main import app
from app.services.breadth import reset_job_state_for_tests, run_refresh
from app.services.massive import GroupedBar


class FakeMassive:
    def __init__(self, tickers: list[str], bars_by_date: dict[date, list[GroupedBar]]):
        self.tickers = tickers
        self.bars_by_date = bars_by_date
        self.grouped_calls: list[date] = []

    def common_stock_tickers(self, on_page=None) -> list[str]:
        if on_page is not None:
            on_page(1)
        return list(self.tickers)

    def grouped_daily(self, session_date: date) -> list[GroupedBar]:
        self.grouped_calls.append(session_date)
        return list(self.bars_by_date.get(session_date, []))


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'breadth.db'}",
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
    monkeypatch.setattr("app.services.breadth.has_api_key", lambda: False)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_job_state_for_tests()


def test_get_empty_breadth(client: TestClient):
    res = client.get("/api/breadth")
    assert res.status_code == 200
    assert res.json() == []


def test_status_without_key(client: TestClient):
    res = client.get("/api/breadth/status")
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "idle"
    assert body["has_api_key"] is False
    assert body["as_of"] is None


def test_refresh_without_key_is_503(client: TestClient):
    res = client.post("/api/breadth/refresh")
    assert res.status_code == 503
    assert "TF_MASSIVE_API_KEY" in res.json()["detail"]


def test_refresh_starts_when_key_configured(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.services.breadth.has_api_key", lambda: True)

    def fake_start() -> bool:
        from app.services.breadth import _set_status

        _set_status(state="running", detail="Starting")
        return True

    monkeypatch.setattr("app.services.breadth.try_start_refresh", fake_start)
    res = client.post("/api/breadth/refresh")
    assert res.status_code == 202
    assert res.json()["state"] == "running"


def test_refresh_conflict_when_already_running(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.services.breadth.has_api_key", lambda: True)
    monkeypatch.setattr("app.services.breadth.try_start_refresh", lambda: False)
    res = client.post("/api/breadth/refresh")
    assert res.status_code == 409


def test_run_refresh_persists_days(db_session, monkeypatch):
    db, _testing_session = db_session
    d0 = date(2024, 6, 3)  # Monday
    d1 = date(2024, 6, 4)
    d2 = date(2024, 6, 5)

    def bars(close_a: float, close_b: float, volume: float) -> list[GroupedBar]:
        return [
            GroupedBar("AAA", 10, 10, 10, close_a, volume),
            GroupedBar("BBB", 10, 10, 10, close_b, volume),
            GroupedBar("ETF", 10, 10, 10, 10, volume),  # not in CS universe
        ]

    fake = FakeMassive(
        tickers=["AAA", "BBB"],
        bars_by_date={
            d0: bars(100.0, 100.0, 200_000),
            d1: bars(105.0, 96.0, 250_000),
            d2: bars(110.0, 90.0, 300_000),
        },
    )

    class FakeSettings:
        massive_api_key = "test-key"
        breadth_lookback_days = 3
        breadth_tickers = None

    monkeypatch.setattr("app.services.breadth.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("app.services.breadth.today_et", lambda: d2)

    run_refresh(db, fake)

    from app.db.models import BreadthDay, DailyBar, UniverseTicker

    assert set(db.scalars(select(UniverseTicker.symbol)).all()) == {"AAA", "BBB"}
    assert set(db.scalars(select(DailyBar.symbol)).all()) == {"AAA", "BBB"}
    days = list(db.scalars(select(BreadthDay).order_by(BreadthDay.session_date)).all())
    assert [row.session_date for row in days] == [d0, d1, d2]
    assert days[1].up_4 == 1
    assert days[1].down_4 == 1
    assert days[1].universe_size == 2
    assert days[2].ratio_5d is None  # only 3 sessions
    assert fake.grouped_calls == [d0, d1, d2]


def test_run_refresh_uses_configured_tickers_and_skips_massive_list(db_session, monkeypatch):
    """TEMPORARY (breadth testing): TF_BREADTH_TICKERS skips common_stock_tickers()."""
    db, _testing_session = db_session
    d0 = date(2024, 6, 3)
    d1 = date(2024, 6, 4)
    d2 = date(2024, 6, 5)

    class RaisingMassive(FakeMassive):
        def common_stock_tickers(self, on_page=None) -> list[str]:
            raise AssertionError("common_stock_tickers should not run when TF_BREADTH_TICKERS is set")

    fake = RaisingMassive(
        tickers=["SHOULD_NOT_USE"],
        bars_by_date={
            d0: [GroupedBar("AAA", 10, 10, 10, 100.0, 200_000)],
            d1: [GroupedBar("AAA", 10, 10, 10, 105.0, 250_000)],
            d2: [GroupedBar("AAA", 10, 10, 10, 110.0, 300_000)],
        },
    )

    class FakeSettings:
        massive_api_key = "test-key"
        breadth_lookback_days = 3
        breadth_tickers = "AAA"

    monkeypatch.setattr("app.services.breadth.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("app.services.breadth.today_et", lambda: d2)

    run_refresh(db, fake)

    from app.db.models import DailyBar, UniverseTicker

    assert set(db.scalars(select(UniverseTicker.symbol)).all()) == {"AAA"}
    assert set(db.scalars(select(DailyBar.symbol)).all()) == {"AAA"}


def test_run_refresh_computes_when_latest_session_unpublished(db_session, monkeypatch):
    db, _testing_session = db_session
    d0 = date(2024, 6, 3)
    d1 = date(2024, 6, 4)
    d2 = date(2024, 6, 5)

    fake = FakeMassive(
        tickers=["AAA"],
        bars_by_date={
            d0: [GroupedBar("AAA", 10, 10, 10, 100.0, 200_000)],
            d1: [GroupedBar("AAA", 10, 10, 10, 105.0, 250_000)],
            # d2 unpublished / 403 → empty
        },
    )

    class FakeSettings:
        massive_api_key = "test-key"
        breadth_lookback_days = 3
        breadth_tickers = None

    monkeypatch.setattr("app.services.breadth.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("app.services.breadth.today_et", lambda: d2)

    run_refresh(db, fake)

    from app.db.models import BreadthDay

    days = list(db.scalars(select(BreadthDay).order_by(BreadthDay.session_date)).all())
    assert [row.session_date for row in days] == [d0, d1]
    assert days[1].up_4 == 1


def test_run_refresh_errors_when_no_published_sessions(db_session, monkeypatch):
    db, _testing_session = db_session
    d2 = date(2024, 6, 5)

    fake = FakeMassive(tickers=["AAA"], bars_by_date={})

    class FakeSettings:
        massive_api_key = "test-key"
        breadth_lookback_days = 3
        breadth_tickers = None

    monkeypatch.setattr("app.services.breadth.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("app.services.breadth.today_et", lambda: d2)

    with pytest.raises(RuntimeError, match="No published EOD sessions"):
        run_refresh(db, fake)


def test_run_refresh_fetches_newer_sessions_when_lookback_already_full(db_session, monkeypatch):
    """A full lookback of old bars must still pick up weekdays after a gap."""
    db, _testing_session = db_session
    seeded = [
        date(2026, 9, 11),
        date(2026, 9, 14),
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 17),
    ]
    d18 = date(2026, 9, 18)
    d21 = date(2026, 9, 21)
    d22 = date(2026, 9, 22)

    from app.db.models import BreadthDay, DailyBar, UniverseTicker

    now = datetime.now(timezone.utc)
    db.add(UniverseTicker(symbol="AAA", refreshed_at=now))
    for i, session_date in enumerate(seeded):
        db.add(
            DailyBar(
                symbol="AAA",
                session_date=session_date,
                open=10,
                high=10,
                low=10,
                close=100.0 + i,
                volume=200_000,
            )
        )
    db.commit()

    fake = FakeMassive(
        tickers=["AAA"],
        bars_by_date={
            d18: [GroupedBar("AAA", 10, 10, 10, 110.0, 250_000)],
            d21: [GroupedBar("AAA", 10, 10, 10, 115.0, 300_000)],
            # d22 unpublished / 403 → empty
        },
    )

    class FakeSettings:
        massive_api_key = "test-key"
        breadth_lookback_days = 5
        breadth_tickers = "AAA"

    monkeypatch.setattr("app.services.breadth.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("app.services.breadth.today_et", lambda: d22)

    run_refresh(db, fake)

    days = list(db.scalars(select(BreadthDay).order_by(BreadthDay.session_date)).all())
    assert [row.session_date for row in days] == [
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 17),
        d18,
        d21,
    ]
    assert fake.grouped_calls == [d18, d21, d22]
