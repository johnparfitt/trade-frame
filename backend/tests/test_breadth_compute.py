"""Pure breadth count tests — no DB, no Massive."""

from __future__ import annotations

from datetime import date, timedelta

from app.services.breadth import _missing_weekdays, compute_day, rolling_ratio

START = date(2024, 1, 2)


def _days(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def _series(closes: list[float], volumes: list[float] | None = None):
    n = len(closes)
    vols = volumes if volumes is not None else [200_000.0] * n
    return (_days(n), closes, vols)


def test_rolling_ratio_none_when_no_downs():
    assert rolling_ratio([10, 20], [0, 0]) is None
    assert rolling_ratio([10, 20], [5, 5]) == 3.0


def test_up_and_down_4pct_require_volume_up_and_min_size():
    as_of = START + timedelta(days=1)
    series = {
        # +4.1% on rising volume >= 100k
        "UP": _series([100.0, 104.1], [150_000, 200_000]),
        # +4.1% but volume too small
        "THIN": _series([100.0, 104.1], [90_000, 95_000]),
        # +4.1% but volume fell
        "FADE": _series([100.0, 104.1], [300_000, 200_000]),
        # -4.1% on rising volume
        "DN": _series([100.0, 95.9], [150_000, 200_000]),
        # +3.9% — below the 4% cut
        "SMALL": _series([100.0, 103.9], [150_000, 200_000]),
    }
    counts = compute_day(series, as_of)
    assert counts.universe_size == 5
    assert counts.up_4 == 1
    assert counts.down_4 == 1
    assert counts.up_25_q is None
    assert counts.up_50_m is None
    assert counts.ma_stack is None


def test_quarter_25_from_65day_low_and_high():
    n = 65
    # Climb from 80 to 101: more than +25% from the period low.
    up_closes = [80.0 + (21.0 * i / (n - 1)) for i in range(n)]
    # Fall from 100 to 74: more than -25% from the period high.
    down_closes = [100.0 - (26.0 * i / (n - 1)) for i in range(n)]
    # Same path as up but tiny volume — excluded by dollar-volume filter.
    thin_closes = list(up_closes)
    as_of = START + timedelta(days=n - 1)
    series = {
        "UPQ": _series(up_closes, [200_000.0] * n),
        "DNQ": _series(down_closes, [200_000.0] * n),
        "THIN": _series(thin_closes, [1.0] * n),
    }
    counts = compute_day(series, as_of)
    assert counts.up_25_q == 1
    assert counts.down_25_q == 1
    assert counts.universe_size == 3


def test_month_50_vs_close_20_days_ago():
    n = 21  # today + 20 days ago
    up_closes = [10.0] * 20 + [15.1]  # +51% vs C20, C20 >= 5
    down_closes = [20.0] * 20 + [9.9]  # -50.5%
    cheap = [4.0] * 20 + [8.0]  # +100% but C20 < $5
    as_of = START + timedelta(days=n - 1)
    series = {
        "UPM": _series(up_closes),
        "DNM": _series(down_closes),
        "CHEAP": _series(cheap),
    }
    counts = compute_day(series, as_of)
    assert counts.up_50_m == 1
    assert counts.down_50_m == 1


def test_ma_stack_close_gt_sma20_gt_sma50():
    n = 50
    stacked = [float(i + 1) for i in range(n)]  # 1..50, close > SMA20 > SMA50
    inverted = [float(n - i) for i in range(n)]  # 50..1, not stacked
    as_of = START + timedelta(days=n - 1)
    series = {
        "BULL": _series(stacked),
        "BEAR": _series(inverted),
    }
    counts = compute_day(series, as_of)
    assert counts.ma_stack == 1


def test_symbol_without_bar_on_as_of_is_excluded():
    as_of = START + timedelta(days=2)
    series = {
        "TODAY": (_days(3), [10.0, 10.0, 10.0], [200_000.0] * 3),
        "STALE": (_days(2), [10.0, 10.0], [200_000.0] * 2),
    }
    counts = compute_day(series, as_of)
    assert counts.universe_size == 1


def test_missing_weekdays_fetches_gap_when_lookback_already_full(monkeypatch):
    """Old stored sessions must not block newer weekdays after a gap."""
    today = date(2026, 9, 22)  # Tuesday
    monkeypatch.setattr("app.services.breadth.today_et", lambda: today)
    existing = {
        date(2026, 9, 11),
        date(2026, 9, 14),
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 17),
    }
    assert _missing_weekdays(existing, lookback=5) == [
        date(2026, 9, 18),
        date(2026, 9, 21),
        date(2026, 9, 22),
    ]


def test_missing_weekdays_empty_when_window_already_stored(monkeypatch):
    today = date(2026, 9, 17)  # Thursday
    monkeypatch.setattr("app.services.breadth.today_et", lambda: today)
    existing = {
        date(2026, 9, 11),
        date(2026, 9, 14),
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 17),
    }
    assert _missing_weekdays(existing, lookback=5) == []


def test_missing_weekdays_backfills_from_empty(monkeypatch):
    today = date(2024, 6, 5)  # Wednesday
    monkeypatch.setattr("app.services.breadth.today_et", lambda: today)
    assert _missing_weekdays(set(), lookback=3) == [
        date(2024, 6, 3),
        date(2024, 6, 4),
        date(2024, 6, 5),
    ]
