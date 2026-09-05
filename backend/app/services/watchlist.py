"""Watchlist CRUD + enrichment."""

from __future__ import annotations

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


def enriched_rows(db: Session) -> list[WatchlistRow]:
    rows: list[WatchlistRow] = []
    for item in list_symbols(db):
        snap = market_data.get_snapshot(item.symbol)
        rows.append(build_row(item, snap))
    return rows
