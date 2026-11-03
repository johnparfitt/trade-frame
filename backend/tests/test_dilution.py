"""Blended dilution score — fake UW client + stubbed EDGAR, no live network."""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from app.services import company_facts, dilution, edgar, filing_checks, gex, sec_significance
from app.services.company_facts import CompanyFacts, Fact
from app.services.dilution import DocChecks, FinSignals
from app.services.edgar import FilingInfo, FilingRecord
from app.services.filing_checks import OfferingCover, ProxyProposals
from app.services.sec_significance import SigResult

TODAY = date(2026, 10, 27)


@pytest.fixture(autouse=True)
def _clear_cache():
    dilution.clear_cache()
    edgar.clear_cache()
    yield
    dilution.clear_cache()
    edgar.clear_cache()


def _bs(fiscal, *, shares=None, total_assets=None, cash=None) -> dict:
    row = {"fiscal_date_ending": fiscal, "report_type": "quarterly"}
    if shares is not None:
        row["common_stock_shares_outstanding"] = str(shares)
    if total_assets is not None:
        row["total_assets"] = str(total_assets)
    if cash is not None:
        row["cash_and_short_term_investments"] = str(cash)
    return row


def _cf(fiscal, *, sbc=0, op=0, capex=None) -> dict:
    row = {
        "fiscal_date_ending": fiscal,
        "report_type": "quarterly",
        "stock_based_compensation": str(sbc),
        "operating_cashflow": str(op),
    }
    if capex is not None:
        row["capital_expenditures"] = str(capex)
    return row


def _quarters(**kwargs) -> list[dict]:
    """Four quarterly cash-flow rows, newest first, with identical values."""
    return [_cf(f"2025-{m:02d}-30", **kwargs) for m in (9, 6, 3, 1)]


def _rec(form: str, days_ago: int, *, today: date = TODAY, items=()) -> FilingRecord:
    return FilingRecord(
        form=form,
        filed=today - timedelta(days=days_ago),
        accession=f"acc-{form}-{days_ago}",
        items=tuple(items),
    )


def _filings(*records: FilingRecord) -> FilingInfo:
    ordered = tuple(sorted(records, key=lambda r: r.filed, reverse=True))
    return FilingInfo(ok=True, found=True, records=ordered)


def _fin(**kwargs) -> FinSignals:
    return FinSignals(has_data=True, **kwargs)


def _score(filings, fin, **kwargs):
    return dilution.score(filings, fin, today=TODAY, **kwargs)


_GROUP_RE = re.compile(r"^(Behavior|Overhang|Filings|Distress) (\d+):")


