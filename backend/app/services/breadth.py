"""EOD market breadth: compute nine stats from stored daily bars, refresh via Massive.

Watchlist/yfinance is not used here. Counts only — no constituent symbol lists.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import BreadthDay, DailyBar, UniverseTicker
from app.db.session import SessionLocal
from app.services.jobs import JobRunner
from app.services.massive import GroupedBar, MassiveClient, MassiveClientProtocol, redact_secrets

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UNIVERSE_TTL = timedelta(days=7)
MIN_VOLUME_4PCT = 100_000
MIN_DOLLAR_VOL = 250_000
MIN_PRICE_MONTH = 5.0
QUARTER_DAYS = 65
MONTH_DAYS = 20
SMA_FAST = 20
SMA_SLOW = 50

_runner = JobRunner(
    "Breadth refresh",
    redact=redact_secrets,
    logger=logger,
    base_status={"progress_done": 0, "progress_total": 0},
)


@dataclass(frozen=True)
class DayCounts:
    universe_size: int
    up_4: int
    down_4: int
    up_25_q: int | None
    down_25_q: int | None
    up_50_m: int | None
    down_50_m: int | None
    ma_stack: int | None


def today_et() -> date:
    return datetime.now(ET).date()


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _avg_close_x_avg_volume(closes: list[float], volumes: list[float], n: int = MONTH_DAYS) -> float | None:
    if len(closes) < n or len(volumes) < n:
        return None
    return (sum(closes[-n:]) / n) * (sum(volumes[-n:]) / n)


def _passes_dollar_vol(closes: list[float], volumes: list[float]) -> bool:
    product = _avg_close_x_avg_volume(closes, volumes)
    return product is not None and product >= MIN_DOLLAR_VOL


def _pct_change(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return 100.0 * (new - old) / old


def _from_period_low(close: float, period_low: float) -> float | None:
    denom = period_low + 0.01
    if denom == 0:
        return None
    return 100.0 * ((close + 0.01) - denom) / denom


def _from_period_high(close: float, period_high: float) -> float | None:
    denom = period_high + 0.01
    if denom == 0:
        return None
    return 100.0 * ((close + 0.01) - denom) / denom


def rolling_ratio(ups: list[int], downs: list[int]) -> float | None:
    """ups/downs over the same window; None if no down-days in the window."""
    total_down = sum(downs)
    if total_down == 0:
        return None
    return round(sum(ups) / total_down, 2)


def compute_day(
    series: dict[str, tuple[list[date], list[float], list[float]]],
    as_of: date,
) -> DayCounts:
    """Count breadth stats for `as_of`.

    `series` maps symbol -> (session_dates, closes, volumes) oldest-to-newest.
    Only symbols with a bar on `as_of` are in the day's universe.
    """
    up_4 = down_4 = 0
    up_25_q = down_25_q = 0
    up_50_m = down_50_m = 0
    ma_stack = 0
    universe_size = 0
    have_quarter = False
    have_month = False
    have_ma = False

    for _symbol, (dates, closes, volumes) in series.items():
        if not dates or dates[-1] != as_of:
            continue
        universe_size += 1
        n = len(closes)

        if n >= 2:
            pct = _pct_change(closes[-1], closes[-2])
            vol, prev_vol = volumes[-1], volumes[-2]
            if pct is not None and vol >= MIN_VOLUME_4PCT and vol > prev_vol:
                if pct >= 4:
                    up_4 += 1
                elif pct <= -4:
                    down_4 += 1

        liquid = _passes_dollar_vol(closes, volumes)

        if n >= QUARTER_DAYS:
            have_quarter = True
            window = closes[-QUARTER_DAYS:]
            if liquid:
                up_move = _from_period_low(closes[-1], min(window))
                down_move = _from_period_high(closes[-1], max(window))
                if up_move is not None and up_move >= 25:
                    up_25_q += 1
                if down_move is not None and down_move <= -25:
                    down_25_q += 1

        if n >= MONTH_DAYS + 1:
            have_month = True
            prior = closes[-(MONTH_DAYS + 1)]
            if liquid and prior >= MIN_PRICE_MONTH:
                month_pct = _pct_change(closes[-1], prior)
                if month_pct is not None:
                    if month_pct >= 50:
                        up_50_m += 1
                    elif month_pct <= -50:
                        down_50_m += 1

        if n >= SMA_SLOW:
            have_ma = True
            sma20 = _sma(closes, SMA_FAST)
            sma50 = _sma(closes, SMA_SLOW)
            if (
                liquid
                and sma20 is not None
                and sma50 is not None
                and closes[-1] > sma20 > sma50
            ):
                ma_stack += 1

    return DayCounts(
        universe_size=universe_size,
        up_4=up_4,
        down_4=down_4,
        up_25_q=up_25_q if have_quarter else None,
        down_25_q=down_25_q if have_quarter else None,
        up_50_m=up_50_m if have_month else None,
        down_50_m=down_50_m if have_month else None,
        ma_stack=ma_stack if have_ma else None,
    )


def _slice_through(dates: list[date], closes: list[float], volumes: list[float], as_of: date):
    """Trim a series so the last bar is `as_of` (or empty if that date is missing)."""
    try:
        idx = dates.index(as_of)
    except ValueError:
        return [], [], []
    end = idx + 1
    start = max(0, end - QUARTER_DAYS)
    return dates[start:end], closes[start:end], volumes[start:end]


def compute_day_from_full_series(
    full: dict[str, tuple[list[date], list[float], list[float]]],
    as_of: date,
) -> DayCounts:
    sliced: dict[str, tuple[list[date], list[float], list[float]]] = {}
    for symbol, (dates, closes, volumes) in full.items():
        d, c, v = _slice_through(dates, closes, volumes, as_of)
        if d:
            sliced[symbol] = (d, c, v)
    return compute_day(sliced, as_of)


def _set_status(**kwargs) -> None:
    _runner.set_status(**kwargs)


def job_status(*, as_of: date | None = None, has_api_key: bool = False) -> dict:
    snap = _runner.snapshot()
    return {
        "state": snap["state"],
        "progress": {
            "done": snap["progress_done"],
            "total": snap["progress_total"],
        },
        "as_of": as_of,
        "detail": snap["detail"],
        "has_api_key": has_api_key,
    }


def configured_test_tickers() -> list[str]:
    """TEMPORARY (breadth testing): parse TF_BREADTH_TICKERS.

    Unset TF_BREADTH_TICKERS and restore lookback to 90 for a full EOD universe.
    """
    raw = get_settings().breadth_tickers
    if not raw or not str(raw).strip():
        return []
    return [part.strip().upper() for part in str(raw).split(",") if part.strip()]


def has_api_key() -> bool:
    key = get_settings().massive_api_key
    return bool(key and key.strip())


def list_days(db: Session, *, limit: int = 90) -> list[BreadthDay]:
    stmt = select(BreadthDay).order_by(BreadthDay.session_date.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def latest_as_of(db: Session) -> date | None:
    return db.scalar(select(func.max(BreadthDay.session_date)))


def try_start_refresh(
    *,
    session_factory: sessionmaker[Session] | None = None,
    client: MassiveClientProtocol | None = None,
) -> bool:
    """Start a background refresh. Returns False if a job is already running."""
    factory = session_factory or SessionLocal

    def work() -> None:
        db = factory()
        owned_client: MassiveClient | None = None
        try:
            active = client
            if active is None:
                key = (get_settings().massive_api_key or "").strip()
                owned_client = MassiveClient(key)
                active = owned_client
            run_refresh(db, active)
        finally:
            if owned_client is not None:
                owned_client.close()
            db.close()

    return _runner.start(
        work,
        starting={"progress_done": 0, "progress_total": 0, "detail": "Starting"},
    )


def run_refresh(db: Session, client: MassiveClientProtocol) -> None:
    lookback = get_settings().breadth_lookback_days
    _refresh_universe(db, client)
    universe = {row.symbol for row in db.scalars(select(UniverseTicker)).all()}
    if not universe:
        raise RuntimeError("Massive returned no common-stock tickers")

    existing = set(db.scalars(select(distinct(DailyBar.session_date))).all())
    to_fetch = _missing_weekdays(existing, lookback)
    _set_status(progress_done=0, progress_total=len(to_fetch), detail="Fetching daily bars")

    fetched: list[date] = []
    for i, session_date in enumerate(to_fetch, start=1):
        _set_status(
            progress_done=i - 1,
            progress_total=len(to_fetch),
            detail=f"Fetching {session_date.isoformat()}",
        )
        bars = client.grouped_daily(session_date)
        if not bars:
            _set_status(progress_done=i, progress_total=len(to_fetch))
            continue
        _replace_bars_for_date(db, session_date, bars, universe)
        fetched.append(session_date)
        _set_status(progress_done=i, progress_total=len(to_fetch))

    if not fetched:
        bar_count = db.scalar(select(func.count()).select_from(DailyBar)) or 0
        if bar_count == 0:
            raise RuntimeError(
                "No published EOD sessions in lookback. "
                "Massive Basic is EOD-only; unpublished dates (including today) were skipped."
            )

    _prune_old_bars(db, lookback)
    _set_status(detail="Computing breadth")
    _recompute_all(db, universe)
    db.commit()


def _refresh_universe(db: Session, client: MassiveClientProtocol) -> None:
    # TEMPORARY (breadth testing): a configured list skips Massive pagination.
    # Unset TF_BREADTH_TICKERS and restore lookback to 90 for a full EOD universe.
    override = configured_test_tickers()
    if override:
        _set_status(detail="Using TF_BREADTH_TICKERS override")
        _replace_universe(db, override)
        return

    newest = db.scalar(select(func.max(UniverseTicker.refreshed_at)))
    now = datetime.now(timezone.utc)
    count = db.scalar(select(func.count(UniverseTicker.symbol))) or 0
    if newest is not None:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        # Ignore a leftover test-sized universe after TF_BREADTH_TICKERS is unset.
        if now - newest < UNIVERSE_TTL and count >= 1000:
            return
    _set_status(detail="Refreshing ticker universe (page 1)")

    def _on_page(page: int) -> None:
        _set_status(detail=f"Refreshing ticker universe (page {page})")

    symbols = client.common_stock_tickers(on_page=_on_page)
    _replace_universe(db, symbols)


def _replace_universe(db: Session, symbols: list[str]) -> None:
    db.execute(delete(UniverseTicker))
    now = datetime.now(timezone.utc)
    db.bulk_insert_mappings(
        UniverseTicker,
        [{"symbol": s, "refreshed_at": now} for s in symbols],
    )
    db.commit()


def _missing_weekdays(existing: set[date], lookback: int) -> list[date]:
    """Weekdays in the lookback window that we don't already have bars for.

    Walks back from today, skipping weekends, until ``lookback`` weekdays are
    in the window. Holidays still get requested and come back empty from Massive.
    Oldest first so a crash mid-backfill still leaves usable history.

    Existing dates outside this window do not count toward the quota — otherwise
    a full lookback of old sessions would skip newer days after a gap.
    """
    cursor = today_et()
    window: list[date] = []
    floor = cursor - timedelta(days=lookback * 3)
    while cursor >= floor and len(window) < lookback:
        if cursor.weekday() < 5:
            window.append(cursor)
        cursor -= timedelta(days=1)
    return [d for d in reversed(window) if d not in existing]


def _replace_bars_for_date(
    db: Session,
    session_date: date,
    bars: list[GroupedBar],
    universe: set[str],
) -> None:
    db.execute(delete(DailyBar).where(DailyBar.session_date == session_date))
    mappings = [
        {
            "symbol": bar.symbol,
            "session_date": session_date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
        if bar.symbol in universe
    ]
    if mappings:
        db.bulk_insert_mappings(DailyBar, mappings)
    db.commit()


def _prune_old_bars(db: Session, lookback: int) -> None:
    dates = list(
        db.scalars(select(distinct(DailyBar.session_date)).order_by(DailyBar.session_date.desc())).all()
    )
    keep = set(dates[:lookback])
    if len(dates) > lookback:
        db.execute(delete(DailyBar).where(DailyBar.session_date.notin_(keep)))
        db.execute(delete(BreadthDay).where(BreadthDay.session_date.notin_(keep)))
        db.commit()


def _load_series(
    db: Session, universe: set[str]
) -> dict[str, tuple[list[date], list[float], list[float]]]:
    rows = db.scalars(select(DailyBar).order_by(DailyBar.symbol, DailyBar.session_date)).all()
    dates_map: dict[str, list[date]] = defaultdict(list)
    closes_map: dict[str, list[float]] = defaultdict(list)
    volumes_map: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.symbol not in universe:
            continue
        dates_map[row.symbol].append(row.session_date)
        closes_map[row.symbol].append(row.close)
        volumes_map[row.symbol].append(row.volume)
    return {
        symbol: (dates_map[symbol], closes_map[symbol], volumes_map[symbol])
        for symbol in dates_map
    }


def _recompute_all(db: Session, universe: set[str]) -> None:
    dates = list(
        db.scalars(select(distinct(DailyBar.session_date)).order_by(DailyBar.session_date)).all()
    )
    if not dates:
        return
    series = _load_series(db, universe)
    prior_up: list[int] = []
    prior_down: list[int] = []
    now = datetime.now(timezone.utc)
    db.execute(delete(BreadthDay))
    mappings = []
    for session_date in dates:
        counts = compute_day_from_full_series(series, session_date)
        prior_up.append(counts.up_4)
        prior_down.append(counts.down_4)
        ratio_5d = rolling_ratio(prior_up[-5:], prior_down[-5:]) if len(prior_up) >= 5 else None
        ratio_10d = rolling_ratio(prior_up[-10:], prior_down[-10:]) if len(prior_up) >= 10 else None
        mappings.append(
            {
                "session_date": session_date,
                "universe_size": counts.universe_size,
                "up_4": counts.up_4,
                "down_4": counts.down_4,
                "ratio_5d": ratio_5d,
                "ratio_10d": ratio_10d,
                "up_25_q": counts.up_25_q,
                "down_25_q": counts.down_25_q,
                "up_50_m": counts.up_50_m,
                "down_50_m": counts.down_50_m,
                "ma_stack": counts.ma_stack,
                "computed_at": now,
            }
        )
    if mappings:
        db.bulk_insert_mappings(BreadthDay, mappings)


def reset_job_state_for_tests() -> None:
    """Test helper: clear the in-process job flag/status."""
    _runner.reset_for_tests()
