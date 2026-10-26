"""SEC EDGAR filing lookups — fake HTTP session, no live network."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from app.services import edgar


@pytest.fixture(autouse=True)
def _clear_cache():
    edgar.clear_cache()
    yield
    edgar.clear_cache()


class _FakeResponse:
    def __init__(self, payload: dict | None = None, text: str = "", error: Exception | None = None):
        self._payload = payload if payload is not None else {}
        self.text = text
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    """Routes EDGAR URLs to canned payloads by substring; records calls."""

    def __init__(self, *, tickers=None, submissions=None, documents=None):
        self._tickers = tickers if tickers is not None else {}
        self._submissions = submissions if submissions is not None else {}
        self._documents = documents if documents is not None else {}
        self.calls: list[str] = []

    def get(self, url: str, *, headers=None) -> _FakeResponse:  # noqa: ARG002
        self.calls.append(url)
        if "company_tickers" in url:
            return _FakeResponse(self._tickers)
        if "submissions" in url:
            return _FakeResponse(self._submissions)
        if "/Archives/" in url:
            return _FakeResponse(text=self._documents.get(url, ""))
        raise AssertionError(f"unexpected URL: {url}")


def _tickers(symbol: str, cik: int) -> dict:
    return {"0": {"cik_str": cik, "ticker": symbol, "title": f"{symbol} Inc."}}


def _submissions(rows: list[dict]) -> dict:
    return {
        "filings": {
            "recent": {
                "form": [r["form"] for r in rows],
                "filingDate": [r["date"].isoformat() for r in rows],
                "accessionNumber": [r["accession"] for r in rows],
                "primaryDocument": [r.get("primary_doc", "") for r in rows],
                "items": [r.get("items", "") for r in rows],
            }
        }
    }


def _row(form, d, accession, *, primary_doc="", items=""):
    return {"form": form, "date": d, "accession": accession, "primary_doc": primary_doc, "items": items}


# --- pure helpers -----------------------------------------------------------


def test_is_significant_includes_new_forms_and_form_d():
    for form in ("PRE 14A", "DEF 14C", "PRE 14C", "D", "D/A", "424B5", "8-K"):
        assert edgar._is_significant(form) is True
    # The Form D rule must NOT swallow DEF 14A / DEFA14A.
    assert edgar._is_significant("DEF 14A") is True  # still significant, via the set
    assert edgar._is_significant("DEFA14A") is False
    assert edgar._is_significant("4") is False


def test_parse_recent_filings_carries_primary_doc_and_items():
    parsed = edgar._parse_recent_filings(
        _submissions(
            [_row("8-K", date(2025, 12, 15), "acc1", primary_doc="d8k.htm", items="2.02,9.01")]
        )
    )
    assert parsed[0]["primary_doc"] == "d8k.htm"
    assert parsed[0]["items"] == ("2.02", "9.01")


# --- get_filings end to end -------------------------------------------------


def test_get_filings_builds_candidates_and_signals():
    today = date.today()
    session = _FakeSession(
        tickers=_tickers("TEST", 320193),
        submissions=_submissions(
            [
                _row("4", today - timedelta(days=1), "noise"),  # not significant
                _row("8-K", today - timedelta(days=5), "0001193125-25-000001", primary_doc="a.htm"),
                _row("424B5", today - timedelta(days=20), "0001193125-25-000002"),
                _row("424B5", today - timedelta(days=200), "0001193125-25-000003"),
                _row("424B5", today - timedelta(days=400), "0001193125-25-000004"),
                _row("S-3", today - timedelta(days=100), "0001193125-25-000005"),
                _row("S-1", today - timedelta(days=60), "0001193125-25-000006"),
                _row("D", today - timedelta(days=15), "0001193125-25-000007"),
            ]
        ),
    )

    info = edgar.get_filings("TEST", session=session)

    # Signals
    assert info.shelf_active is True
    assert info.days_since_424b == 20
    assert info.count_424b_24mo == 3
    assert info.recent_s1 is True
    assert info.days_since_form_d == 15

    # Candidates: newest-first, Form 4 skipped, capped at _MAX_CANDIDATES.
    assert len(info.candidates) == edgar._MAX_CANDIDATES
    assert info.candidates[0].form == "8-K"
    assert info.candidates[0].items == ()
    assert info.candidates[0].accession == "0001193125-25-000001"
    assert info.candidates[0].doc_url.endswith("000119312525000001/a.htm")
    assert info.candidates[0].url.endswith("0001193125-25-000001-index.htm")
    assert all(edgar._is_significant(c.form) for c in info.candidates)


def test_get_filings_no_cik_is_empty():
    session = _FakeSession(tickers=_tickers("OTHER", 111))
    assert edgar.get_filings("TEST", session=session) == edgar.EMPTY_FILINGS


def test_get_filings_outage_returns_empty_and_is_not_cached():
    class _Boom:
        def get(self, url, *, headers=None):  # noqa: ARG002
            raise httpx.HTTPError("edgar down")

    assert edgar.get_filings("TEST", session=_Boom()) == edgar.EMPTY_FILINGS
    good = _FakeSession(
        tickers=_tickers("TEST", 320193),
        submissions=_submissions([_row("10-K", date(2025, 1, 1), "acc")]),
    )
    assert edgar.get_filings("TEST", session=good).candidates[0].form == "10-K"


def test_second_lookup_is_served_from_cache():
    session = _FakeSession(
        tickers=_tickers("TEST", 320193),
        submissions=_submissions([_row("10-K", date(2025, 1, 1), "acc")]),
    )
    edgar.get_filings("TEST", session=session)
    edgar.get_filings("test", session=session)
    submission_calls = [c for c in session.calls if "submissions" in c]
    assert len(submission_calls) == 1


# --- fetch_document_text ----------------------------------------------------


def test_fetch_document_text_strips_html_and_truncates():
    url = "https://www.sec.gov/Archives/edgar/data/320193/000/x.htm"
    html = "<html><body><p>Hello   <b>World</b></p>\n\n extra</body></html>"
    session = _FakeSession(documents={url: html})
    text = edgar.fetch_document_text(url, session=session)
    assert "Hello World extra" in text
    assert "<" not in text


def test_fetch_document_text_empty_url_returns_blank():
    assert edgar.fetch_document_text("", session=_FakeSession()) == ""
