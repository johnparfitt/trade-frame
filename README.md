# trade-frame

Local single-user stock trading pane of glass. Agent rules: `AGENTS.md`.

## Layout

- `backend/` — FastAPI + SQLAlchemy + SQLite.
- `frontend/` — React + Vite + TypeScript + Tailwind + TanStack Query.

## Running locally

Two processes. Start the backend first.

### Backend (port 8000)

```bash
cd backend
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

- Health: http://127.0.0.1:8000/api/health
- API docs: http://127.0.0.1:8000/docs

### Frontend (port 5173)

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. Vite proxies `/api` to port 8000.

## Configuration

Override via `TF_` env vars or `backend/.env` (gitignored; copy from `backend/.env.example`):

- `TF_DATABASE_URL` (default SQLite under `backend/data/`)
- `TF_RVOL_LOOKBACK_DAYS` (default `20`)
- `TF_SNAPSHOT_CACHE_TTL` (seconds, default `30`)
- `TF_GEX_CACHE_TTL` (seconds, default `900`; cache for the watchlist Gamma Magnet level — each miss is one Unusual Whales call per symbol)
- `TF_SEC_USER_AGENT` (watchlist SEC column; SEC EDGAR requires a descriptive User-Agent with contact info — set this to your own, e.g. `trade-frame/1.0 (you@example.com)`).
- `TF_SEC_CACHE_TTL` (seconds, default `21600` = 6h; cache for successful EDGAR filing-list lookups). A failed lookup is not cached, so the next snapshot retries.
- `TF_SEC_SIGNIFICANCE_ENABLED` (default `true`), `TF_SEC_SIGNIFICANCE_MIN` (default `50`), and `TF_SEC_SIGNIFICANCE_RECENT_DAYS` (default `30`): the `SEC` column is an LLM-scored 0-100 filing-significance metric. The number is the newest filing that scores above the minimum and was filed within the window (color: red bearish, green bullish, amber neutral). Clicking the score lists every other filing in that window that also clears the bar. The column stays blank on quiet days. A pulsing dot marks an unseen filing from the last 7 days. Opening one filing in that list marks only that one. Mark as read, or a successful "Analyze together", marks every filing in the cell seen and removes the dot; the score stays. Remove hides those filings from the column until a new one arrives. "Analyze together" asks the model how those filings relate; that call is separate from a refresh and is saved for the same set of filings. Review, in that list, asks a second model whether it agrees with that filing's score; the stored score changes only if you confirm, and a confirmed score at or below the minimum leaves the column. Earnings (results, earnings-date announcements, guidance) are deliberately ignored, and M&A where the watched company is the acquirer is scored low unless it is dilutive or transformational. Uses `TF_ANTHROPIC_API_KEY` and `TF_LLM_MODEL`; set `TF_SEC_SIGNIFICANCE_ENABLED=false` to fall back to a keyless form-type heuristic.
- `TF_MASSIVE_API_KEY` (Breadth pane; free key from [massive.com](https://massive.com/pricing) Stocks Basic)
- `TF_BREADTH_LOOKBACK_DAYS` (trading days of EOD history to keep, default `90`)
- `TF_BREADTH_TICKERS` (optional comma-separated list; when set, skip Massive’s full common-stock universe)

TEMPORARY (breadth testing): a short `TF_BREADTH_TICKERS` list plus `TF_BREADTH_LOOKBACK_DAYS=5` in `backend/.env` makes Refresh finish in about a minute. Unset the ticker list and restore lookback to 90 for a full EOD run. With 5 days, quarter/MA columns will be empty.

The first full Breadth refresh backfills 90 sessions (about 15–20 minutes on Massive Basic’s 5 calls/min cap). Later refreshes usually fetch one session. Use the pane’s Refresh button after the US close — there is no scheduler.

## Testing

```bash
cd backend
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m pytest
```

pytest covers watchlist CRUD (yfinance mocked) and breadth counts/API (Massive mocked).
