"""Watchlist analyst-net scoring — fake UW client, no live network."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.services import analysts

TODAY = date(2026, 11, 6)


class _FakeClient:
    def __init__(self, rows=None, error: Exception | None = None):
        self._rows = rows if rows is not None else []
        self._error = error
        self.calls: list[tuple[str, str, int]] = []

    def analyst_ratings(self, ticker: str, *, newer_than: str, limit: int = 500):
        self.calls.append((ticker, newer_than, limit))
        if self._error is not None:
            raise self._error
        return self._rows


def _row(action: str, name: str, timestamp: str) -> dict:
    return {"action": action, "analyst_name": name, "timestamp": timestamp, "ticker": "NVDA"}


@pytest.fixture(autouse=True)
def _clear_cache():
    analysts.clear_cache()
    yield
    analysts.clear_cache()


def test_allowlist_is_the_named_analysts():
    assert len(analysts.ANALYST_NAMES) == 85
    assert len(set(analysts.ANALYST_NAMES)) == 85


def test_net_counts_each_upgrade_and_downgrade():
    rows = [
        _row("upgraded", "Dan Ives", "2026-11-01T12:00:00Z"),
        _row("upgraded", "Dan Ives", "2026-10-15T12:00:00Z"),
        _row("downgraded", "Adam Jonas", "2026-10-02T12:00:00Z"),
    ]

    score = analysts.score_ratings("nvda", rows, today=TODAY)

    assert score.net == 1
    assert score.upgrades == (
        "NVDA|dan ives|2026-11-01T12:00:00Z",
        "NVDA|dan ives|2026-10-15T12:00:00Z",
    )


def test_ignores_other_actions_and_off_list_names():
    rows = [
        _row("initiated", "Dan Ives", "2026-11-01T12:00:00Z"),
        _row("reiterated", "Dan Ives", "2026-11-01T12:00:00Z"),
        _row("maintained", "Dan Ives", "2026-10-20T12:00:00Z"),
        _row("target raised", "Dan Ives", "2026-10-20T12:00:00Z"),
        _row("target lowered", "Dan Ives", "2026-10-20T12:00:00Z"),
        _row("upgraded", "Somebody Else", "2026-11-01T12:00:00Z"),
        _row("downgraded", "Dan Ives", "2026-10-01T12:00:00Z"),
    ]

    score = analysts.score_ratings("NVDA", rows, today=TODAY)

    assert score.net == -1
    assert score.upgrades == ()


def test_drops_a_rating_older_than_60_days():
    rows = [
        _row("upgraded", "Dan Ives", "2026-09-06T23:59:59Z"),
        _row("upgraded", "Adam Jonas", "2026-09-07T00:00:00Z"),
    ]

    score = analysts.score_ratings("NVDA", rows, today=TODAY)

    assert score.net == 1
    assert score.upgrades == ("NVDA|adam jonas|2026-09-07T00:00:00Z",)


def test_apostrophe_and_spacing_still_match():
    rows = [
        _row("upgraded", "  joe o\u2019dea ", "2026-11-01T12:00:00Z"),
        _row("downgraded", "c.j. muse", "2026-10-20T00:00:00Z"),
    ]

    score = analysts.score_ratings("NVDA", rows, today=TODAY)

    assert score.net == 0
    assert score.upgrades == ("NVDA|joe o'dea|2026-11-01T12:00:00Z",)


def test_successful_lookup_with_nothing_in_the_window_is_zero():
    assert analysts.score_ratings("NVDA", [], today=TODAY).net == 0


def test_failed_call_leaves_the_score_empty_and_is_not_cached():
    client = _FakeClient(error=httpx.HTTPError("boom"))

    first = analysts.get_score("NVDA", client=client, today=TODAY)
    second = analysts.get_score("NVDA", client=client, today=TODAY)

    assert first == analysts.EMPTY_SCORE
    assert second.net is None
    assert client.calls == [("NVDA", "2026-09-07", 500), ("NVDA", "2026-09-07", 500)]


def test_missing_key_leaves_the_score_empty(monkeypatch):
    monkeypatch.setattr(analysts, "has_uw_key", lambda: False)

    assert analysts.get_score("NVDA").net is None


def test_second_lookup_is_served_from_cache():
    client = _FakeClient(rows=[_row("upgraded", "Dan Ives", "2026-11-01T12:00:00Z")])

    assert analysts.get_score("NVDA", client=client, today=TODAY).net == 1
    assert analysts.get_score("nvda", client=client, today=TODAY).net == 1
    assert client.calls == [("NVDA", "2026-09-07", 500)]