def _group_subtotals(note: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in note.splitlines():
        m = _GROUP_RE.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _line_points(line: str) -> list[int]:
    return [int(p) for p in re.findall(r"\(\+(\d+)\)", line)]


def _group_line(note: str, name: str) -> str:
    return next(line for line in note.splitlines() if line.startswith(f"{name} "))


class _FakeUW:
    def __init__(self, data: dict | None = None, *, error: Exception | None = None):
        self._data = data or {}
        self._error = error
        self.calls: list[str] = []

    def financials(self, ticker: str, *, report_type: str = "quarterly") -> dict:
        self.calls.append(ticker)
        if self._error is not None:
            raise self._error
        return self._data


# --- share growth -------------------------------------------------------------


def test_share_growth_year_over_year():
    data = {
        "balance_sheets": [
            _bs("2025-09-30T00:00:00.000Z", shares=120, total_assets=1000),
            _bs("2024-09-30T00:00:00.000Z", shares=100, total_assets=900),
        ]
    }
    fin = dilution.financial_signals(data)
    assert fin.share_growth_pct == pytest.approx(20.0)
    assert fin.share_date == date(2025, 9, 30)
    assert fin.latest_shares == 120


def test_share_growth_ninety_day_comparison_is_none():
    data = {"balance_sheets": [_bs("2025-09-30", shares=120), _bs("2025-07-01", shares=100)]}
    fin = dilution.financial_signals(data)
    assert fin.share_growth_pct is None
    assert fin.share_date == date(2025, 9, 30)


def test_share_growth_two_year_comparison_is_none():
    data = {"balance_sheets": [_bs("2025-09-30", shares=120), _bs("2023-09-30", shares=100)]}
    assert dilution.financial_signals(data).share_growth_pct is None


def test_share_growth_picks_prior_closest_to_one_year():
    data = {
        "balance_sheets": [
            _bs("2025-09-30", shares=130),
            _bs("2024-12-31", shares=125),  # 273 days: outside the window
            _bs("2024-09-30", shares=100),  # 365 days: chosen
            _bs("2024-06-30", shares=90),  # 457 days: outside the window
        ]
    }
    assert dilution.financial_signals(data).share_growth_pct == pytest.approx(30.0)


def test_buybacks_show_up_as_negative_share_growth():
    data = {"balance_sheets": [_bs("2025-09-30", shares=96), _bs("2024-09-30", shares=100)]}
    assert dilution.financial_signals(data).share_growth_pct == pytest.approx(-4.0)


def test_annual_rows_are_ignored_when_quarterly_present():
    data = {
        "balance_sheets": [
            _bs("2025-09-30", shares=120, total_assets=1000),
            _bs("2024-09-30", shares=100, total_assets=1000),
            {**_bs("2025-09-30", shares=999, total_assets=9999), "report_type": "annual"},
        ]
    }
    assert dilution.financial_signals(data).share_growth_pct == pytest.approx(20.0)


# --- TTM / SBC / runway -----------------------------------------------------------


def test_ttm_sbc_sums_four_discrete_quarters():
    data = {"balance_sheets": [_bs("2025-09-30")], "cash_flows": _quarters(sbc=10)}
    assert dilution.financial_signals(data).ttm_sbc == pytest.approx(40.0)


def test_annual_only_ttm_uses_latest_year_not_four_years():
    data = {
        "balance_sheets": [_bs("2025-12-31")],
        "cash_flows": [
            {**_cf(f"{y}-12-31", sbc=40), "report_type": "annual"} for y in (2025, 2024, 2023, 2022)
        ],
    }
    assert dilution.financial_signals(data).ttm_sbc == pytest.approx(40.0)


def test_runway_uses_free_cash_flow():
    data = {
        "balance_sheets": [_bs("2025-09-30", cash=100)],
        "cash_flows": _quarters(op=-20, capex=5),
    }
    fin = dilution.financial_signals(data)
    # FCF -25/quarter -> burn 100/yr -> 1 year of cash.
    assert fin.runway_years == pytest.approx(1.0)
    assert fin.runway_ocf_basis is False


def test_runway_falls_back_to_operating_cash_flow_without_capex():
    data = {"balance_sheets": [_bs("2025-09-30", cash=50)], "cash_flows": _quarters(op=-25)}
    fin = dilution.financial_signals(data)
    assert fin.runway_years == pytest.approx(0.5)
    assert fin.runway_ocf_basis is True


def test_burn_counts_negative_latest_quarter_despite_positive_ttm():
    data = {
        "balance_sheets": [_bs("2025-09-30", cash=20)],
        "cash_flows": [
            _cf("2025-09-30", op=-10, capex=0),
            _cf("2025-06-30", op=30, capex=0),
            _cf("2025-03-30", op=30, capex=0),
            _cf("2025-01-30", op=30, capex=0),
        ],
    }
    # TTM FCF +80, but the latest quarter annualizes to a 40/yr burn.
    assert dilution.financial_signals(data).runway_years == pytest.approx(0.5)


def test_no_runway_when_generating_cash():
    data = {"balance_sheets": [_bs("2025-09-30", cash=50)], "cash_flows": _quarters(op=25, capex=5)}
    assert dilution.financial_signals(data).runway_years is None


def test_empty_financials_has_no_data():
    assert dilution.financial_signals({}) == dilution.EMPTY_FIN
    assert dilution.financial_signals({}).has_data is False


# --- market cap / size ------------------------------------------------------------


def test_market_cap_prefers_snapshot_then_price_times_shares_then_unknown():
    assert dilution.resolve_market_cap(5e9, 10.0, 1e6) == 5e9
    assert dilution.resolve_market_cap(None, 10.0, 1e6) == 1e7
    assert dilution.resolve_market_cap(None, None, 1e6) is None
    assert dilution.resolve_market_cap(None, 10.0, None) is None


@pytest.mark.parametrize(
    ("mcap", "multiplier"),
    [(250e6, 1.25), (300e6, 1.0), (1e9, 1.0), (5e9, 0.6), (50e9, 0.3), (None, 1.0)],
)
def test_size_multiplier_buckets(mcap, multiplier):
    assert dilution.size_multiplier(mcap) == multiplier


@pytest.mark.parametrize(("g", "points"), [(1, 1), (20, 16), (45, 25), (50, 26), (100, 35), (300, 35)])
def test_growth_points_are_log_scaled(g, points):
    assert round(dilution.growth_points(g)) == points


# --- Behavior ---------------------------------------------------------------------


def test_score_buyback_blue_chip_is_near_zero_despite_shelf():
    fin = _fin(share_growth_pct=-2.0, ttm_sbc=0.05 * 3e12)
    risk, note = _score(_filings(_rec("S-3", 100)), fin, market_cap=3e12)
    assert risk == 1  # shelf 3 x0.3; SBC gated off (not issuing)
    assert "Filings 1: S-3 shelf (+3) = 3 x0.3 = 1" in note
    assert "- Net buybacks (shares -2% YoY)" in note
    assert "SBC" not in note


def test_score_small_growth_is_not_issuing():
    risk, note = _score(_filings(_rec("S-3", 100)), _fin(share_growth_pct=0.3, ttm_sbc=50))
    assert risk == 3  # base shelf only: no growth, SBC, or issuing shelf
    assert "YoY" not in note


def test_score_sbc_is_a_share_of_market_cap_when_issuing():
    fin = _fin(share_growth_pct=10.0, ttm_sbc=20e6)
    _, note = _score(_filings(), fin, market_cap=1e9)
    assert "SBC 2.0% of mkt cap (+3)" in note  # 2% / 3% * 5 = 3.3
    _, small = _score(_filings(), _fin(share_growth_pct=10.0, ttm_sbc=5e6), market_cap=1e9)
    assert "SBC" not in small  # below 1%
    _, unknown = _score(_filings(), fin, market_cap=None)
    assert "SBC" not in unknown


def test_score_runway_gated_off_for_large_caps():
    fin = _fin(runway_years=0.4)
    assert _score(_filings(), fin, market_cap=20e9)[0] == 0
    assert _score(_filings(), fin, market_cap=1e9)[0] == 15
    assert _score(_filings(), fin, market_cap=None)[0] == 15  # unknown size still counts


def test_score_runway_line_marks_ocf_basis():
    _, note = _score(_filings(), _fin(runway_years=0.4, runway_ocf_basis=True))
    assert "~5mo runway, OCF basis (+15)" in note


def test_score_behavior_group_is_capped():
    fin = _fin(share_growth_pct=150.0, runway_years=0.3, ttm_sbc=40e6)
    risk, note = _score(_filings(), fin, market_cap=1e9)
    # growth 35 + runway 15 + SBC 5 = 55
    assert risk == 45
    assert _group_line(note, "Behavior").endswith("= 55, capped at 45")


# --- Filings ----------------------------------------------------------------------


def test_score_shelf_is_worth_more_when_issuing():
    risk, note = _score(_filings(_rec("S-3", 100)), _fin(share_growth_pct=10.0))
    assert risk == 18  # growth 10 + issuing shelf 8
    assert "S-3 shelf, issuing (+8)" in note


def test_score_s3asr_only_large_cap_is_zero():
    risk, note = _score(_filings(_rec("S-3ASR", 200)), _fin(share_growth_pct=-3.0), market_cap=3e12)
    assert risk == 0
    assert "shelf" not in note


def test_score_f3_counts_as_shelf():
    assert _score(_filings(_rec("F-3", 200)), _fin())[0] == 3


def test_score_stale_shelf_ignored():
    assert _score(_filings(_rec("S-3", 3 * 365 + 1)), _fin())[0] == 0


def test_score_424b2_only_bank_is_zero():
    records = [_rec("424B2", d) for d in (5, 20, 40, 60, 80)]
    risk, note = _score(_filings(*records), _fin(share_growth_pct=-1.5))
    assert risk == 0
    assert "424B" not in note
    assert "offerings/24mo" not in note


def test_score_424b3_resale_counts_at_reduced_weight():
    risk, note = _score(_filings(_rec("424B3", 30)), _fin())
    assert risk == 4
    assert "424B3 resale 30d ago (+4)" in note


def test_score_offering_outranks_resale():
    risk, note = _score(_filings(_rec("424B3", 10), _rec("424B5", 30)), _fin())
    assert risk == 10
    assert "424B5 offering 30d ago (+10)" in note
    assert "resale" not in note


def test_score_424b4_counts_as_offering():
    assert _score(_filings(_rec("424B4", 30)), _fin())[0] == 10


def test_score_serial_offerings_exclude_resales():
    records = [_rec("424B5", d) for d in (200, 400, 600)] + [_rec("424B3", 300)]
    risk, note = _score(_filings(*records), _fin())
    assert risk == 5
    assert "3 offerings/24mo (+5)" in note


def test_score_shelf_stacks_with_offering():
    fin = _fin(share_growth_pct=10.0, share_date=TODAY - timedelta(days=60))
    _, note = _score(_filings(_rec("S-3", 300), _rec("424B5", 20)), fin)
    assert _group_line(note, "Filings") == "Filings 18: 424B5 offering 20d ago (+10), S-3 shelf, issuing (+8)"


def test_score_offering_before_share_date_is_already_in_growth():
    fin = _fin(share_growth_pct=30.0, share_date=TODAY - timedelta(days=60))
    _, before = _score(_filings(_rec("424B5", 80)), fin)
    assert "offering" not in before
    _, after = _score(_filings(_rec("424B5", 30)), fin)
    assert "424B5 offering 30d ago (+10)" in after


def test_score_offering_counts_when_growth_did_not_score():
    fin = _fin(share_growth_pct=None, share_date=TODAY - timedelta(days=60))
    _, note = _score(_filings(_rec("424B5", 80)), fin)
    assert "424B5 offering 80d ago (+10)" in note


def test_score_serial_offerings_count_even_before_share_date():
    fin = _fin(share_growth_pct=30.0, share_date=TODAY - timedelta(days=10))
    _, note = _score(_filings(*[_rec("424B5", d) for d in (20, 200, 400)]), fin)
    assert "3 offerings/24mo (+5)" in note
    assert "424B5 offering" not in note


def test_score_registration_includes_amendments_and_f1():
    for form in ("S-1", "S-1/A", "F-1"):
        risk, note = _score(_filings(_rec(form, 60)), _fin())
        assert risk == 8
        assert f"{form} 60d ago (+8)" in note


def test_score_recent_form_d_contributes():
    risk, note = _score(_filings(_rec("D", 20)), dilution.EMPTY_FIN)
    assert risk == 4
    assert "Form D 20d ago (+4)" in note


def test_score_stale_form_d_ignored():
    assert _score(_filings(_rec("D", 200)), dilution.EMPTY_FIN)[0] == 0


def test_score_item_302_unregistered_sale_contributes():
    for form in ("8-K", "8-K/A"):
        risk, note = _score(_filings(_rec(form, 41, items=("3.02", "9.01"))), _fin())
        assert risk == 8
        assert "Item 3.02 41d ago (+8)" in _group_line(note, "Filings")


def test_score_item_302_ignores_other_items_and_stale_filings():
    assert _score(_filings(_rec("8-K", 10, items=("2.02", "9.01"))), _fin())[0] == 0
    assert _score(_filings(_rec("8-K", 100, items=("3.02",))), _fin())[0] == 0


def test_score_item_302_counts_even_before_share_date():
    fin = _fin(share_growth_pct=20.0, share_date=TODAY - timedelta(days=10))
    _, note = _score(_filings(_rec("8-K", 41, items=("3.02",))), fin)
    assert "Item 3.02 41d ago (+8)" in note


# --- Distress -----------------------------------------------------------------


def test_score_recent_reverse_split_is_distress():
    splits = [(TODAY - timedelta(days=150), 0.05)]
    risk, note = _score(_filings(), _fin(), splits=splits)
    assert risk == 10
    assert _group_line(note, "Distress") == "Distress 10: Reverse split 1:20 5mo ago (+10)"


def test_score_reverse_split_label_uses_days_when_recent():
    _, note = _score(_filings(), _fin(), splits=[(TODAY - timedelta(days=12), 0.1)])
    assert "Reverse split 1:10 12d ago (+10)" in note


def test_score_reverse_split_ignores_forward_and_stale_splits():
    splits = [(TODAY - timedelta(days=30), 10.0), (TODAY - timedelta(days=400), 0.1)]
    risk, note = _score(_filings(), _fin(), splits=splits)
    assert risk == 0
    assert "Distress" not in note


def test_score_reverse_split_is_not_scaled_by_size():
    splits = [(TODAY - timedelta(days=150), 0.05)]
    assert _score(_filings(), _fin(), splits=splits, market_cap=50e9)[0] == 10


def test_score_flags_missing_split_history():
    _, note = _score(_filings(), _fin(), splits=None)
    assert "- Split history unavailable" in note
    assert "Split history" not in _score(_filings(), _fin(), splits=[])[1]


def test_score_filings_scaled_by_size_then_capped():
    filings = _filings(
        _rec("424B5", 10), _rec("424B5", 200), _rec("424B5", 400), _rec("S-3", 300), _rec("S-1", 60), _rec("D", 20)
    )
    fin = _fin(share_growth_pct=None)
    risk, note = _score(filings, fin, market_cap=250e6)
    # offering 10 + serial 5 + shelf 3 + S-1 8 + Form D 4 = 30 -> x1.25 = 38 -> 30
    assert risk == 30
    assert _group_line(note, "Filings").endswith("= 30 x1.25 = 38, capped at 30")
    large, large_note = _score(filings, fin, market_cap=50e9)
    assert large == 9  # 30 x0.3
    assert _group_line(large_note, "Filings").endswith("= 30 x0.3 = 9")


# --- Total / note -----------------------------------------------------------------


def test_score_total_clamps_to_cap(monkeypatch):
    monkeypatch.setattr(dilution, "_TOTAL_CAP", 50)
    fin = _fin(share_growth_pct=150.0, runway_years=0.3)
    risk, note = _score(_filings(_rec("424B5", 10), _rec("S-1", 60)), fin)
    # Behavior 45 (capped from 50) + Filings 18 = 63 -> 50
    assert risk == 50
    assert note.splitlines()[0] == "Dilution Risk 50/100 (63, capped at 50)"


@pytest.mark.parametrize("mcap", [None, 250e6, 1e9, 5e9, 50e9])
def test_score_note_adds_up_to_score(mcap):
    filings = _filings(
        _rec("424B5", 10),
        _rec("424B5", 200),
        _rec("424B5", 400),
        _rec("S-3", 300),
        _rec("S-1", 60),
        _rec("D", 20),
        _rec("8-K", 41, items=("3.02",)),
    )
    fin = _fin(share_growth_pct=45.0, share_date=TODAY - timedelta(days=30), runway_years=0.8, ttm_sbc=2e7)
    splits = [(TODAY - timedelta(days=150), 0.05)]
    risk, note = _score(filings, fin, market_cap=mcap, splits=splits)
    subtotals = _group_subtotals(note)
    assert note.startswith(f"Dilution Risk {risk}/100")
    assert sum(subtotals.values()) == risk
    # Unscaled, uncapped groups: the subtotal is the sum of its lines.
    behavior = _group_line(note, "Behavior")
    if "capped" not in behavior:
        assert sum(_line_points(behavior)) == subtotals["Behavior"]


def test_score_filings_only_flags_financials_missing():
    risk, note = _score(_filings(_rec("S-3", 100), _rec("424B5", 10)), dilution.EMPTY_FIN)
    assert risk == 13  # shelf 3 (not issuing) + offering 10
    assert "- Financials unavailable" in note


def test_score_distinguishes_failed_lookups_from_missing_data():
    _, note = _score(edgar.FAILED_FILINGS, dilution.FAILED_FIN)
    assert "- Financials lookup failed" in note
    assert "- SEC filings lookup failed" in note
    _, note = _score(edgar.EMPTY_FILINGS, _fin())
    assert "- SEC filings unavailable" in note


def test_score_no_signals():
    risk, note = _score(_filings(), dilution.EMPTY_FIN)
    assert risk == 0
    assert note == "Dilution Risk 0/100\n- Financials unavailable"
    _, quiet = _score(_filings(), _fin())
    assert quiet == "Dilution Risk 0/100\nNo dilution signals"


# --- get_dilution end to end -------------------------------------------------


def _patch_filings(monkeypatch, info: FilingInfo) -> None:
    monkeypatch.setattr(edgar, "get_filings", lambda symbol, session=None: info)


def _blank_significance(monkeypatch) -> None:
    monkeypatch.setattr(sec_significance, "evaluate", lambda symbol, candidates, session=None: SigResult())


def _cache_ttl(symbol: str) -> float:
    """Seconds until ``symbol``'s cached dilution entry expires (clock frozen at 1000)."""
    return dilution._cache[symbol][0] - 1000.0


def _live(form: str, days_ago: int) -> FilingRecord:
    return _rec(form, days_ago, today=date.today())


def test_get_dilution_blends_filings_and_financials(monkeypatch):
    _patch_filings(monkeypatch, _filings(_live("S-3", 100), _live("424B5", 30)))
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
        }
    )

    info = dilution.get_dilution("TEST", uw_client=uw)

    assert info.sec_filing_form == "8-K"
    assert info.sec_filing_url == "https://sec.gov/x-index.htm"
    assert info.sec_significance == 82
    assert info.sec_significance_direction == "bearish"
    assert "Priced $150M offering" in (info.sec_significance_note or "")
    # growth 16 (+20%) + issuing shelf 8 + offering after share_date 10
    assert info.dilution_risk == 34.0
    assert uw.calls == ["TEST"]


