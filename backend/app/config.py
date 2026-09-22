from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ directory (two levels up from this file: app/config.py -> app -> backend)
BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"


class Settings(BaseSettings):
    """Application settings, overridable via environment variables or a .env file."""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_prefix="TF_",
        extra="ignore",
    )

    # Storage
    database_url: str = f"sqlite:///{DATA_DIR / 'trade_frame.db'}"

    # Market data / RVOL
    rvol_lookback_days: int = 20  # trailing daily-volume average window (excludes today)

    # In-memory cache TTL (seconds) for enriched symbol snapshots
    snapshot_cache_ttl: int = 30

    # Breadth pane (Massive EOD grouped daily). Empty/unset disables refresh.
    massive_api_key: str | None = None
    breadth_lookback_days: int = 90
    # TEMPORARY (breadth testing): comma-separated symbols skip Massive's full
    # CS ticker list. Unset this and keep lookback at 90 for a full EOD run.
    breadth_tickers: str | None = None

    # Daily Market Report pane (Unusual Whales digest + LLM analysis).
    # Empty/unset disables the Generate button.
    uw_api_key: str | None = None
    anthropic_api_key: str | None = None
    # LLM model behind a single config knob so the provider/model is swappable.
    llm_model: str = "claude-sonnet-4-5"

    # CORS: the Vite dev server origin(s)
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
