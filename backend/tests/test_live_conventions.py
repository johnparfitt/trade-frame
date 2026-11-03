"""Data conventions the dilution score relies on, pinned to recorded live payloads.

The fixture was captured from UW and yfinance on 2026-10-27. If a provider
changes a convention, re-record it and revisit ``app.services.dilution``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.services import dilution

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "live_conventions_2026-10-27.json").read_text()
)
UW = FIXTURE["uw"]


def _shares_by_date(ticker: str) -> list[tuple[str, float]]:
    rows = UW[ticker]["balance_sheets"]
    return sorted((r["fiscal_date_ending"], float(r["common_stock_shares_outstanding"])) for r in rows)


@pytest.mark.parametrize("ticker", ["NVDA", "LCID"])
def test_uw_share_counts_are_split_adjusted(ticker):
    # NVDA split 10-for-1 and LCID 1-for-10 inside this range; an unadjusted
    # series would jump ~10x (or ~0.1x) between consecutive quarters.
    series = _shares_by_date(ticker)
    for (_, prev), (_, cur) in zip(series, series[1:]):
        assert 0.5 < cur / prev < 1.5


def test_uw_split_adjusted_counts_give_real_growth():
    nvda = dilution.financial_signals({"balance_sheets": UW["NVDA"]["balance_sheets"]})
    assert nvda.share_growth_pct == pytest.approx(-1.0, abs=0.1)  # buybacks, not +900%
    lcid = dilution.financial_signals({"balance_sheets": UW["LCID"]["balance_sheets"]})
    assert lcid.share_growth_pct == pytest.approx(25.0, abs=0.1)  # issuance, not -90%


@pytest.mark.parametrize("ticker", ["NVDA", "LCID"])
def test_uw_quarterly_cash_flows_are_discrete(ticker):
    # Year-to-date quarters would sum to ~2.5x the annual figure.
    rows = UW[ticker]["cash_flows"]
    annual = next(r for r in rows if r["report_type"] == "annual")
    quarters = [r for r in rows if r["report_type"] == "quarterly"]
    assert len(quarters) == 4
    for field in ("operating_cashflow", "capital_expenditures"):
        total = sum(float(q[field]) for q in quarters)
        assert total == pytest.approx(float(annual[field]), rel=0.001)
    # SBC is discrete too, but UW's LCID quarters miss the annual by one
    # quarter's worth (a data quirk, not year-to-date rows).
    sbc = sum(float(q["stock_based_compensation"]) for q in quarters)
    assert sbc == pytest.approx(float(annual["stock_based_compensation"]), rel=0.15)


def test_uw_capex_field_is_positive_capital_expenditures():
    for ticker in ("NVDA", "LCID"):
        for row in UW[ticker]["cash_flows"]:
            assert float(row["capital_expenditures"]) > 0


def test_uw_share_count_covers_all_classes():
    latest = _shares_by_date("GOOGL")[-1][1]
    assert latest > 10e9  # A+B+C; class A alone is ~5.8B


def test_yfinance_split_ratio_is_new_over_old():
    splits = {t: {date.fromisoformat(d): r for d, r in rows} for t, rows in FIXTURE["yfinance_splits"].items()}
    assert splits["NVDA"][date(2024, 6, 10)] == 10.0  # 10-for-1 forward
    assert splits["LCID"][date(2025, 9, 2)] == 0.1  # 1-for-10 reverse
    assert splits["AMC"][date(2023, 8, 24)] == 0.1
