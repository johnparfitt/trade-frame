"""SEC filing significance scorer — fake LLM + fake EDGAR session, no network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services import sec_significance
from app.services.edgar import FilingRef


@pytest.fixture(autouse=True)
def _clear_cache():
    sec_significance.clear_cache()
    yield
    sec_significance.clear_cache()


def _recent(days_ago: int = 0) -> str:
    """ISO filing date ``days_ago`` days before today (inside the recency window)."""
    return (date.today() - timedelta(days=days_ago)).isoformat()


class _Resp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


class _DocSession:
    def __init__(self, docs: dict[str, str]):
        self._docs = docs
        self.gets: list[str] = []

    def get(self, url: str, *, headers=None) -> _Resp:  # noqa: ARG002
        self.gets.append(url)
        return _Resp(self._docs.get(url, ""))


class _FakeLLM:
    """LLM client stub: returns whatever ``fn(prompt)`` yields; records calls."""

    def __init__(self, fn):
        self._fn = fn
        self.calls: list[str] = []

    def analyze(self, prompt: str, model: str) -> str:  # noqa: ARG002
        self.calls.append(prompt)
        return self._fn(prompt)


class _FakeSettings:
    def __init__(self, *, enabled=True, minimum=50, recent_days=3650):
        self.sec_significance_enabled = enabled
        self.sec_significance_min = minimum
        self.sec_significance_recent_days = recent_days


def _ref(form, d, accession, *, doc, items=()):
    return FilingRef(
        form=form,
        date=d,
        url=f"https://www.sec.gov/{accession}-index.htm",
        accession=accession,
        primary_doc="x.htm",
        doc_url=doc,
        items=items,
    )


# --- heuristic --------------------------------------------------------------


def test_heuristic_form_and_item_mapping():
    assert sec_significance._heuristic("8-K", ["2.02"]) == (25, "neutral", "results of operations")
    assert sec_significance._heuristic("8-K", ["3.02"]) == (70, "bearish", "unregistered equity sale (dilution)")
    assert sec_significance._heuristic("8-K", []) == (35, "neutral", "current report (8-K)")
    assert sec_significance._heuristic("10-Q", [])[0] == 30
    assert sec_significance._heuristic("424B5", []) == (70, "bearish", "prospectus / offering (supply)")
    assert sec_significance._heuristic("ZZZ", []) == sec_significance._DEFAULT_HEURISTIC


# --- evaluate ---------------------------------------------------------------


def test_evaluate_returns_highest_recent_above_threshold():
    cand = _ref("8-K", _recent(1), "acc1", doc="http://doc1")
    session = _DocSession({"http://doc1": "news about a large offering"})
    llm = _FakeLLM(lambda p: '{"score": 80, "direction": "bearish", "reason": "Priced $150M offering"}')

    res = sec_significance.evaluate("T", [cand], session=session, llm_client=llm)

    assert res.score == 80
    assert res.direction == "bearish"
    assert res.url == cand.url
    assert res.form == "8-K"
    assert res.note.startswith("Significance 80/100 (bearish)")
    assert "Priced $150M offering" in res.note


def test_evaluate_skips_worthless_filing_above_a_real_one():
    junk = _ref("8-K", _recent(1), "accJunk", doc="http://junk")
    real = _ref("424B5", _recent(2), "accReal", doc="http://real")
    session = _DocSession({"http://junk": "JUNK routine", "http://real": "REAL offering"})
    llm = _FakeLLM(lambda p: '{"score": 20, "reason": "routine"}' if "JUNK" in p else '{"score": 90, "direction": "bearish", "reason": "offering"}')

    res = sec_significance.evaluate("T", [junk, real], session=session, llm_client=llm)

    assert res.score == 90
    assert res.url == real.url
    assert len(llm.calls) == 2  # had to score the junk one before reaching the real one


def test_evaluate_blank_when_nothing_clears_threshold():
    a = _ref("10-Q", _recent(1), "a1", doc="http://a1")
    b = _ref("8-K", _recent(2), "a2", doc="http://a2")
    session = _DocSession({"http://a1": "routine", "http://a2": "routine"})
    llm = _FakeLLM(lambda p: '{"score": 30, "reason": "routine"}')

    res = sec_significance.evaluate("T", [a, b], session=session, llm_client=llm)

    assert res is sec_significance.EMPTY
    assert res.score is None


def test_evaluate_stops_at_first_qualifier():
    first = _ref("424B5", _recent(1), "f1", doc="http://f1")
    second = _ref("8-K", _recent(2), "f2", doc="http://f2")
    session = _DocSession({"http://f1": "offering", "http://f2": "more"})
    llm = _FakeLLM(lambda p: '{"score": 75, "direction": "bearish", "reason": "offering"}')

    res = sec_significance.evaluate("T", [first, second], session=session, llm_client=llm)

    assert res.score == 75
    assert len(llm.calls) == 1  # the second (older) candidate is never scored
    assert "http://f2" not in session.gets


def test_evaluate_caches_by_accession():
    cand = _ref("424B5", _recent(1), "acc1", doc="http://doc1")
    session = _DocSession({"http://doc1": "offering"})
    llm = _FakeLLM(lambda p: '{"score": 70, "direction": "bearish", "reason": "offering"}')

    sec_significance.evaluate("T", [cand], session=session, llm_client=llm)
    sec_significance.evaluate("T", [cand], session=session, llm_client=llm)

    assert len(llm.calls) == 1  # second lookup served from the accession cache
    assert len(session.gets) == 1


def test_evaluate_empty_candidates_is_blank():
    assert sec_significance.evaluate("T", []) is sec_significance.EMPTY


# --- recency gate -----------------------------------------------------------


def test_evaluate_ignores_filings_older_than_window():
    # Older than the default 14-day window: must be blank and never scored.
    cand = _ref("424B5", _recent(40), "accOld", doc="http://old")
    session = _DocSession({"http://old": "offering"})
    llm = _FakeLLM(lambda p: '{"score": 95, "direction": "bearish", "reason": "offering"}')

    res = sec_significance.evaluate("T", [cand], session=session, llm_client=llm)

    assert res is sec_significance.EMPTY
    assert llm.calls == []  # stale filing is never handed to the LLM
    assert session.gets == []


def test_evaluate_stops_scan_at_first_stale_candidate():
    # Newest-first: once a stale one is hit, the rest (older) are skipped too.
    fresh_junk = _ref("8-K", _recent(1), "fresh", doc="http://fresh")
    stale_real = _ref("424B5", _recent(30), "stale", doc="http://stale")
    session = _DocSession({"http://fresh": "routine", "http://stale": "offering"})
    llm = _FakeLLM(lambda p: '{"score": 20, "reason": "routine"}' if "routine" in p else '{"score": 95, "direction": "bearish", "reason": "offering"}')

    res = sec_significance.evaluate("T", [fresh_junk, stale_real], session=session, llm_client=llm)

    assert res is sec_significance.EMPTY
    assert len(llm.calls) == 1  # only the fresh junk filing was scored
    assert "http://stale" not in session.gets


# --- direction --------------------------------------------------------------


def test_direction_defaults_to_neutral_when_model_omits_it():
    cand = _ref("8-K", _recent(1), "acc1", doc="http://doc1")
    session = _DocSession({"http://doc1": "big news"})
    llm = _FakeLLM(lambda p: '{"score": 80, "reason": "big"}')

    res = sec_significance.evaluate("T", [cand], session=session, llm_client=llm)

    assert res.direction == "neutral"
    assert "(neutral)" in res.note


def test_bullish_direction_propagates():
    cand = _ref("8-K", _recent(1), "acc1", doc="http://doc1")
    session = _DocSession({"http://doc1": "buyback"})
    llm = _FakeLLM(lambda p: '{"score": 75, "direction": "bullish", "reason": "$2B buyback"}')

    res = sec_significance.evaluate("T", [cand], session=session, llm_client=llm)

    assert res.direction == "bullish"


# --- fallbacks --------------------------------------------------------------


def test_malformed_llm_output_falls_back_to_heuristic():
    cand = _ref("424B5", _recent(1), "acc1", doc="http://doc1")
    session = _DocSession({"http://doc1": "offering text"})
    llm = _FakeLLM(lambda p: "sorry, I cannot comply")

    res = sec_significance.evaluate("T", [cand], session=session, llm_client=llm)

    assert res.score == 70  # 424B heuristic
    assert res.direction == "bearish"
    assert "supply" in res.note


def test_missing_key_falls_back_to_heuristic():
    cand = _ref("424B5", _recent(1), "acc1", doc="http://doc1")
    session = _DocSession({"http://doc1": "offering text"})

    # llm_client=None + no configured key -> llm.run raises -> heuristic.
    res = sec_significance.evaluate("T", [cand], session=session, llm_client=None)

    assert res.score == 70


def test_disabled_uses_heuristic_without_fetching(monkeypatch):
    monkeypatch.setattr(sec_significance, "get_settings", lambda: _FakeSettings(enabled=False))
    cand = _ref("424B5", _recent(1), "acc1", doc="http://doc1")

    called = _FakeLLM(lambda p: '{"score": 99, "reason": "x"}')
    res = sec_significance.evaluate("T", [cand], session=None, llm_client=called)

    assert res.score == 70  # heuristic, not the LLM's 99
    assert called.calls == []


def test_earnings_item_stays_below_threshold(monkeypatch):
    # Earnings (8-K 2.02) must never surface: heuristic keeps it well under 50.
    monkeypatch.setattr(sec_significance, "get_settings", lambda: _FakeSettings(enabled=False))
    cand = _ref("8-K", _recent(1), "accEarn", doc="http://earn", items=("2.02", "9.01"))

    res = sec_significance.evaluate("T", [cand], session=None, llm_client=None)

    assert res is sec_significance.EMPTY


def test_parse_llm_json_handles_fences_and_direction():
    assert sec_significance._parse_llm_json(
        '```json\n{"score": 72, "direction": "bearish", "reason": "x"}\n```'
    ) == (72, "bearish", "x")
    # Missing/invalid direction normalizes to neutral.
    assert sec_significance._parse_llm_json('{"score": 72, "reason": "x"}') == (72, "neutral", "x")
    assert sec_significance._parse_llm_json('{"score": 72, "direction": "sideways", "reason": "x"}') == (72, "neutral", "x")
    assert sec_significance._parse_llm_json("no json here") is None
