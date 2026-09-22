from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas_common import JobState


class BreadthDayOut(BaseModel):
    session_date: date
    universe_size: int
    up_4: int
    down_4: int
    ratio_5d: float | None = None
    ratio_10d: float | None = None
    up_25_q: int | None = None
    down_25_q: int | None = None
    up_50_m: int | None = None
    down_50_m: int | None = None
    ma_stack: int | None = None
    computed_at: datetime


class BreadthProgress(BaseModel):
    done: int = 0
    total: int = 0


class BreadthStatus(BaseModel):
    state: JobState = Field(description="idle | running | error")
    progress: BreadthProgress = Field(default_factory=BreadthProgress)
    as_of: date | None = None
    detail: str | None = None
    has_api_key: bool = False
