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
- `TF_SEC_USER_AGENT` (watchlist Dilution Risk / SEC columns; SEC EDGAR requires a descriptive User-Agent with contact info — set this to your own, e.g. `trade-frame/1.0 (you@example.com)`). The Dilution Risk score blends EDGAR filing signals (active S-3 shelf, recent 424B offerings, recent S-1, recent Form D) with Unusual Whales financials (net equity issuance, share growth, cash runway), so it also uses `TF_UW_API_KEY`.
- `TF_SEC_CACHE_TTL` (seconds, default `21600` = 6h; cache for the Dilution Risk / SEC lookups — each miss is a couple of EDGAR calls plus one UW financials call per symbol)
- `TF_SEC_SIGNIFICANCE_ENABLED` (default `true`), `TF_SEC_SIGNIFICANCE_MIN` (default `50`), and `TF_SEC_SIGNIFICANCE_RECENT_DAYS` (default `14`): the `SEC` column is an LLM-scored 0-100 filing-significance metric. An LLM reads the most recent significant filing and rates how far it could move the stock (the number = magnitude) and which way (the color: red bearish, green bullish, amber neutral). Only filings scoring above the minimum **and** filed within the last `TF_SEC_SIGNIFICANCE_RECENT_DAYS` days are surfaced, so the column stays blank on quiet days and ignores stale filings; it shows a recency dot and an unseen "look at me" pulse until you click through. Earnings (results, earnings-date announcements, guidance) are deliberately ignored, and M&A where the watched company is the acquirer is scored low unless it is dilutive or transformational. Uses `TF_ANTHROPIC_API_KEY` and `TF_LLM_MODEL`; set `TF_SEC_SIGNIFICANCE_ENABLED=false` to fall back to a keyless form-type heuristic.
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
