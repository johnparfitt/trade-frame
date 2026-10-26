"""Blended dilution score — fake UW client + stubbed EDGAR, no live network."""

from __future__ import annotations

import pytest

from app.services import dilution, edgar, gex, sec_significance
from app.services.dilution import FinSignals
from app.services.edgar import FilingInfo
from app.services.sec_significance import SigResult


@pytest.fixture(autouse=True)
def _clear_cache():
    dilution.clear_cache()
    edgar.clear_cache()
    yield
    dilution.clear_cache()
    edgar.clear_cache()


def _bs(fiscal, *, shares=None, total_assets=None, cash=None) -> dict:
    row = {"fiscal_date_ending": fiscal}
    if shares is not None:
        row["common_stock_shares_outstanding"] = str(shares)
    if total_assets is not None:
        row["total_assets"] = str(total_assets)
    if cash is not None:
        row["cash_and_short_term_investments"] = str(cash)
    return row


def _cf(fiscal, *, issuance=0, sbc=0, repurchase=0, op=0) -> dict:
    return {
        "fiscal_date_ending": fiscal,
        "proceeds_from_issuance_of_common_stock": str(issuance),
        "stock_based_compensation": str(sbc),
        "payments_for_repurchase_of_common_stock": str(repurchase),
        "operating_cashflow": str(op),
    }


class _FakeUW:
    def __init__(self, data: dict):
        self._data = data
        self.calls: list[str] = []

    def financials(self, ticker: str, *, report_type: str = "quarterly") -> dict:
        self.calls.append(ticker)
        return self._data


# --- financial_signals parsing ----------------------------------------------


def test_share_growth_year_over_year():
    data = {
        "balance_sheets": [
            _bs("2025-09-30T00:00:00.000Z", shares=120, total_assets=1000),
            _bs("2024-09-30T00:00:00.000Z", shares=100, total_assets=900),
        ]
    }
    fin = dilution.financial_signals(data)
    assert fin.share_growth_pct == pytest.approx(20.0)


def test_sbc_ratio_is_ttm_over_total_assets():
    data = {
        "balance_sheets": [_bs("2025-09-30", total_assets=1000)],
        "cash_flows": [_cf(f"2025-{m:02d}-30", sbc=10) for m in (9, 6, 3, 1)],
    }
    fin = dilution.financial_signals(data)
    # TTM SBC 40 / 1000 assets
    assert fin.sbc_ratio == pytest.approx(0.04)


def test_buybacks_show_up_as_negative_share_growth():
    data = {
        "balance_sheets": [
            _bs("2025-09-30T00:00:00.000Z", shares=96, total_assets=1000),
            _bs("2024-09-30T00:00:00.000Z", shares=100, total_assets=1000),
        ]
    }
    fin = dilution.financial_signals(data)
    assert fin.share_growth_pct == pytest.approx(-4.0)


def test_annual_rows_are_ignored_when_quarterly_present():
    data = {
        "balance_sheets": [
            {**_bs("2025-09-30", shares=120, total_assets=1000), "report_type": "quarterly"},
            {**_bs("2024-09-30", shares=100, total_assets=1000), "report_type": "quarterly"},
            {**_bs("2025-09-30", shares=999, total_assets=9999), "report_type": "annual"},
        ]
    }
    fin = dilution.financial_signals(data)
    assert fin.share_growth_pct == pytest.approx(20.0)


def test_runway_only_when_burning_cash():
    burning = {
        "balance_sheets": [_bs("2025-09-30", cash=50)],
        "cash_flows": [_cf(f"2025-{m:02d}-30", op=-25) for m in (9, 6, 3, 1)],
    }
    assert dilution.financial_signals(burning).runway_years == pytest.approx(0.5)

    profitable = {
        "balance_sheets": [_bs("2025-09-30", cash=50)],
        "cash_flows": [_cf(f"2025-{m:02d}-30", op=25) for m in (9, 6, 3, 1)],
    }
    assert dilution.financial_signals(profitable).runway_years is None


def test_empty_financials_has_no_data():
    assert dilution.financial_signals({}) == dilution.EMPTY_FIN
    assert dilution.financial_signals({}).has_data is False


# --- scoring -----------------------------------------------------------------


def _breakdown_points(note: str) -> list[float]:
    """Parse the '+N label' lines out of a breakdown note."""
    return [
        float(line[1:].split(" ", 1)[0])
        for line in note.splitlines()
        if line.startswith("+")
    ]


def test_score_buyback_blue_chip_is_low_despite_shelf():
    filings = FilingInfo(shelf_active=True)
    # Shrinking share count (buybacks), even with heavy SBC.
    fin = FinSignals(share_growth_pct=-2.0, sbc_ratio=0.04, has_data=True)
    risk, note = dilution.score(filings, fin)
    assert risk == 5.0  # shelf baseline only; SBC gated off, no issuing bonus
    assert note.startswith("Dilution Risk 5/100")
    assert "S-3 shelf (unused capacity)" in note
    assert "- Net buybacks (shares -2% YoY)" in note


def test_score_shelf_bonus_only_when_issuing():
    filings = FilingInfo(shelf_active=True)
    issuing = FinSignals(share_growth_pct=10.0, has_data=True)
    risk, note = dilution.score(filings, issuing)
    # growth 10/20*45 = 22.5; shelf 5 + issuing bonus 10 = 37.5
    assert risk == 37.5
    assert "+15 Active S-3 shelf (issuing)" in note