def test_get_dilution_blank_sec_fields_when_nothing_significant(monkeypatch):
    _patch_filings(monkeypatch, _filings(_live("424B5", 30)))
    _blank_significance(monkeypatch)
    uw = _FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1000)]})

    info = dilution.get_dilution("TEST", uw_client=uw)

    assert info.dilution_risk == 10.0  # a score still computes
    assert info.sec_filing_url is None
    assert info.sec_filing_form is None
    assert info.sec_significance is None
    assert info.sec_significance_note is None
    assert info.sec_significance_direction is None


def test_get_dilution_market_cap_gates_runway(monkeypatch):
    _patch_filings(monkeypatch, _filings())
    _blank_significance(monkeypatch)
    burner = {"balance_sheets": [_bs("2025-09-30", shares=1e9, cash=50)], "cash_flows": _quarters(op=-25, capex=0)}

    big = dilution.get_dilution("BIG", market_cap=20e9, uw_client=_FakeUW(burner))
    assert "runway" not in (big.dilution_note or "")
    # No snapshot market cap: price x shares = $20B also gates it off.
    priced = dilution.get_dilution("PRICED", price=20.0, uw_client=_FakeUW(burner))
    assert "runway" not in (priced.dilution_note or "")
    unknown = dilution.get_dilution("UNKNOWN", uw_client=_FakeUW(burner))
    assert "runway" in (unknown.dilution_note or "")


