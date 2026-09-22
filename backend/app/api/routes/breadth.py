from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.models import BreadthDay
from app.db.session import get_db
from app.schemas_breadth import BreadthDayOut, BreadthStatus
from app.services import breadth

router = APIRouter(prefix="/api/breadth", tags=["breadth"])


@router.get("", response_model=list[BreadthDayOut])
def get_breadth(db: Session = Depends(get_db)) -> list[BreadthDay]:
    return breadth.list_days(db)


@router.get("/status", response_model=BreadthStatus)
def get_breadth_status(db: Session = Depends(get_db)) -> BreadthStatus:
    payload = breadth.job_status(
        as_of=breadth.latest_as_of(db),
        has_api_key=breadth.has_api_key(),
    )
    return BreadthStatus.model_validate(payload)


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED, response_model=BreadthStatus)
def refresh_breadth(db: Session = Depends(get_db)) -> BreadthStatus:
    if not breadth.has_api_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Massive API key is not configured (set TF_MASSIVE_API_KEY).",
        )
    started = breadth.try_start_refresh()
    if not started:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A breadth refresh is already running.",
        )
    payload = breadth.job_status(
        as_of=breadth.latest_as_of(db),
        has_api_key=True,
    )
    return BreadthStatus.model_validate(payload)
