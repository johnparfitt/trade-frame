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

Override via `TF_` env vars or `backend/.env`:

- `TF_DATABASE_URL` (default SQLite under `backend/data/`)
- `TF_RVOL_LOOKBACK_DAYS` (default `20`)
- `TF_SNAPSHOT_CACHE_TTL` (seconds, default `30`)

## Testing

```bash
cd backend
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m pytest
```