def test_get_dilution_market_cap_scales_filings(monkeypatch):
    _patch_filings(monkeypatch, _filings(_live("424B5", 30)))
    _blank_significance(monkeypatch)
    uw = _FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1000)]})

    assert dilution.get_dilution("TINY", market_cap=100e6, uw_client=uw).dilution_risk == 13.0
    assert dilution.get_dilution("HUGE", market_cap=100e9, uw_client=uw).dilution_risk == 3.0


def test_get_dilution_etf_is_empty_and_cached_for_full_ttl(monkeypatch):
    monkeypatch.setattr(dilution.time, "monotonic", lambda: 1000.0)
    _patch_filings(monkeypatch, edgar.EMPTY_FILINGS)

    assert dilution.get_dilution("SPY", splits=[], uw_client=_FakeUW({})) == dilution.EMPTY
    assert _cache_ttl("SPY") == pytest.approx(dilution.get_settings().sec_cache_ttl)


def test_get_dilution_all_lookups_failing_is_none_not_zero(monkeypatch):
    monkeypatch.setattr(dilution.time, "monotonic", lambda: 1000.0)
    _patch_filings(monkeypatch, edgar.FAILED_FILINGS)

    info = dilution.get_dilution("TEST", uw_client=_FakeUW(error=RuntimeError("uw down")))

    assert info.dilution_risk is None
    assert _cache_ttl("TEST") == pytest.approx(600.0)


