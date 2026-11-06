from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import (
    SecBrief,
    SecReview,
    SecReviewRequest,
    SecScoreApplied,
    SecScoreOverride,
    SymbolInput,
    WatchlistRow,
)
from app.services import market_data, sec_significance, watchlist

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


@router.post("/{symbol}/sec-brief", response_model=SecBrief)
def sec_brief(symbol: str) -> SecBrief:
    """Combined read of this symbol's significant filings. Not part of a refresh."""
    try:
        brief = sec_significance.cluster_brief(symbol)
    except sec_significance.InsufficientFilings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fewer than two significant filings in the window.",
        ) from None
    except sec_significance.BriefUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Combined filing analysis is unavailable.",
        ) from None
    return SecBrief(text=brief.text, generated_at=brief.generated_at)


@router.post("/{symbol}/sec-review", response_model=SecReview)
def sec_review(symbol: str, payload: SecReviewRequest) -> SecReview:
    """Second read of one filing. Does not change the stored score."""
    try:
        review = sec_significance.review_filing(symbol, payload.url)
    except sec_significance.FilingNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That filing is not in the current window.",
        ) from None
    except sec_significance.ReviewUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Filing review is unavailable.",
        ) from None
    return SecReview(
        agrees=review.agrees,
        score=review.score,
        direction=review.direction,
        reason=review.reason,
        will_show=review.will_show,
    )


@router.post("/{symbol}/sec-review/apply", response_model=SecScoreApplied)
def apply_sec_review(symbol: str, payload: SecScoreOverride) -> SecScoreApplied:
    """Replace the stored score with the second read the user confirmed."""
    try:
        applied = sec_significance.apply_review(
            symbol,
            payload.url,
            (payload.score, payload.direction, payload.reason),
        )
    except sec_significance.FilingNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That filing is not in the current window.",
        ) from None
    except sec_significance.ReviewUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not store the replacement score.",
        ) from None
    return SecScoreApplied(
        score=applied.score,
        direction=applied.direction,
        reason=applied.reason,
        will_show=applied.will_show,
    )


@router.delete("/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_watchlist(symbol: str, db: Session = Depends(get_db)) -> None:
    removed = watchlist.remove_symbol(db, symbol)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Symbol '{symbol.upper()}' is not on the watchlist.",
        )
