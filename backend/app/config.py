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

    # In-memory cache TTL (seconds) for the watchlist gamma magnet. Longer than
    # the snapshot TTL because each miss
    # costs one Unusual Whales call per symbol.
    gex_cache_ttl: int = 900

    # SEC EDGAR (watchlist Dilution Risk + SEC filing columns). Free, no key,
    # but EDGAR requires a descriptive User-Agent with contact info and caps
    # callers at 10 req/s — set TF_SEC_USER_AGENT to your own contact.
    sec_user_agent: str = "trade-frame/1.0 (contact: set TF_SEC_USER_AGENT)"
    # In-memory cache TTL (seconds) for SEC EDGAR lookups. 6h by default because
    # filings update at most daily and each miss costs 2-3 EDGAR calls per symbol.
    sec_cache_ttl: int = 21600
    # SEC filing significance (watchlist SEC column). An LLM reads the filing and
    # scores it 0-100; only filings scoring above the minimum are surfaced, so the
    # column stays blank on quiet days. Set enabled=False to use the deterministic
    # form/8-K-item heuristic only (no LLM calls); needs TF_ANTHROPIC_API_KEY when on.
    sec_significance_enabled: bool = True
    sec_significance_min: int = 50
    # Filings older than this many days are ignored (column stays blank), so the
    # SEC column only ever surfaces a genuinely recent event.
    sec_significance_recent_days: int = 14

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
