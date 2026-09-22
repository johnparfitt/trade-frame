from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db.models import DailyReport
from app.db.session import get_db
from app.schemas_report import ReportOut, ReportStatus
from app.services import report

router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("", response_model=list[ReportOut])
def get_reports(db: Session = Depends(get_db)) -> list[DailyReport]:
    return report.list_reports(db)


@router.get("/status", response_model=ReportStatus)
def get_report_status(db: Session = Depends(get_db)) -> ReportStatus:
    payload = report.job_status(as_of=report.latest_as_of(db))
    return ReportStatus.model_validate(payload)


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED, response_model=ReportStatus)
def generate_report(db: Session = Depends(get_db)) -> ReportStatus:
    if not report.has_uw_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unusual Whales API key is not configured (set TF_UW_API_KEY).",
        )
    if not report.has_llm_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Anthropic API key is not configured (set TF_ANTHROPIC_API_KEY).",
        )
    started = report.try_start_generate()
    if not started:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A report generation is already running.",
        )
    payload = report.job_status(as_of=report.latest_as_of(db))
    return ReportStatus.model_validate(payload)


@router.get("/{session_date}/raw")
def get_report_raw(session_date: date, db: Session = Depends(get_db)) -> Response:
    raw = report.get_raw_json(db, session_date)
    if raw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No report for that date.")
    return Response(content=raw, media_type="application/json")
