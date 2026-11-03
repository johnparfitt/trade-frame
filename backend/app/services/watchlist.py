"""Watchlist CRUD + enrichment."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import WatchlistItem
from app.schemas import WatchlistRow
from app.services import market_data
from app.services.market_data import SymbolSnapshot


def build_row(item: WatchlistItem, snap: SymbolSnapshot) -> WatchlistRow:
    """Build the API row from a watchlist item + its market-data snapshot.

    IMPORTANT: `WatchlistRow` (app/schemas.py) is intentionally kept field-for-field
    identical to `SymbolSnapshot` (app/services/market_data.py), plus `added_at`.
    This function relies on that: it spreads every snapshot field through by name
    instead of listing them one by one, so adding a new metric can never be
    "forgotten" here. If you add a field to SymbolSnapshot, add the same-named
    field to WatchlistRow and this function keeps working with no changes.
    `tests/test_schema_sync.py` enforces that the two stay in sync.
    """
    return WatchlistRow(**asdict(snap), added_at=item.added_at)


def list_symbols(db: Session) -> list[WatchlistItem]:
    stmt = select(WatchlistItem).order_by(WatchlistItem.symbol.asc())
    return list(db.execute(stmt).scalars().all())


def get_item(db: Session, symbol: str) -> WatchlistItem | None:
    stmt = select(WatchlistItem).where(WatchlistItem.symbol == symbol.upper())
    return db.execute(stmt).scalar_one_or_none()


def add_symbol(db: Session, symbol: str) -> WatchlistItem:
    symbol = symbol.strip().upper()
    existing = get_item(db, symbol)
    if existing is not None:
        return existing
    item = WatchlistItem(symbol=symbol)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def remove_symbol(db: Session, symbol: str) -> bool:
    item = get_item(db, symbol)
    if item is None:
        return False
    db.delete(item)
    db.commit()
    return True


# Snapshots are network-bound (yfinance, UW, EDGAR, LLM), so a cold watchlist
# loads several symbols at once. Kept small: each symbol makes two UW calls and
# EDGAR's shared throttle caps it at 8 req/s regardless.
_MAX_WORKERS = 4
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="snapshot")
# symbol -> in-flight lookup, so overlapping refreshes share one fetch per symbol.
_inflight: dict[str, Future[SymbolSnapshot]] = {}
_inflight_lock = threading.Lock()


def _forget(symbol: str, future: Future[SymbolSnapshot]) -> None:
    with _inflight_lock:
        if _inflight.get(symbol) is future:
            del _inflight[symbol]


def _snapshot_future(symbol: str) -> Future[SymbolSnapshot]:
    with _inflight_lock:
        future = _inflight.get(symbol)
        created = future is None
        if future is None:
            future = _executor.submit(market_data.get_snapshot, symbol)
            _inflight[symbol] = future
    if created:
        # Outside the lock: an already-finished future runs the callback inline.
        future.add_done_callback(lambda f: _forget(symbol, f))
    return future


def enriched_rows(db: Session) -> list[WatchlistRow]:
    items = list_symbols(db)
    futures = [_snapshot_future(item.symbol) for item in items]
    rows: list[WatchlistRow] = []
    for item, future in zip(items, futures):
        try:
            snap = future.result()
        except Exception as exc:  # get_snapshot never raises; keep one bad row from failing all
            snap = SymbolSnapshot(symbol=item.symbol, error=f"Lookup failed: {exc}")
        rows.append(build_row(item, snap))
    return rows
