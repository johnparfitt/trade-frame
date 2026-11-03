"""Snapshot helpers that read yfinance price history (no network)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.services import market_data


def _history(days: int, splits: dict[str, float] | None = None) -> pd.DataFrame:
    index = pd.bdate_range(end="2026-10-27", periods=days, tz="America/New_York")
    close = [100.0 + (i % 7) for i in range(days)]
    df = pd.DataFrame(
        {
            "Open": close,
            "High": [c + 2 for c in close],
            "Low": [c - 2 for c in close],
            "Close": close,
            "Volume": 1_000_000,
            "Dividends": 0.0,
            "Stock Splits": 0.0,
        },
        index=index,
    )
    for day, ratio in (splits or {}).items():
        df.loc[pd.Timestamp(day, tz="America/New_York"), "Stock Splits"] = ratio
    return df


def test_splits_from_history_returns_dated_ratios():
    hist = _history(250, {"2026-03-02": 0.05, "2026-07-01": 4.0})
    assert market_data._splits_from_history(hist) == [
        (date(2026, 3, 2), 0.05),
        (date(2026, 7, 1), 4.0),
    ]


def test_splits_from_history_without_splits_is_empty():
    assert market_data._splits_from_history(_history(250)) == []


def test_splits_from_history_failed_fetch_is_none():
    assert market_data._splits_from_history(None) is None
    # yfinance can return an empty frame instead of raising.
    assert market_data._splits_from_history(pd.DataFrame()) is None


def test_atr_extension_uses_six_month_window_of_year_history():
    year = _history(250)
    six_months = year[year.index >= year.index[-1] - pd.Timedelta(days=183)]
    assert market_data._compute_atr_pct_50(year, 105.0) == market_data._compute_atr_pct_50(
        six_months, 105.0
    )
    assert market_data._compute_atr_pct_50(year, 105.0) is not None


def test_atr_extension_failed_fetch_is_none():
    assert market_data._compute_atr_pct_50(None, 105.0) is None
    assert market_data._compute_atr_pct_50(pd.DataFrame(), 105.0) is None
