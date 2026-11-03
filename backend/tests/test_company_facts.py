"""EDGAR company facts parsing and caching — canned payloads, no network."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.services import company_facts, edgar
from app.services.company_facts import COVER_SHARES, CONVERTIBLES, WARRANTS
from app.services.edgar import FilingRecord

TODAY = date(2026, 10, 27)


def _fact(val, end, filed, accn="0000000001-26-000001", form="10-Q") -> dict:
    return {"val": val, "end": end, "filed": filed, "accn": accn, "form": form}


def _payload(dei=None, gaap=None) -> dict:
    def tags(spec, unit_for):
        return {tag: {"units": {unit_for(tag): rows}} for tag, rows in (spec or {}).items()}

    def gaap_unit(tag: str) -> str:
        return "USD/shares" if "Price" in tag else ("shares" if "Warrant" in tag else "USD")

    facts = {}
    if dei:
        facts["dei"] = tags(dei, lambda _: "shares")
    if gaap:
        facts["us-gaap"] = tags(gaap, gaap_unit)
    return {"cik": 1, "facts": facts}


def _parse(**kwargs) -> company_facts.CompanyFacts:
    return company_facts.parse(_payload(**kwargs), today=TODAY)


# --- picking values ------------------------------------------------------------------


def test_cover_shares_takes_latest_end_and_keeps_filed_date():
    facts = _parse(
        dei={
            "EntityCommonStockSharesOutstanding": [
                _fact(100, "2026-04-25", "2026-05-01", accn="a1"),
                _fact(110, "2026-07-25", "2026-08-01", accn="a2"),
            ]
        }
    )
    assert facts.ok and facts.found
    assert facts.cover_shares == company_facts.Fact(
        value=110, end=date(2026, 7, 25), filed=date(2026, 8, 1), accession="a2"
    )


def test_same_end_from_different_accessions_uses_latest_filed():
    facts = _parse(
        gaap={
            "ClassOfWarrantOrRightOutstanding": [
                _fact(5_000_000, "2025-12-31", "2026-03-01", accn="10k"),
                _fact(5_200_000, "2025-12-31", "2026-05-01", accn="10q-restated"),
            ]
        }
    )
    assert facts.warrants is not None
    assert facts.warrants.value == 5_200_000
    assert facts.warrants.accession == "10q-restated"


def test_facts_older_than_fifteen_months_are_ignored():
    facts = _parse(gaap={"ClassOfWarrantOrRightOutstanding": [_fact(11_500_000, "2021-09-30", "2021-11-15")]})
    assert facts.warrants is None
    assert WARRANTS not in facts.ambiguous


def test_same_accession_and_end_with_identical_values_is_used():
    rows = [_fact(250, "2026-08-03", "2026-08-06"), _fact(250, "2026-08-03", "2026-08-06")]
    facts = _parse(dei={"EntityCommonStockSharesOutstanding": rows})
    assert facts.cover_shares is not None and facts.cover_shares.value == 250


def test_same_accession_and_end_with_differing_values_is_ambiguous():
    # Per-class cover counts (e.g. Class A and Class B) in one filing.
    rows = [_fact(5_800_000_000, "2026-07-20", "2026-07-25"), _fact(860_000_000, "2026-07-20", "2026-07-25")]
    facts = _parse(dei={"EntityCommonStockSharesOutstanding": rows})
    assert facts.cover_shares is None
    assert COVER_SHARES in facts.ambiguous


def test_multi_class_company_without_cover_fact_has_none():
    facts = _parse(gaap={"ConvertibleDebt": [_fact(1e9, "2026-06-30", "2026-08-05")]})
    assert facts.found
    assert facts.cover_shares is None
    assert COVER_SHARES not in facts.ambiguous


# --- convertibles ------------------------------------------------------------------------


def test_convertible_total_tag_is_preferred_over_components_at_same_end():
    facts = _parse(
        gaap={
            "ConvertibleNotesPayable": [_fact(5_000_000, "2026-06-30", "2026-08-13")],
            "ConvertibleDebtCurrent": [_fact(718_000, "2026-06-30", "2026-08-13")],
            "ConvertibleDebtNoncurrent": [_fact(3_934_000, "2026-06-30", "2026-08-13")],
        }
    )
    assert facts.convertible_principal is not None
    assert facts.convertible_principal.value == 5_000_000  # never total + components


def test_convertible_components_are_summed_when_no_total():
    facts = _parse(
        gaap={
            "ConvertibleDebtCurrent": [_fact(490_000_000, "2026-03-31", "2026-05-08")],
            "ConvertibleDebtNoncurrent": [_fact(1_597_000_000, "2026-03-31", "2026-05-08")],
        }
    )
    assert facts.convertible_principal is not None
    assert facts.convertible_principal.value == 2_087_000_000


def test_fresher_convertible_measure_wins():
    facts = _parse(
        gaap={
            "ConvertibleDebt": [_fact(1_101_976_000, "2026-06-30", "2026-08-05")],
            "ConvertibleDebtCurrent": [_fact(490_354_000, "2026-03-31", "2026-05-08")],
            "ConvertibleDebtNoncurrent": [_fact(1_597_266_000, "2026-03-31", "2026-05-08")],
        }
    )
    assert facts.convertible_principal is not None
    assert facts.convertible_principal.end == date(2026, 6, 30)
    assert facts.convertible_principal.value == 1_101_976_000


def test_ambiguous_convertible_principal_is_flagged():
    rows = [_fact(100e6, "2026-06-30", "2026-08-05"), _fact(250e6, "2026-06-30", "2026-08-05")]
    facts = _parse(gaap={"ConvertibleDebt": rows})
    assert facts.convertible_principal is None
    assert CONVERTIBLES in facts.ambiguous


def test_conversion_price_uses_price_unit():
    facts = _parse(gaap={"DebtInstrumentConvertibleConversionPrice1": [_fact(12.5, "2026-06-30", "2026-08-05")]})
    assert facts.conversion_price is not None and facts.conversion_price.value == 12.5


def test_payload_without_facts_is_not_found():
    assert company_facts.parse({"cik": 1, "facts": {}}, today=TODAY) == company_facts.NO_FACTS


# --- fetching and caching --------------------------------------------------------------------


class _Session:
    def __init__(self, result):
        self.result = result
        self.gets: list[str] = []

    def get(self, url, *, headers=None):  # noqa: ARG002
        self.gets.append(url)
        request = httpx.Request("GET", url)
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, int):
            return httpx.Response(self.result, request=request)
        return httpx.Response(200, json=self.result, request=request)


def _records(*accessions_forms: tuple[str, str]) -> tuple[FilingRecord, ...]:
    return tuple(FilingRecord(form=f, filed=date(2026, 10, 1), accession=a) for a, f in accessions_forms)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(edgar, "_throttle", lambda: None)


def test_cache_key_is_latest_periodic_filing_else_newest():
    assert company_facts.cache_key(_records(("8k", "8-K"), ("q2", "10-Q"), ("k", "10-K"))) == "q2"
    assert company_facts.cache_key(_records(("s1", "S-1"), ("8k", "8-K"))) == "s1"  # recent IPO
    assert company_facts.cache_key(()) == ""


def test_fetches_once_per_periodic_filing():
    session = _Session(_payload(dei={"EntityCommonStockSharesOutstanding": [_fact(1, "2026-09-30", "2026-10-05")]}))
    records = _records(("8k", "8-K"), ("q2", "10-Q"))

    company_facts.get_company_facts(42, records, session=session)
    company_facts.get_company_facts(42, records, session=session)
    assert len(session.gets) == 1
    assert session.gets[0].endswith("/api/xbrl/companyfacts/CIK0000000042.json")

    company_facts.get_company_facts(42, _records(("q3", "10-Q")) + records, session=session)
    assert len(session.gets) == 2  # a new 10-Q refetches


def test_404_is_cached_as_no_facts():
    session = _Session(404)

    assert company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=session) == company_facts.NO_FACTS
    company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=session)
    assert len(session.gets) == 1


@pytest.mark.parametrize("result", [503, 429, 403, httpx.ReadTimeout("slow")], ids=["503", "429", "403", "timeout"])
def test_transient_failure_is_failed_and_not_cached(result):
    session = _Session(result)

    info = company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=session)
    assert info.ok is False
    company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=session)
    assert len(session.gets) == 2


# --- SQLite persistence ----------------------------------------------------------------------


def test_parsed_facts_persist_across_a_restart():
    end = date.today().isoformat()
    session = _Session(
        _payload(
            dei={"EntityCommonStockSharesOutstanding": [_fact(1_000, end, end)]},
            gaap={
                "ClassOfWarrantOrRightOutstanding": [_fact(5, end, end), _fact(6, end, end)],  # ambiguous
                "ConvertibleDebt": [_fact(2_500, end, end)],
            },
        )
    )
    first = company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=session)

    company_facts.clear_cache()  # a restart drops memory, not SQLite
    unused = _Session(503)
    second = company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=unused)

    assert unused.gets == []
    assert second == first
    assert second.cover_shares is not None and second.cover_shares.value == 1_000
    assert WARRANTS in second.ambiguous


def test_no_facts_persists_but_failures_do_not():
    company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=_Session(404))
    company_facts.get_company_facts(43, _records(("q2", "10-Q")), session=_Session(503))
    company_facts.clear_cache()

    unused = _Session(_payload())
    assert company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=unused) == company_facts.NO_FACTS
    assert unused.gets == []
    company_facts.get_company_facts(43, _records(("q2", "10-Q")), session=unused)
    assert len(unused.gets) == 1


def test_a_new_periodic_filing_misses_the_store():
    session = _Session(_payload(dei={"EntityCommonStockSharesOutstanding": [_fact(1, "2026-09-30", "2026-10-05")]}))
    company_facts.get_company_facts(42, _records(("q2", "10-Q")), session=session)
    company_facts.clear_cache()

    company_facts.get_company_facts(42, _records(("q3", "10-Q"), ("q2", "10-Q")), session=session)
    assert len(session.gets) == 2
