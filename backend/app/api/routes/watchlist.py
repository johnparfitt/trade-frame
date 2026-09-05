from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import SymbolInput, WatchlistRow
from app.services import market_data, watchlist

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("", response_model=list[WatchlistRow])
def get_watchlist(db: Session = Depends(get_db)) -> list[WatchlistRow]:
    return watchlist.enriched_rows(db)


@router.post("", response_model=WatchlistRow, status_code=status.HTTP_201_CREATED)
def add_to_watchlist(payload: SymbolInput, db: Session = Depends(get_db)) -> WatchlistRow:
    if not market_data.symbol_exists(payload.symbol):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Symbol '{payload.symbol}' could not be resolved.",
        )
    item = watchlist.add_symbol(db, payload.symbol)
    snap = market_data.get_snapshot(item.symbol)
    return watchlist.build_row(item, snap)


@router.delete("/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_watchlist(symbol: str, db: Session = Depends(get_db)) -> None:
    removed = watchlist.remove_symbol(db, symbol)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Symbol '{symbol.upper()}' is not on the watchlist.",
        )