def test_score_serial_diluter_saturates():
    filings = FilingInfo(
        shelf_active=True, days_since_424b=20, count_424b_24mo=3, recent_s1=True
    )
    fin = FinSignals(
        share_growth_pct=30.0, sbc_ratio=0.05, runway_years=0.75, has_data=True
    )
    risk, note = dilution.score(filings, fin)
    assert risk == 100.0
    assert "424B 20d ago" in note
    assert "offerings/24mo" in note
    assert "cash runway" in note
    # Raw contributions exceed the cap.
    assert sum(_breakdown_points(note)) >= 100.0


def test_score_filings_only_flags_financials_missing():
    filings = FilingInfo(shelf_active=True, days_since_424b=10)
    risk, note = dilution.score(filings, dilution.EMPTY_FIN)
    # shelf base 5 (not issuing) + recent 424B 15 = 20
    assert risk == 20.0
    assert "- Financials unavailable" in note


def test_score_note_is_a_points_breakdown():
    filings = FilingInfo(shelf_active=True, days_since_424b=30)
    # Inputs chosen to yield whole-number points: growth 8/20*45 = 18, SBC 10,
    # shelf (issuing) 15, 424B 15 -> 58 total.
    fin = FinSignals(share_growth_pct=8.0, sbc_ratio=0.04, has_data=True)
    risk, note = dilution.score(filings, fin)
    assert risk == 58.0
    assert note.startswith(f"Dilution Risk {risk:.0f}/100")
    # The +N lines add up to the headline total.
    assert sum(_breakdown_points(note)) == risk


def test_score_no_signals():
    risk, note = dilution.score(FilingInfo(), dilution.EMPTY_FIN)
    assert risk == 0.0
    assert note == "Dilution Risk 0/100\n- Financials unavailable"


def test_score_recent_form_d_contributes():
    filings = FilingInfo(days_since_form_d=20)
    risk, note = dilution.score(filings, dilution.EMPTY_FIN)
    assert risk == 10.0  # _FORM_D_POINTS
    assert "Form D 20d ago" in note


def test_score_stale_form_d_ignored():
    filings = FilingInfo(days_since_form_d=200)  # outside _FORM_D_DAYS
    risk, _ = dilution.score(filings, dilution.EMPTY_FIN)
    assert risk == 0.0


# --- get_dilution end to end -------------------------------------------------


def test_get_dilution_blends_filings_and_financials(monkeypatch):
    filings = FilingInfo(shelf_active=True, days_since_424b=30)
    monkeypatch.setattr(edgar, "get_filings", lambda symbol, session=None: filings)
    # The surfaced SEC filing + significance come from the >50 selection.
    monkeypatch.setattr(
        sec_significance,
        "evaluate",
        lambda symbol, candidates, session=None: SigResult(
            score=82,
            note="Significance 82/100 (bearish)\n8-K filed 2026-09-30\nPriced $150M offering",
            direction="bearish",
            url="https://sec.gov/x-index.htm",
            form="8-K",
            date="2026-09-30",
        ),
    )

    uw = _FakeUW(
        {
            "balance_sheets": [
                _bs("2025-09-30", shares=120, total_assets=1000),
                _bs("2024-09-30", shares=100, total_assets=900),
            ],
            "cash_flows": [_cf(f"2025-{m:02d}-30", issuance=15) for m in (9, 6, 3, 1)],
        }
    )

    info = dilution.get_dilution("TEST", uw_client=uw)

    assert info.sec_filing_form == "8-K"
    assert info.sec_filing_url == "https://sec.gov/x-index.htm"
    assert info.sec_significance == 82
    assert info.sec_significance_direction == "bearish"
    assert "Priced $150M offering" in (info.sec_significance_note or "")
    assert info.dilution_risk is not None and info.dilution_risk > 0
    assert uw.calls == ["TEST"]


def test_get_dilution_blank_sec_fields_when_nothing_significant(monkeypatch):
    filings = FilingInfo(shelf_active=True, days_since_424b=30)
    monkeypatch.setattr(edgar, "get_filings", lambda symbol, session=None: filings)
    monkeypatch.setattr(
        sec_significance, "evaluate", lambda symbol, candidates, session=None: SigResult()
    )
    uw = _FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1000)]})

    info = dilution.get_dilution("TEST", uw_client=uw)

    # A dilution score still computes, but no filing is surfaced.
    assert info.dilution_risk is not None and info.dilution_risk > 0
    assert info.sec_filing_url is None
    assert info.sec_filing_form is None
    assert info.sec_significance is None
    assert info.sec_significance_note is None
    assert info.sec_significance_direction is None


def test_get_dilution_empty_when_no_cik_and_no_financials(monkeypatch):
    monkeypatch.setattr(edgar, "get_filings", lambda symbol, session=None: edgar.EMPTY_FILINGS)
    uw = _FakeUW({})
    assert dilution.get_dilution("TEST", uw_client=uw) == dilution.EMPTY


def test_get_dilution_without_uw_key_scores_filings_only(monkeypatch):
    # No candidates -> evaluate is a no-op (returns blank) with no network.
    monkeypatch.setattr(
        edgar,
        "get_filings",
        lambda symbol, session=None: FilingInfo(shelf_active=True),
    )
    monkeypatch.setattr(gex, "has_uw_key", lambda: False)

    info = dilution.get_dilution("TEST")
    assert info.dilution_risk == 5.0
    assert info.sec_filing_form is None  # nothing cleared the significance bar
    assert "Financials unavailable" in (info.dilution_note or "")


def test_second_lookup_is_served_from_cache(monkeypatch):
    monkeypatch.setattr(
        edgar, "get_filings", lambda symbol, session=None: FilingInfo(shelf_active=True)
    )
    uw = _FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1000)]})

    dilution.get_dilution("TEST", uw_client=uw)
    dilution.get_dilution("test", uw_client=uw)
    assert uw.calls == ["TEST"]
