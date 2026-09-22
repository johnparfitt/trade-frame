from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas_common import JobState


class ReportOut(BaseModel):
    session_date: date
    generated_at: datetime
    model: str
    analysis: str


class ReportStatus(BaseModel):
    state: JobState = Field(description="idle | running | error")
    detail: str | None = None
    as_of: date | None = None
    has_uw_key: bool = False
    has_llm_key: bool = False
