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