def test_get_dilution_failed_lookup_caches_for_ten_minutes(monkeypatch):
    monkeypatch.setattr(dilution.time, "monotonic", lambda: 1000.0)
    _patch_filings(monkeypatch, _filings(_live("424B5", 30)))
    _blank_significance(monkeypatch)

    info = dilution.get_dilution("TEST", uw_client=_FakeUW(error=RuntimeError("uw down")))

    assert info.dilution_risk == 10.0  # partial data still scores
    assert "Financials lookup failed" in (info.dilution_note or "")
    assert _cache_ttl("TEST") == pytest.approx(600.0)


def test_get_dilution_healthy_lookup_caches_for_full_ttl(monkeypatch):
    monkeypatch.setattr(dilution.time, "monotonic", lambda: 1000.0)
    _patch_filings(monkeypatch, _filings(_live("424B5", 30)))
    _blank_significance(monkeypatch)

    dilution.get_dilution(
        "TEST", splits=[], uw_client=_FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1)]})
    )

    assert _cache_ttl("TEST") == pytest.approx(dilution.get_settings().sec_cache_ttl)


def test_get_dilution_failed_split_history_caches_for_ten_minutes(monkeypatch):
    monkeypatch.setattr(dilution.time, "monotonic", lambda: 1000.0)
    _patch_filings(monkeypatch, _filings(_live("424B5", 30)))
    _blank_significance(monkeypatch)

    info = dilution.get_dilution(
        "TEST", splits=None, uw_client=_FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1)]})
    )

    assert "Split history unavailable" in (info.dilution_note or "")
    assert _cache_ttl("TEST") == pytest.approx(600.0)


def test_get_dilution_scores_reverse_split_from_snapshot(monkeypatch):
    _patch_filings(monkeypatch, _filings(_live("424B5", 30)))
    _blank_significance(monkeypatch)
    uw = _FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1)]})

    info = dilution.get_dilution(
        "TEST", splits=[(date.today() - timedelta(days=90), 0.1)], uw_client=uw
    )

    assert info.dilution_risk == 20.0  # offering 10 + reverse split 10
    assert "Reverse split 1:10 3mo ago (+10)" in (info.dilution_note or "")


def test_get_dilution_without_uw_key_scores_filings_only(monkeypatch):
    # No candidates -> evaluate is a no-op (returns blank) with no network.
    _patch_filings(monkeypatch, _filings(_live("S-3", 100)))
    monkeypatch.setattr(gex, "has_uw_key", lambda: False)

    info = dilution.get_dilution("TEST")
    assert info.dilution_risk == 3.0
    assert info.sec_filing_form is None  # nothing cleared the significance bar
    assert "Financials unavailable" in (info.dilution_note or "")


def test_second_lookup_is_served_from_cache(monkeypatch):
    _patch_filings(monkeypatch, _filings(_live("S-3", 100)))
    uw = _FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1000)]})

    dilution.get_dilution("TEST", uw_client=uw)
    dilution.get_dilution("test", uw_client=uw)
    assert uw.calls == ["TEST"]


# --- cover-page share count ------------------------------------------------------


def _facts(**kwargs) -> CompanyFacts:
    return CompanyFacts(ok=True, found=True, **kwargs)


def _f(value: float, end: date) -> Fact:
    return Fact(value=value, end=end, filed=end + timedelta(days=5), accession=f"acc-{end}")


def _history_fin(**kwargs) -> FinSignals:
    """UW quarterly share counts: 100 a year before 2026-06-30, 110 at 2026-06-30."""
    history = ((date(2025, 6, 30), 100.0), (date(2025, 9, 30), 102.0), (date(2026, 6, 30), 110.0))
    defaults = dict(share_growth_pct=10.0, share_date=date(2026, 6, 30), latest_shares=110.0, share_history=history)
    return _fin(**{**defaults, **kwargs})


