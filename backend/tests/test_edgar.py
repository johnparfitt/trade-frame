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
            doc = self._documents.get(url, "")
            if isinstance(doc, Exception):
                return _FakeResponse(error=doc)
            return _FakeResponse(text=doc)
        raise AssertionError(f"unexpected URL: {url}")


def _status_error(url: str, status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request)
    )


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


def test_get_filings_builds_records_and_candidates():
    today = date.today()
    session = _FakeSession(
        tickers=_tickers("TEST", 320193),
        submissions=_submissions(
            [
                _row("4", today - timedelta(days=1), "noise"),  # not significant
                _row(
                    "8-K",
                    today - timedelta(days=5),
                    "0001193125-25-000001",
                    primary_doc="a.htm",
                    items="3.02,9.01",
                ),
                _row("424B5", today - timedelta(days=20), "0001193125-25-000002"),
                _row("424B5", today - timedelta(days=200), "0001193125-25-000003"),
                _row("424B5", today - timedelta(days=400), "0001193125-25-000004"),
                _row("S-3", today - timedelta(days=100), "0001193125-25-000005"),
                _row("S-1", today - timedelta(days=60), "0001193125-25-000006"),
                _row("D", today - timedelta(days=15), "0001193125-25-000007"),
                _row("S-3", today - timedelta(days=4 * 365), "0001193125-21-000008"),  # too old
            ]
        ),
    )

    info = edgar.get_filings("TEST", session=session)

    assert info.ok is True and info.found is True
    assert info.cik == 320193

    # Records: every form, newest-first, dated, older than the longest window dropped.
    assert [r.form for r in info.records] == ["4", "8-K", "D", "424B5", "S-1", "S-3", "424B5", "424B5"]
    eight_k = info.records[1]
    assert eight_k.filed == today - timedelta(days=5)
    assert eight_k.items == ("3.02", "9.01")
    assert eight_k.doc_url.endswith("000119312525000001/a.htm")
    assert info.records[2].doc_url == ""  # no primary document

    # Candidates: newest-first, Form 4 skipped, capped at _MAX_CANDIDATES.
    assert len(info.candidates) == edgar._MAX_CANDIDATES
    assert info.candidates[0].form == "8-K"
    assert info.candidates[0].items == ("3.02", "9.01")
    assert info.candidates[0].accession == "0001193125-25-000001"
    assert info.candidates[0].doc_url.endswith("000119312525000001/a.htm")
    assert info.candidates[0].url.endswith("0001193125-25-000001-index.htm")
    assert all(edgar._is_significant(c.form) for c in info.candidates)


def test_get_filings_no_cik_is_empty_not_failed():
    session = _FakeSession(tickers=_tickers("OTHER", 111))
    info = edgar.get_filings("TEST", session=session)
    assert info == edgar.EMPTY_FILINGS
    assert info.ok is True and info.found is False


def test_get_filings_outage_is_failed_and_not_cached():
    class _Boom:
        def get(self, url, *, headers=None):  # noqa: ARG002
            raise httpx.HTTPError("edgar down")

    info = edgar.get_filings("TEST", session=_Boom())
    assert info == edgar.FAILED_FILINGS
    assert info.ok is False
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


_DOC_URL = "https://www.sec.gov/Archives/edgar/data/320193/000/x.htm"


def test_fetch_document_text_strips_html():
    html = "<html><body><p>Hello   <b>World</b></p>\n\n extra</body></html>"
    doc = edgar.fetch_document_text(_DOC_URL, session=_FakeSession(documents={_DOC_URL: html}))
    assert doc.failure is None
    assert doc.text == "Hello World extra"


def test_fetch_document_text_drops_hidden_blocks_and_decodes_entities():
    html = (
        "<html><head><style>p { color: red; }</style>"
        "<script type='text/javascript'>var x = 1;</script></head><body>"
        "<div style='display:none'><ix:header><ix:hidden>dei:EntityCentralIndexKey 0000320193"
        "</ix:hidden></ix:header></div>"
        "<p>At&#8209;the&nbsp;market offering &amp; sales agreement</p></body></html>"
    )
    doc = edgar.fetch_document_text(_DOC_URL, session=_FakeSession(documents={_DOC_URL: html}))
    assert "color" not in doc.text
    assert "var x" not in doc.text
    assert "EntityCentralIndexKey" not in doc.text
    assert doc.text == "At\u2011the market offering & sales agreement"


def test_fetch_document_text_truncates_to_max_chars():
    session = _FakeSession(documents={_DOC_URL: "a" * 100})
    assert len(edgar.fetch_document_text(_DOC_URL, session=session, max_chars=10).text) == 10
    assert len(edgar.fetch_document_text(_DOC_URL, session=session).text) == 100


def test_fetch_document_text_shares_recent_fetches():
    session = _FakeSession(documents={_DOC_URL: "<p>" + "a" * 60_000 + "</p>"})

    edgar.fetch_document_text(_DOC_URL, session=session)  # 50k default
    cover = edgar.fetch_document_text(_DOC_URL, session=session, max_chars=10_000)
    assert len(cover.text) == 10_000
    assert len(session.calls) == 1  # a shorter read is served from the cache

    full = edgar.fetch_document_text(_DOC_URL, session=session, max_chars=1_000_000)
    assert len(full.text) == 60_000
    assert len(session.calls) == 2  # a longer read refetches


def test_fetch_document_text_failures_are_not_cached():
    session = _FakeSession(documents={_DOC_URL: _status_error(_DOC_URL, 503)})
    edgar.fetch_document_text(_DOC_URL, session=session)
    edgar.fetch_document_text(_DOC_URL, session=session)
    assert len(session.calls) == 2


def test_fetch_document_text_without_url_is_permanent():
    doc = edgar.fetch_document_text("", session=_FakeSession())
    assert doc.text == ""
    assert doc.failure == edgar.PERMANENT


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_fetch_document_text_retryable_status_is_transient(status):
    session = _FakeSession(documents={_DOC_URL: _status_error(_DOC_URL, status)})
    doc = edgar.fetch_document_text(_DOC_URL, session=session)
    assert doc.text == ""
    assert doc.failure == edgar.TRANSIENT


@pytest.mark.parametrize("status", [400, 404, 410])
def test_fetch_document_text_missing_document_is_permanent(status):
    session = _FakeSession(documents={_DOC_URL: _status_error(_DOC_URL, status)})
    assert edgar.fetch_document_text(_DOC_URL, session=session).failure == edgar.PERMANENT


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectError("no route")], ids=["timeout", "network"]
)
def test_fetch_document_text_network_errors_are_transient(error):
    session = _FakeSession(documents={_DOC_URL: error})
    assert edgar.fetch_document_text(_DOC_URL, session=session).failure == edgar.TRANSIENT
