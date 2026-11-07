"""Market data behind the watchlist.

Returns a SymbolSnapshot (company, industry, % change, RVOL) for a symbol,
with a short TTL in-memory cache so polling the watchlist doesn't refetch on
every request. yfinance (no API key) supplies the price metrics.
`gamma_magnet` comes from Unusual Whales via `app.services.gex` and is None
unless `TF_UW_API_KEY` is set. `analyst_net` is the same key via
`app.services.analysts` (upgrades minus downgrades, last 60 days). The SEC
column comes from `app.services.sec_significance`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import yfinance as yf

from app.config import get_settings
from app.services import analysts, gex, sec_significance
from app.services.sec_significance import SecFiling

settings = get_settings()


@dataclass
class SymbolSnapshot:
    """Raw market-data result for one symbol.

    CONTRACT: every field here (except this docstring's exemptions - there are
    none currently) must have a same-named field of the same nullability on
    `WatchlistRow` in app/schemas.py. `watchlist.build_row()` spreads this
    dataclass straight into that Pydantic model by field name, and
    tests/test_schema_sync.py asserts the two stay in lockstep. When adding a
    new metric: add the field here, compute it in `get_snapshot()`, then add
    the same-named field to `WatchlistRow` — no other backend file needs to
    change.
    """

    symbol: str
    company: str | None = None
    industry: str | None = None
    last: float | None = None  # current/last price
    pct_change: float | None = None
    rvol: float | None = None
    range_pct_52w: float | None = None  # 0-100: where price sits in its 52-week range
    next_earnings: str | None = None  # ISO date (YYYY-MM-DD) of the next earnings report
    avg_volume: int | None = None  # 3-month average daily volume
    atr_pct_50: float | None = None  # extension from 50 SMA in ATR%-of-price units
    adr_pct: float | None = None  # 20-day average daily range as % of close
    float_shares: int | None = None  # tradeable float (shares)
    short_pct_float: float | None = None  # % of float shorted
    days_to_cover: float | None = None  # short ratio (days to cover)
    gamma_magnet: float | None = None  # strike with the largest-magnitude net gamma (UW)
    analyst_net: int | None = None  # upgrades minus downgrades, last 60 days (UW)
    analyst_upgrades: tuple[str, ...] = ()  # ids of those upgrades (unseen-dot)
    sec_filing_url: str | None = None  # link to the surfaced (significant) SEC filing
    sec_filing_form: str | None = None  # that filing's form type (e.g. "8-K")
    sec_filing_date: str | None = None  # that filing's ISO date
    sec_significance: int | None = None  # 0-100 LLM significance of that filing
    sec_significance_note: str | None = None  # why it is significant (hover)
    sec_significance_direction: str | None = None  # bullish | bearish | neutral
    sec_filings: tuple[SecFiling, ...] = ()  # every in-window qualifier, newest first
    error: str | None = None


# Very small thread-safe TTL cache: symbol -> (expires_at, snapshot)
_cache: dict[str, tuple[float, SymbolSnapshot]] = {}
_cache_lock = threading.Lock()


def _cache_get(symbol: str) -> SymbolSnapshot | None:
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        expires_at, snapshot = entry
        if time.monotonic() >= expires_at:
            _cache.pop(symbol, None)
            return None
        return snapshot


def _cache_put(symbol: str, snapshot: SymbolSnapshot) -> None:
    with _cache_lock:
        _cache[symbol] = (time.monotonic() + settings.snapshot_cache_ttl, snapshot)


def drop_snapshot(symbol: str) -> None:
    """Drop one symbol so the next watchlist read rebuilds it."""
    with _cache_lock:
        _cache.pop(symbol.strip().upper(), None)


def _fi_get(fi, *keys: str):
    """Read a value from a yfinance fast_info object, tolerating key variants."""
    for key in keys:
        val = None
        try:
            val = fi.get(key)
        except Exception:
            val = None
        if val is None:
            val = getattr(fi, key, None)
        if val is not None:
            return val
    return None


def _as_float(val) -> float | None:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _pct_change_from_history(ticker: yf.Ticker) -> float | None:
    try:
        hist = ticker.history(period="5d", interval="1d")
        closes = hist["Close"].dropna()
        if len(closes) >= 2:
            prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
            if prev:
                return round((last - prev) / prev * 100.0, 2)
    except Exception:
        pass
    return None


def _compute_pct_change(ticker: yf.Ticker, last: float | None, prev: float | None) -> float | None:
    if last and prev:
        return round((last - prev) / prev * 100.0, 2)
    return _pct_change_from_history(ticker)


def _compute_range_position(last: float | None, high: float | None, low: float | None) -> float | None:
    if last and high and low and high > low:
        pct = (last - low) / (high - low) * 100.0
        return round(max(0.0, min(100.0, pct)), 1)
    return None


def _extract_float_metrics(info: dict) -> tuple[int | None, float | None, float | None]:
    """Return (float_shares, short_pct_float, days_to_cover)."""
    float_shares = info.get("floatShares")
    short_pct = info.get("shortPercentOfFloat")
    short_ratio = info.get("shortRatio")

    float_shares_int = int(float_shares) if float_shares else None
    short_pct_float = round(float(short_pct) * 100.0, 1) if short_pct is not None else None
    days_to_cover = round(float(short_ratio), 1) if short_ratio is not None else None

    return float_shares_int, short_pct_float, days_to_cover


def _next_earnings(info: dict) -> str | None:
    """Earliest earnings timestamp that is in the future; else the most recent one."""
    candidates = [
        info.get("earningsTimestampStart"),
        info.get("earningsTimestamp"),
        info.get("earningsTimestampEnd"),
    ]
    ts = [c for c in candidates if isinstance(c, (int, float)) and c > 0]
    if not ts:
        return None
    now = datetime.now(timezone.utc).timestamp()
    future = sorted(c for c in ts if c >= now)
    chosen = future[0] if future else max(ts)
    try:
        return datetime.fromtimestamp(chosen, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _year_history(ticker: yf.Ticker) -> Any:
    """One year of daily bars for the ATR metric."""
    try:
        return ticker.history(period="1y", interval="1d")
    except Exception:
        return None


def _compute_atr_pct_50(hist: Any, last_close: float | None) -> float | None:
    """Extension from 50 SMA measured in ATR%-of-price units, over ~6 months of ``hist``.

    - ma50 = 50-day SMA of close
    - atr14 = 14-period Wilder ATR
    - atr_pct = atr14 / last_close
    - gain_from_ma50 = (last_close - ma50) / ma50
    - atr_pct_50 = gain_from_ma50 / atr_pct
    """
    try:
        if hist is None or hist.empty:
            return None
        # Wilder smoothing depends on the starting bar, so keep the 6-month
        # window this metric has always used.
        hist = hist[hist.index >= hist.index[-1] - timedelta(days=183)]
        hist = hist.dropna(subset=["High", "Low", "Close"])
        if len(hist) < 64:
            return None

        closes = hist["Close"].astype(float)
        highs = hist["High"].astype(float)
        lows = hist["Low"].astype(float)

        last = last_close if last_close else float(closes.iloc[-1])
        if not last:
            return None

        ma50 = float(closes.iloc[-50:].mean())
        if not ma50:
            return None

        c_vals = closes.tolist()
        h_vals = highs.tolist()
        l_vals = lows.tolist()
        trs: list[float] = []
        for i in range(1, len(c_vals)):
            tr = max(
                h_vals[i] - l_vals[i],
                abs(h_vals[i] - c_vals[i - 1]),
                abs(l_vals[i] - c_vals[i - 1]),
            )
            trs.append(tr)

        if len(trs) < 14:
            return None

        atr = sum(trs[:14]) / 14
        for tr in trs[14:]:
            atr = (atr * 13 + tr) / 14

        atr_pct = atr / last
        if not atr_pct:
            return None

        gain_from_ma50 = (last - ma50) / ma50
        return round(gain_from_ma50 / atr_pct, 1)
    except Exception:
        return None


def _compute_adr_pct(ticker: yf.Ticker, lookback: int = 20) -> float | None:
    """ADR% = average daily (High - Low) / Close over lookback days, as a percentage."""
    try:
        hist = ticker.history(period="3mo", interval="1d")
        hist = hist.dropna(subset=["High", "Low", "Close"])
        if len(hist) < lookback:
            return None
        recent = hist.iloc[-lookback:]
        daily_range_pct = (recent["High"] - recent["Low"]) / recent["Close"] * 100.0
        return round(float(daily_range_pct.mean()), 1)
    except Exception:
        return None


def _compute_rvol(ticker: yf.Ticker) -> float | None:
    """RVOL = current-day volume / trailing average daily volume (excluding today)."""
    lookback = settings.rvol_lookback_days
    try:
        # Grab a bit more than the lookback to be safe with holidays/weekends.
        hist = ticker.history(period=f"{lookback + 15}d", interval="1d")
        vols = hist["Volume"].dropna()
        vols = vols[vols > 0]
        if len(vols) < 2:
            return None
        today_vol = float(vols.iloc[-1])
        prior = vols.iloc[-(lookback + 1):-1] if len(vols) > lookback else vols.iloc[:-1]
        avg = float(prior.mean())
        if not avg:
            return None
        return round(today_vol / avg, 2)
    except Exception:
        return None


def get_snapshot(symbol: str) -> SymbolSnapshot:
    symbol = symbol.strip().upper()
    cached = _cache_get(symbol)
    if cached is not None:
        return cached

    snapshot = SymbolSnapshot(symbol=symbol)
    try:
        ticker = yf.Ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        fi = ticker.fast_info

        company = info.get("longName") or info.get("shortName")
        industry = info.get("industry") or info.get("sector")

        last = _as_float(_fi_get(fi, "lastPrice", "last_price")) or _as_float(
            info.get("currentPrice") or info.get("regularMarketPrice")
        )
        prev = _as_float(
            _fi_get(fi, "previousClose", "previous_close", "regularMarketPreviousClose")
        ) or _as_float(info.get("previousClose") or info.get("regularMarketPreviousClose"))

        pct_change = _compute_pct_change(ticker, last, prev)
        rvol = _compute_rvol(ticker)
        year_history = _year_history(ticker)
        atr_pct_50 = _compute_atr_pct_50(year_history, last)
        adr_pct = _compute_adr_pct(ticker)

        high52 = _as_float(_fi_get(fi, "yearHigh")) or _as_float(info.get("fiftyTwoWeekHigh"))
        low52 = _as_float(_fi_get(fi, "yearLow")) or _as_float(info.get("fiftyTwoWeekLow"))
        avg_volume = info.get("averageVolume") or _fi_get(fi, "threeMonthAverageVolume")
        float_shares, short_pct_float, days_to_cover = _extract_float_metrics(info)

        # If we got nothing back at all, treat as an unresolved symbol.
        if company is None and pct_change is None and rvol is None and last is None:
            snapshot.error = "Could not resolve symbol"
        else:
            snapshot.company = company
            snapshot.industry = industry
            snapshot.last = round(last, 4) if last else None
            snapshot.pct_change = pct_change
            snapshot.rvol = rvol
            snapshot.range_pct_52w = _compute_range_position(last, high52, low52)
            snapshot.next_earnings = _next_earnings(info)
            snapshot.avg_volume = int(avg_volume) if avg_volume else None
            snapshot.atr_pct_50 = atr_pct_50
            snapshot.adr_pct = adr_pct
            snapshot.float_shares = float_shares
            snapshot.short_pct_float = short_pct_float
            snapshot.days_to_cover = days_to_cover
            levels = gex.get_levels(symbol)
            snapshot.gamma_magnet = levels.gamma_magnet
            analyst = analysts.get_score(symbol)
            snapshot.analyst_net = analyst.net
            snapshot.analyst_upgrades = analyst.upgrades
            sig = sec_significance.lookup(symbol)
            snapshot.sec_filing_url = sig.url
            snapshot.sec_filing_form = sig.form
            snapshot.sec_filing_date = sig.date
            snapshot.sec_significance = sig.score
            snapshot.sec_significance_note = sig.note
            snapshot.sec_significance_direction = sig.direction
            snapshot.sec_filings = sig.filings
    except Exception as exc:  # pragma: no cover - defensive
        snapshot.error = f"Lookup failed: {exc}"

    _cache_put(symbol, snapshot)
    return snapshot


def symbol_exists(symbol: str) -> bool:
    """Lightweight validation that a symbol resolves in yfinance."""
    snap = get_snapshot(symbol)
    return snap.error is None
