"""Daily Market Report: fetch the UW digest, analyze it with an LLM, persist.

Manual trigger only (a Generate button, like Breadth) — no scheduler, per
AGENTS.md. The generation runs on a background thread and reports progress via
an in-process status dict polled by ``GET /api/report/status``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import DailyReport
from app.db.session import SessionLocal
from app.services import llm
from app.services.jobs import JobRunner
from app.services.uw import (
    UnusualWhalesClient,
    UnusualWhalesClientProtocol,
    redact_secrets,
)

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

AnalyzeFn = Callable[[dict[str, Any]], str]

_runner = JobRunner("Daily report generation", redact=redact_secrets, logger=logger)


def today_et() -> date:
    return datetime.now(ET).date()


def _set_status(**kwargs) -> None:
    _runner.set_status(**kwargs)


def job_status(*, as_of: date | None = None) -> dict:
    snap = _runner.snapshot()
    return {
        "state": snap["state"],
        "detail": snap["detail"],
        "as_of": as_of,
        "has_uw_key": has_uw_key(),
        "has_llm_key": has_llm_key(),
    }


def has_uw_key() -> bool:
    key = get_settings().uw_api_key
    return bool(key and key.strip())


def has_llm_key() -> bool:
    key = get_settings().anthropic_api_key
    return bool(key and key.strip())


def list_reports(db: Session, *, limit: int = 30) -> list[DailyReport]:
    stmt = select(DailyReport).order_by(DailyReport.session_date.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def latest_as_of(db: Session) -> date | None:
    return db.scalar(select(func.max(DailyReport.session_date)))


def _report_date(data: dict[str, Any]) -> date:
    raw = data.get("date")
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return today_et()


def _is_zero_dte(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        try:
            return float(value.strip()) == 0
        except ValueError:
            return False
    return False


def without_zero_dte_unusual_options(data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of the digest with 0-DTE unusual_options rows dropped."""
    payload = dict(data)
    rows = payload.get("unusual_options")
    if not isinstance(rows, list):
        return payload
    payload["unusual_options"] = [
        row for row in rows if not (isinstance(row, dict) and _is_zero_dte(row.get("dte")))
    ]
    return payload


def get_raw_json(db: Session, session_date: date) -> str | None:
    row = db.get(DailyReport, session_date)
    if row is None:
        return None
    return row.raw_json


def run_generate(
    db: Session,
    client: UnusualWhalesClientProtocol,
    *,
    analyze: AnalyzeFn | None = None,
) -> DailyReport:
    """Fetch the UW daily digest, analyze it, and upsert one DailyReport row."""
    analyze_fn = analyze if analyze is not None else (lambda data: llm.analyze(data))
    _set_status(detail="Fetching Unusual Whales daily report")
    data = client.daily_report()
    if not data:
        raise RuntimeError("Unusual Whales returned an empty daily report.")

    payload = without_zero_dte_unusual_options(data)
    _set_status(detail="Analyzing with LLM")
    analysis = analyze_fn(payload)

    session_date = _report_date(payload)
    now = datetime.now(timezone.utc)
    model = get_settings().llm_model

    db.execute(delete(DailyReport).where(DailyReport.session_date == session_date))
    row = DailyReport(
        session_date=session_date,
        generated_at=now,
        model=model,
        analysis=analysis,
        raw_json=json.dumps(payload, default=str),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def try_start_generate(
    *,
    session_factory: sessionmaker[Session] | None = None,
    client: UnusualWhalesClientProtocol | None = None,
    analyze: AnalyzeFn | None = None,
) -> bool:
    """Start a background generation. Returns False if a job is already running."""
    factory = session_factory or SessionLocal

    def work() -> None:
        db = factory()
        owned_client: UnusualWhalesClient | None = None
        try:
            active = client
            if active is None:
                key = (get_settings().uw_api_key or "").strip()
                owned_client = UnusualWhalesClient(key)
                active = owned_client
            run_generate(db, active, analyze=analyze)
        finally:
            if owned_client is not None:
                owned_client.close()
            db.close()

    return _runner.start(work, starting={"detail": "Starting"})


def reset_job_state_for_tests() -> None:
    """Test helper: clear the in-process job flag/status."""
    _runner.reset_for_tests()
