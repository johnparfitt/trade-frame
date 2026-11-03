from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UniverseTicker(Base):
    """US common stocks used as the breadth universe (Massive type=CS)."""

    __tablename__ = "universe_tickers"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyBar(Base):
    """EOD OHLCV for one symbol/session, sourced from Massive grouped daily."""

    __tablename__ = "daily_bars"
    __table_args__ = (Index("ix_daily_bars_session_date", "session_date"),)

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)


class BreadthDay(Base):
    """One trading day's breadth counts, computed from stored daily bars."""

    __tablename__ = "breadth_days"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    universe_size: Mapped[int] = mapped_column(Integer, nullable=False)
    up_4: Mapped[int] = mapped_column(Integer, nullable=False)
    down_4: Mapped[int] = mapped_column(Integer, nullable=False)
    ratio_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    ratio_10d: Mapped[float | None] = mapped_column(Float, nullable=True)
    up_25_q: Mapped[int | None] = mapped_column(Integer, nullable=True)
    down_25_q: Mapped[int | None] = mapped_column(Integer, nullable=True)
    up_50_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    down_50_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ma_stack: Mapped[int | None] = mapped_column(Integer, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecSignificanceScore(Base):
    """LLM significance score for one SEC filing (filings are immutable).

    ``prompt_key`` identifies the prompt + model that produced the score; a
    lookup under a different key is a miss, so editing the prompt re-scores.
    """

    __tablename__ = "sec_significance_scores"

    accession: Mapped[str] = mapped_column(String(30), primary_key=True)
    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecCachedResult(Base):
    """A persisted SEC lookup result (a filing check or parsed company facts).

    ``version`` hashes the rules that produced ``payload`` (JSON); a lookup
    under a different version is a miss, so editing keywords or tags re-checks.
    """

    __tablename__ = "sec_cached_results"

    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyReport(Base):
    """One trading day's Unusual Whales digest and its LLM analysis."""

    __tablename__ = "daily_reports"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    analysis: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