def test_cover_date_drives_the_yoy_window():
    cover = _f(130.0, date(2026, 8, 3))
    fin = dilution.apply_cover_count(_history_fin(), _facts(cover_shares=cover), [], TODAY)

    assert fin.share_date == date(2026, 8, 3)
    assert fin.latest_shares == 130.0
    # Measured from the cover date: of the priors 307d and 399d earlier, 2025-06-30
    # (399d, 100 shares) is closer to a year.
    assert fin.share_growth_pct == pytest.approx(30.0)


def test_older_cover_count_keeps_the_balance_sheet():
    fin = _history_fin()
    assert dilution.apply_cover_count(fin, _facts(cover_shares=_f(99.0, date(2026, 5, 1))), [], TODAY) == fin


def test_multi_class_company_without_cover_fact_uses_balance_sheet():
    fin = _history_fin()
    assert dilution.apply_cover_count(fin, _facts(), [], TODAY) == fin
    ambiguous = _facts(ambiguous=frozenset({company_facts.COVER_SHARES}))
    assert dilution.apply_cover_count(fin, ambiguous, [], TODAY) == fin


def test_cover_count_is_split_adjusted_to_uw_basis():
    cover = _f(2_000.0, date(2026, 8, 3))
    splits = [(date(2026, 9, 15), 0.05), (date(2026, 1, 5), 0.5)]  # only the later 1:20 applies
    fin = dilution.apply_cover_count(_history_fin(), _facts(cover_shares=cover), splits, TODAY)
    assert fin.latest_shares == pytest.approx(100.0)


def test_cover_count_not_used_without_split_data():
    fin = _history_fin()
    assert dilution.apply_cover_count(fin, _facts(cover_shares=_f(130.0, date(2026, 8, 3))), None, TODAY) == fin


def test_cover_count_moves_share_date_for_no_double_counting():
    # An offering between the balance sheet and the cover date is already in g.
    fin = dilution.apply_cover_count(
        _history_fin(), _facts(cover_shares=_f(130.0, TODAY - timedelta(days=20))), [], TODAY
    )
    _, note = _score(_filings(_rec("424B5", 40)), fin)
    assert "offering" not in note


# --- split guard on share growth ------------------------------------------------


def _grown(g: float) -> FinSignals:
    return _fin(share_growth_pct=g, share_prior_date=date(2025, 6, 30), share_date=date(2026, 6, 30))


def test_large_change_across_a_split_is_unverified():
    fin = dilution.verify_share_growth(_grown(193_000.0), [(date(2026, 2, 6), 0.05)])
    assert fin.share_growth_pct is None
    risk, note = _score(_filings(), fin)
    assert risk == 0
    assert "- Share change unverified: possible split" in note


def test_large_change_without_failed_split_data_is_unverified():
    assert dilution.verify_share_growth(_grown(60.0), None).share_growth_unverified is True


def test_real_issuance_without_a_split_still_scores():
    fin = dilution.verify_share_growth(_grown(60.0), [])
    assert fin.share_growth_pct == 60.0
    # A split outside the YoY window doesn't matter either.
    assert dilution.verify_share_growth(_grown(60.0), [(date(2026, 9, 1), 0.1)]).share_growth_pct == 60.0


def test_small_change_across_a_split_still_scores():
    assert dilution.verify_share_growth(_grown(20.0), [(date(2026, 2, 6), 0.05)]).share_growth_pct == 20.0
    assert dilution.verify_share_growth(_grown(20.0), None).share_growth_pct == 20.0


def test_huge_growth_label_is_capped():
    _, note = _score(_filings(), _fin(share_growth_pct=16_926.0))
    assert "Shares >+1000% YoY (+35)" in note


# --- document checks -------------------------------------------------------------


class _FakeChecks:
    """Stand-in for filing_checks: canned answers by accession; records calls."""

    def __init__(self, offerings=None, proxies=None, splits_8k=None):
        self.offerings = offerings or {}
        self.proxies = proxies or {}
        self.splits_8k = splits_8k or {}
        self.calls: list[str] = []

    def install(self, monkeypatch) -> "_FakeChecks":
        def offering(record, *, session):  # noqa: ARG001
            self.calls.append(record.accession)
            return self.offerings.get(record.accession, OfferingCover())

        def proxy(record, *, session):  # noqa: ARG001
            self.calls.append(record.accession)
            return self.proxies.get(record.accession, ProxyProposals())

        def split_8k(record, *, session):  # noqa: ARG001
            self.calls.append(record.accession)
            return self.splits_8k.get(record.accession, False)

        monkeypatch.setattr(filing_checks, "check_offering", offering)
        monkeypatch.setattr(filing_checks, "check_proxy", proxy)
        monkeypatch.setattr(filing_checks, "check_reverse_split_8k", split_8k)
        return self


_NOTES = OfferingCover(debt=True)
_ATM_COVER = OfferingCover(atm=True)


def _checks(records, monkeypatch, **answers) -> tuple[DocChecks, _FakeChecks]:
    fake = _FakeChecks(**answers).install(monkeypatch)
    return dilution.run_document_checks(records, session=None, today=TODAY), fake


def test_only_newest_six_offerings_are_fetched_and_rest_inherit_majority(monkeypatch):
    records = _filings(*[_rec("424B5", 10 * (i + 1)) for i in range(9)]).records
    accs = [r.accession for r in records]
    debt_answers = {a: _NOTES for a in accs[:5]}  # 5 of 6 checked are notes

    checks, fake = _checks(records, monkeypatch, offerings=debt_answers)

    assert fake.calls == accs[:6]
    assert [checks.offering_class[a] for a in accs[6:]] == ["debt"] * 3


def test_inherited_class_is_equity_without_a_debt_majority(monkeypatch):
    records = _filings(*[_rec("424B5", 10 * (i + 1)) for i in range(8)]).records
    accs = [r.accession for r in records]
    checks, _ = _checks(records, monkeypatch, offerings={a: _NOTES for a in accs[:3]})  # 3 of 6: a tie
    assert [checks.offering_class[a] for a in accs[6:]] == ["equity", "equity"]


