from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class SymbolInput(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)

    @field_validator("symbol")
    @classmethod
    def normalize(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be blank")
        return v


class WatchlistRow(BaseModel):
    """A watchlist entry enriched with live market data (computed on read).

    CONTRACT: every field below (except `added_at`) must have a same-named,
    same-nullability field on `SymbolSnapshot` in app/services/market_data.py.
    `app/services/watchlist.py::build_row` builds this object by spreading
    `dataclasses.asdict(snapshot)` into this model, so a name/type mismatch
    will raise at runtime (and is caught in tests/test_schema_sync.py) rather
    than silently dropping data. When adding a new metric, add it to
    SymbolSnapshot AND here (same name) — that's the only duplication left.
    """

    symbol: str
    company: str | None = None
    industry: str | None = None
    last: float | None = None
    pct_change: float | None = None  # today's percent change, e.g. 1.23 == +1.23%
    rvol: float | None = None  # relative volume vs trailing average
    range_pct_52w: float | None = None  # 0-100: position within the 52-week range
    next_earnings: str | None = None  # ISO date of next earnings report
    avg_volume: int | None = None  # 3-month average daily volume
    atr_pct_50: float | None = None  # extension from 50 SMA in ATR%-of-price units
    adr_pct: float | None = None  # 20-day average daily range as % of close
    float_shares: int | None = None
    float_pct: float | None = None  # floatShares / sharesOutstanding as %
    short_pct_float: float | None = None  # % of float shorted
    days_to_cover: float | None = None  # short ratio
    added_at: datetime
    error: str | None = None  # populated if the symbol could not be resolved
