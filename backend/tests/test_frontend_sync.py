"""Golden-path guardrail: frontend WatchlistRow/columns must not drift from backend.

This is a lightweight, regex-based check (no TS toolchain in the backend test
env) that the field names in `frontend/src/api.ts`'s `WatchlistRow` interface,
and the column `key`s in `frontend/src/components/Watchlist.tsx`'s
`ALL_COLUMNS`, are all real fields on the backend `WatchlistRow` schema. It
won't catch every possible mismatch, but it catches the common drift: a field
renamed/removed on the backend while the frontend still references the old
name (or vice versa for the TS interface, which should mirror the backend
1:1).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.schemas import WatchlistRow

REPO_ROOT = Path(__file__).resolve().parents[2]
API_TS = REPO_ROOT / "frontend" / "src" / "api.ts"
WATCHLIST_TSX = REPO_ROOT / "frontend" / "src" / "components" / "Watchlist.tsx"

FIELD_LINE_RE = re.compile(r"^\s*(\w+)\s*:\s*.+;\s*$")
COLUMN_KEY_RE = re.compile(r'key:\s*"(\w+)"')


def _backend_fields() -> set[str]:
    return set(WatchlistRow.model_fields.keys())


def _ts_interface_fields(text: str, interface_name: str) -> set[str]:
    match = re.search(rf"interface {interface_name} \{{(.*?)\n\}}", text, re.S)
    assert match, f"Could not find `interface {interface_name}` in {API_TS}"
    body = match.group(1)
    fields = set()
    for line in body.splitlines():
        m = FIELD_LINE_RE.match(line)
        if m:
            fields.add(m.group(1))
    assert fields, f"Parsed zero fields out of interface {interface_name}; regex is stale."
    return fields


def test_ts_watchlist_row_interface_matches_backend_schema():
    text = API_TS.read_text()
    ts_fields = _ts_interface_fields(text, "WatchlistRow")
    backend_fields = _backend_fields()
    assert ts_fields == backend_fields, (
        "frontend/src/api.ts `WatchlistRow` interface has drifted from the "
        f"backend WatchlistRow schema.\n"
        f"  Only in frontend: {sorted(ts_fields - backend_fields)}\n"
        f"  Only in backend:  {sorted(backend_fields - ts_fields)}\n"
        "Keep both lists of fields identical."
    )


def test_all_columns_keys_are_valid_watchlist_row_fields():
    text = WATCHLIST_TSX.read_text()
    keys = set(COLUMN_KEY_RE.findall(text))
    assert keys, "Parsed zero column keys out of ALL_COLUMNS; regex is stale."
    backend_fields = _backend_fields()
    unknown = keys - backend_fields
    assert not unknown, (
        f"Watchlist.tsx ALL_COLUMNS references key(s) {sorted(unknown)} that "
        "don't exist on the backend WatchlistRow schema. Rename/remove the "
        "column definition, or add the field to the backend."
    )