def test_unknown_offering_check_counts_as_equity(monkeypatch):
    records = _filings(_rec("424B5", 10)).records
    fake = _FakeChecks().install(monkeypatch)
    monkeypatch.setattr(filing_checks, "check_offering", lambda record, *, session: None)

    checks = dilution.run_document_checks(records, session=None, today=TODAY)

    assert checks.offering_class[records[0].accession] == "equity"
    assert checks.unknown == 1
    assert fake.calls == []


def test_atm_only_from_424b5(monkeypatch):
    records = _filings(_rec("424B5", 10), _rec("424B3", 20)).records
    checks, _ = _checks(records, monkeypatch, offerings={r.accession: _ATM_COVER for r in records})
    assert checks.offering_class == {records[0].accession: "atm", records[1].accession: "equity"}


def test_proxy_checks_stop_once_both_proposals_found(monkeypatch):
    records = _filings(_rec("DEF 14A", 30), _rec("PRE 14A", 40), _rec("DEF 14A", 300)).records
    both = ProxyProposals(authorized_increase=True, reverse_split=True)
    checks, fake = _checks(records, monkeypatch, proxies={records[0].accession: both})

    assert fake.calls == [records[0].accession]
    assert checks.authorized_increase == records[0]
    assert checks.reverse_split_proposed == records[0]


def test_proxy_checks_read_at_most_two_within_a_year(monkeypatch):
    records = _filings(_rec("DEF 14A", 30), _rec("PRE 14A", 40), _rec("DEF 14A", 100), _rec("DEF 14A", 400)).records
    _, fake = _checks(records, monkeypatch)
    assert fake.calls == [records[0].accession, records[1].accession]


def test_item_503_reverse_split_8k(monkeypatch):
    split_8k = _rec("8-K", 45, items=("5.03", "9.01"))
    records = _filings(split_8k, _rec("8-K", 20, items=("2.02",))).records
    checks, fake = _checks(records, monkeypatch, splits_8k={split_8k.accession: True})

    assert checks.reverse_split_8k == split_8k
    assert fake.calls == [split_8k.accession]

    fake.calls.clear()
    skipped = dilution.run_document_checks(records, session=None, today=TODAY, check_reverse_split_8k=False)
    assert skipped.reverse_split_8k is None
    assert fake.calls == []


# --- filings scoring with document checks ----------------------------------------


def _with_classes(**classes: str) -> DocChecks:
    return DocChecks(offering_class=classes)


def test_active_atm_outranks_offering_and_stacks_with_shelf():
    atm, shelf = _rec("424B5", 150), _rec("S-3", 400)
    fin = _fin(share_growth_pct=20.0, share_date=TODAY - timedelta(days=30))
    risk, note = _score(_filings(atm, shelf), fin, checks=_with_classes(**{atm.accession: "atm"}))
    line = _group_line(note, "Filings")
    assert "Active ATM, 424B5 5mo ago (+12)" in line
    assert "S-3 shelf, issuing (+8)" in line
    assert "offering" not in line


def test_active_atm_counts_even_before_share_date():
    atm = _rec("424B5", 150)
    fin = _fin(share_growth_pct=20.0, share_date=TODAY - timedelta(days=10))
    _, note = _score(_filings(atm), fin, checks=_with_classes(**{atm.accession: "atm"}))
    assert "Active ATM" in note


def test_stale_atm_ignored():
    atm = _rec("424B5", 600)
    assert _score(_filings(atm), _fin(), checks=_with_classes(**{atm.accession: "atm"}))[0] == 0


def test_debt_takedowns_score_no_offering_or_serial_points():
    notes = [_rec("424B5", d) for d in (10, 100, 200, 300)]
    checks = _with_classes(**{r.accession: "debt" for r in notes})
    risk, note = _score(_filings(*notes), _fin(), checks=checks)
    assert risk == 0
    assert "Filings" not in note


def test_debt_424b3_is_not_a_resale():
    notes = _rec("424B3", 10)
    assert _score(_filings(notes), _fin(), checks=_with_classes(**{notes.accession: "debt"}))[0] == 0


def test_unknown_check_flags_filing_text_unavailable():
    offering = _rec("424B5", 10)
    _, note = _score(_filings(offering), _fin(), checks=DocChecks(unknown=1))
    assert "424B5 offering 10d ago (+10)" in note  # counted as equity
    assert "- Filing text unavailable" in note


# --- overhang ------------------------------------------------------------------------


def test_warrants_score_by_share_of_outstanding():
    facts = _facts(warrants=_f(15e6, date(2026, 6, 30)))
    risk, note = _score(_filings(), _fin(latest_shares=100e6), facts=facts)
    assert risk == 6  # 15% / 25% x 10
    assert _group_line(note, "Overhang") == "Overhang 6: Warrants 15% of shares (+6)"


def test_small_warrant_overhang_scores_nothing():
    facts = _facts(warrants=_f(2.6e6, date(2026, 3, 31)))
    assert _score(_filings(), _fin(latest_shares=250e6), facts=facts)[0] == 0


def test_warrants_split_adjusted_to_todays_basis():
    facts = _facts(warrants=_f(400e6, date(2026, 3, 31)))
    splits = [(date(2026, 6, 1), 0.05)]  # 400M pre-split -> 20M
    _, note = _score(_filings(), _fin(latest_shares=100e6), facts=facts, splits=splits)
    assert "Warrants 20% of shares (+8)" in note


