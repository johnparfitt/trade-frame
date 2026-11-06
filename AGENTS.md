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
- SEC EDGAR: the watchlist SEC column comes from the free EDGAR APIs
  (`app/services/edgar.py`) — no key, but EDGAR requires a descriptive
  `User-Agent` (config `TF_SEC_USER_AGENT`) and caps callers at 10 req/s
  (`edgar.py` throttles every EDGAR call to 8 req/s; route new EDGAR calls
  through `edgar.http_get_json` / `fetch_document_text`). `edgar.py` returns a
  newest-first list of significant-filing *candidates* (10-K/Q, 8-K, S-1/3,
  424B, S-8, 14A/14C, Form D); it does not itself pick the surfaced filing.
  Document text is cached in a small LRU for the SEC scorer. Successful filing
  lists are cached for `TF_SEC_CACHE_TTL`. A failed lookup is not cached, so
  the next snapshot retries.
- SEC filing significance (the watchlist `SEC` column) is in
  `app/services/sec_significance.py`: an LLM reads the filing document and
  returns `{score, direction, reason}` — `score` is 0-100 *magnitude* of the
  potential move and `direction` is `bullish`/`bearish`/`neutral`. `evaluate()`
  scans the EDGAR candidates newest-first and keeps every filing that is both
  within `TF_SEC_SIGNIFICANCE_RECENT_DAYS` (default 30; candidates are
  newest-first, so the scan stops at the first stale one) and scoring above
  `TF_SEC_SIGNIFICANCE_MIN` (default 50). Worthless filings are skipped, so the
  column stays blank on quiet days and a real filing is never hidden behind a
  junk one. The cell's score, color, and hover note stay the newest qualifier;
  `sec_filings` lists every qualifier in the window. EDGAR hands the scorer at
  most 12 in-window candidates (`edgar._MAX_CANDIDATES`); older forms do not
  take a slot. The prompt deliberately forces earnings (results, earnings-date
  announcements, guidance) and acquirer-side M&A (unless dilutive/transformational)
  below the threshold. Scores are cached per accession (filings are immutable;
  ~1 LLM call per new filing in the window) in memory, and LLM scores also
  persist in SQLite (`sec_significance_scores`, keyed to a model + prompt hash
  so editing the prompt re-scores; heuristic fallbacks are never persisted).
  Resilient + never raises: a missing/empty key, `TF_SEC_SIGNIFICANCE_ENABLED=false`,
  an outage, or an unparseable response falls back to a deterministic
  form/8-K-item heuristic (which carries a direction too). Keys from env
  (`TF_ANTHROPIC_API_KEY`, model via `TF_LLM_MODEL`); the LLM call stays isolated
  in `llm.py` (`run()`). On-demand during `get_snapshot`, no scheduler. Frontend
  surfaces the newest score with a direction color (red bearish / green bullish /
  amber neutral) and a pulsing dot while any filing from the last 7 days is
  unseen (seen-state in `localStorage` by filing URL). Clicking the score opens
  the list and does not call the model. Opening one filing in that list marks
  only that filing. The list's Mark as read button, and a successful "Analyze
  together", mark every filing in the cell seen. The score stays and the dot
  goes away. Remove hides every filing URL in the cell (`localStorage`); the
  column stays blank until a new filing URL arrives. The stored score is not
  deleted. "Analyze
  together" is a separate
  `POST /api/watchlist/{symbol}/sec-brief` — it is not part of a watchlist
  refresh. That call reads the filings together (`prompts/sec/cluster.md`,
  about 8,000 characters of each document) and stores the text in
  `sec_filing_briefs`, keyed by the sorted accessions plus the model and prompt,
  so the same set is not analyzed again. A failed or disabled LLM call is not
  stored. The cell score does not change. "Review" on one filing
  (`POST /api/watchlist/{symbol}/sec-review`, prompt `prompts/sec/review.md`,
  the same document cap as the first score) asks a second model whether it
  agrees. That reply is not stored. Confirming replaces the accession's row
  in `sec_significance_scores` (same prompt key, so the next snapshot serves
  it). A replacement at or below the minimum leaves the column. One LLM call
  per click, not cached. Editing `significance.md` later re-scores and drops
  the override.
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
