from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ directory (two levels up from this file: app/config.py -> app -> backend)
BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"


class Settings(BaseSettings):
    """Application settings, overridable via environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="TF_", extra="ignore")

    # Storage
    database_url: str = f"sqlite:///{DATA_DIR / 'trade_frame.db'}"

    # Market data / RVOL
    rvol_lookback_days: int = 20  # trailing daily-volume average window (excludes today)

    # In-memory cache TTL (seconds) for enriched symbol snapshots
    snapshot_cache_ttl: int = 30

    # CORS: the Vite dev server origin(s)
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