def test_convertibles_score_with_conversion_price_bonus():
    facts = _facts(
        convertible_principal=_f(50e6, date(2026, 6, 30)),
        conversion_price=_f(12.0, date(2026, 6, 30)),
    )
    risk, note = _score(_filings(), _fin(), facts=facts, market_cap=250e6, price=10.0, price_time="14:05")
    line = _group_line(note, "Overhang")
    assert "Convertibles 20% of mkt cap (+6)" in line
    assert "Conversion $12.00 near $10.00 price as of 14:05 (+3)" in line
    assert risk == 9


def test_far_out_of_the_money_conversion_gets_no_bonus():
    facts = _facts(
        convertible_principal=_f(50e6, date(2026, 6, 30)),
        conversion_price=_f(40.0, date(2026, 6, 30)),
    )
    _, note = _score(_filings(), _fin(), facts=facts, market_cap=250e6, price=10.0)
    assert "Conversion" not in note


def test_overhang_group_is_capped():
    facts = _facts(
        warrants=_f(50e6, date(2026, 6, 30)),
        convertible_principal=_f(100e6, date(2026, 6, 30)),
        conversion_price=_f(5.0, date(2026, 6, 30)),
    )
    risk, note = _score(_filings(), _fin(latest_shares=100e6), facts=facts, market_cap=250e6, price=10.0)
    assert risk == 15  # warrants 10 + converts 7 + bonus 3 = 20 -> 15
    assert _group_line(note, "Overhang").endswith("= 20, capped at 15")


def test_absent_warrant_and_convertible_tags_score_zero_silently():
    risk, note = _score(_filings(), _fin(latest_shares=100e6), facts=_facts(), market_cap=250e6)
    assert risk == 0
    assert "unavailable" not in note


def test_failed_or_missing_company_facts_say_unavailable():
    for facts in (company_facts.FAILED_FACTS, company_facts.NO_FACTS):
        _, note = _score(_filings(), _fin(), facts=facts)
        assert "- Warrant/convertible data unavailable" in note


def test_ambiguous_overhang_facts_say_unavailable():
    facts = _facts(ambiguous=frozenset({company_facts.WARRANTS, company_facts.CONVERTIBLES}))
    _, note = _score(_filings(), _fin(latest_shares=100e6), facts=facts, market_cap=250e6)
    assert "- Warrant data unavailable" in note
    assert "- Convertible data unavailable" in note


# --- distress from filing text ----------------------------------------------------


def test_proxy_proposals_score_and_cap_with_executed_split():
    proxy = _rec("DEF 14A", 60)
    checks = DocChecks(authorized_increase=proxy, reverse_split_proposed=proxy)
    splits = [(TODAY - timedelta(days=90), 0.1)]
    risk, note = _score(_filings(proxy), _fin(), checks=checks, splits=splits)
    assert risk == 15
    assert _group_line(note, "Distress") == (
        "Distress 15: Reverse split 1:10 3mo ago (+10), Authorized-share increase proposed 2mo ago (+8), "
        "Reverse split proposed 2mo ago (+8) = 26, capped at 15"
    )


def test_reverse_split_8k_counts_when_split_history_lacks_it():
    split_8k = _rec("8-K", 40, items=("5.03",))
    risk, note = _score(_filings(split_8k), _fin(), checks=DocChecks(reverse_split_8k=split_8k))
    assert risk == 10
    assert "Reverse split per 8-K 40d ago (+10)" in note


# --- get_dilution with company facts and document checks ---------------------------


def _filings_with_cik(*records: FilingRecord) -> FilingInfo:
    ordered = tuple(sorted(records, key=lambda r: r.filed, reverse=True))
    return FilingInfo(ok=True, found=True, records=ordered, cik=42)


def test_get_dilution_unknown_document_check_is_degraded(monkeypatch):
    monkeypatch.setattr(dilution.time, "monotonic", lambda: 1000.0)
    _patch_filings(monkeypatch, _filings(_live("424B5", 30)))
    _blank_significance(monkeypatch)
    monkeypatch.setattr(filing_checks, "check_offering", lambda record, *, session: None)

    info = dilution.get_dilution("TEST", splits=[], uw_client=_FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1)]}))

    assert "Filing text unavailable" in (info.dilution_note or "")
    assert _cache_ttl("TEST") == pytest.approx(600.0)


def test_get_dilution_failed_company_facts_is_degraded(monkeypatch):
    monkeypatch.setattr(dilution.time, "monotonic", lambda: 1000.0)
    _patch_filings(monkeypatch, _filings_with_cik(_live("S-3", 100)))
    _blank_significance(monkeypatch)
    monkeypatch.setattr(company_facts, "get_company_facts", lambda cik, records, *, session: company_facts.FAILED_FACTS)

    info = dilution.get_dilution("TEST", splits=[], uw_client=_FakeUW({"balance_sheets": [_bs("2025-09-30", total_assets=1)]}))

    assert "Warrant/convertible data unavailable" in (info.dilution_note or "")
    assert _cache_ttl("TEST") == pytest.approx(600.0)


def test_get_dilution_uses_cover_count_and_overhang(monkeypatch):
    _patch_filings(monkeypatch, _filings_with_cik())
    _blank_significance(monkeypatch)
    today = date.today()
    cover_end = today - timedelta(days=20)
    facts = _facts(
        cover_shares=_f(200e6, cover_end),
        warrants=_f(30e6, today - timedelta(days=60)),
    )
    monkeypatch.setattr(company_facts, "get_company_facts", lambda cik, records, *, session: facts)
    year_ago = (cover_end - timedelta(days=365)).isoformat()
    uw = _FakeUW({"balance_sheets": [_bs(year_ago, shares=100e6, total_assets=1)]})

    info = dilution.get_dilution("TEST", splits=[], price=1.0, uw_client=uw)

    note = info.dilution_note or ""
    assert "Shares +100% YoY (+35)" in note  # cover 200M vs UW 100M a year earlier
    assert "Warrants 15% of shares (+6)" in note  # 30M / the 200M cover count
    assert info.dilution_risk == 41.0
