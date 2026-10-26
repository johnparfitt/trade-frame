# Personal notes (human only)

**Not authoritative.** Do not use this file for architecture, behavior, config, or tasks.
**Source of truth:** `README.md` (setup/config), `AGENTS.md` (agent rules), and the code.

Scratchpad for JP. May be stale, wrong, or contradictory. Last touched: 2026-09-18

## Data ingest

Nothing is scheduled. Data only moves when you open a pane or click Refresh.

| Pane | Source | Trigger | How often | $/day |
|---|---|---|---|---|
| **Watchlist** | Yahoo Finance via yfinance (no key) | Open the pane, click Refresh, or add a symbol | Frontend polls every **16 min** while the pane is open. Backend cache is **30s**. | **$0** |
| **Breadth** | Massive Stocks Basic (EOD grouped daily) | Refresh button only | After the US close, usually **1 session**. First run backfills **90 days**. Ticker universe refreshes every **7 days**. | **$0** |
| **Report** | Unusual Whales `/api/market/daily-report` + Claude (LLM analysis) | Generate button only | One UW call + one LLM call per click, after the close. | **UW Basic + LLM tokens/run** |

Massive Basic is 5 calls/min, EOD-only (today’s unpublished file is skipped). First full breadth refresh is ~15–20 min; later ones are usually one call. Close the watchlist pane and Yahoo polling stops.

Report needs `TF_UW_API_KEY` + `TF_ANTHROPIC_API_KEY` (model via `TF_LLM_MODEL`). It fetches the UW daily digest, runs the `prompts/unusual-whales/daily-report.md` prompt through the LLM, and stores one analysis per session date.

## Known gaps / TODO

- **DB schema migrations.** `init_db()` (`backend/app/db/session.py`) only runs
  `Base.metadata.create_all`, which creates missing tables but never ALTERs
  existing ones. After changing a model (e.g. adding a column to `breadth_days`
  or `daily_reports`), an existing `backend/data/*.db` will NOT pick up the new
  column and you'll hit runtime errors. For now: delete the local SQLite file to
  let it recreate. If schemas start evolving often, add a lightweight migration
  path (Alembic, or a small ALTER bootstrap in `init_db`).
- **`redact_secrets` duplication.** `backend/app/services/uw.py` (Bearer tokens)
  and `backend/app/services/massive.py` (apiKey) each define their own
  `redact_secrets` plus a copy-pasted "re-raise `HTTPStatusError` with a redacted
  message" block. They're injected into the shared `JobRunner`
  (`backend/app/services/jobs.py`) today. When a 3rd external client appears,
  consolidate into a shared `services/http_util.py` (a `redact` composed of both
  patterns + a `raise_redacted` helper).
