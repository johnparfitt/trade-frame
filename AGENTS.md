# AGENTS.md

Operating rules for AI agents working in this repo. This is not a substitute
for `README.md` — see that file for setup/run commands, config vars,
and how to run tests. This file only holds things an agent must follow
even when a task seems unrelated to them.

## Scope — do not reintroduce out-of-scope features

This is a local, single-user "single pane of glass" for stock trading.

- **No stock screening.** No screener UI, no scan/filter-and-discover feature.
- **No trade execution.** No order entry, no broker integration that places trades.
- **No auth/multi-tenancy.** Single local user, no login system.
- **No background scheduler** (e.g. APScheduler) unless explicitly requested

If a task seems to require one of these, stop and ask rather than adding it.

## Where to put new work

Do not stuff unrelated UI or types into the watchlist pane or `WatchlistRow`.

- **New pane:** add `frontend/src/panes/<name>/` and one object to `PANES` in
  `frontend/src/panes/index.ts` (path, label, Component). `App.tsx` renders
  nav and routes from that list — do not hardcode a second pane in App.
- **New API:** add `backend/app/api/routes/<name>.py`, include the router in
  `backend/app/main.py`, and put request/response models in a new schema
  module (not on `WatchlistRow` / `backend/app/schemas.py`).
- **Watchlist-only changes:** stay inside `frontend/src/panes/watchlist/` and
  the existing watchlist route/service/schema files.

## The watchlist metric-sync contract

`SymbolSnapshot` (`backend/app/services/market_data.py`) and `WatchlistRow`
(`backend/app/schemas.py`) must stay field-for-field identical except for
`added_at`. `build_row()` (`backend/app/services/watchlist.py`) relies on this
via `dataclasses.asdict(snapshot)` spread into the Pydantic model — do not
replace that with a manual per-field mapping.

**Rule:** when adding, renaming, or removing a snapshot metric:

1. Keep `SymbolSnapshot`, backend `WatchlistRow`, and frontend `WatchlistRow`
   synchronized. `added_at` is the only field absent from `SymbolSnapshot`.
2. Update `ALL_COLUMNS` in `frontend/src/panes/watchlist/columns.tsx` if the
   metric should appear in the table. Every column key must reference a valid
   `WatchlistRow` field, but not every field needs a column.
3. Run the backend test suite:
   ```bash
   cd backend && ./.venv/bin/python -m pytest
   ```

## Watchlist UI layout invariants

- The table (`frontend/src/panes/watchlist/WatchlistTable.tsx`) is `table-fixed`
  with proportional `width` **weights** on `ALL_COLUMNS` entries
  (`frontend/src/panes/watchlist/columns.tsx`), not fixed pixel widths. Don't
  reintroduce fixed pixel widths.
- No horizontal scrolling on the table. Long text columns (Company, Industry)
  truncate via `truncate`/`max-w-0` instead of wrapping or overflowing.
- The sort-icon `<th>` always reserves a fixed `w-2` for the icon (visible or
  not), so columns don't shift width when a column becomes sorted/unsorted.
- Column visibility preferences persist to `localStorage`; when adding a new
  column, it should default to visible for users with existing saved prefs.

## Data source notes

- yfinance must stay on `1.7.0`+ with `curl_cffi` installed — earlier
  versions/combinations hit 429s and crumb-handling failures. Don't downgrade
  without verifying live data still fetches.
- `TF_SNAPSHOT_CACHE_TTL` and `TF_RVOL_LOOKBACK_DAYS` are config knobs (see
  README), not values to hardcode elsewhere.
- Unusual Whales: before calling the UW API or interpreting UW JSON, read
  `prompts/unusual-whales/SKILL.md`. Do not invent endpoints. The API token
  comes from env, never from the skill. Daily-report analysis prompt:
  `prompts/unusual-whales/daily-report.md`. Product UI still follows Scope
  (no screener).
- Daily Market Report (Report pane) uses UW `/api/market/daily-report` analyzed
  by an LLM with the `daily-report.md` prompt. Manual Generate button only (no
  scheduler). Keys from env: `TF_UW_API_KEY`, `TF_ANTHROPIC_API_KEY`; model via
  `TF_LLM_MODEL`. The LLM call is isolated in `backend/app/services/llm.py` —
  keep provider swaps there.

## Definition of done

Before considering a change finished:

- Backend tests pass. If you changed watchlist API behavior, add/update a test
  in `backend/tests/test_watchlist_api.py`. If you touched the metric-sync
  contract above, add/update a test there as well.
- If you changed Watchlist layout or behavior, verify it in the browser
  (add/remove a symbol, sort, toggle a column). Backend tests do not
  cover the UI.
