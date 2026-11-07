"""Golden-path guardrail: SymbolSnapshot and WatchlistRow must stay in lockstep.

`watchlist.build_row()` builds a `WatchlistRow` by spreading
`dataclasses.asdict(snapshot)` straight into the Pydantic model (see
app/services/watchlist.py). That only works — and only stays safe as new
metrics are added — if the two field sets stay identical (aside from
`added_at`, which is watchlist-specific and not part of the market-data
snapshot). If someone adds a field to one and forgets the other, these tests
fail loudly instead of silently dropping/erroring on data at request time.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from app.schemas import WatchlistRow
from app.services.market_data import SymbolSnapshot
from app.services.sec_significance import SecFiling
from app.services.watchlist import build_row


def _snapshot_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(SymbolSnapshot)}


def _watchlist_row_field_names() -> set[str]:
    return set(WatchlistRow.model_fields.keys())


def test_every_snapshot_field_has_a_matching_watchlist_row_field():
    snapshot_fields = _snapshot_field_names()
    row_fields = _watchlist_row_field_names()
    missing = snapshot_fields - row_fields
    assert not missing, (
        f"SymbolSnapshot field(s) {sorted(missing)} have no matching field on "
        "WatchlistRow (app/schemas.py). build_row() spreads SymbolSnapshot "
        "into WatchlistRow by name, so add the same-named field there."
    )


def test_watchlist_row_only_adds_added_at_on_top_of_snapshot():
    snapshot_fields = _snapshot_field_names()
    row_fields = _watchlist_row_field_names()
    extra = row_fields - snapshot_fields
    assert extra == {"added_at"}, (
        f"WatchlistRow has field(s) {sorted(extra)} with no matching field on "
        "SymbolSnapshot (besides the expected 'added_at'). Either add the "
        "field to SymbolSnapshot/get_snapshot(), or if it's intentionally "
        "watchlist-only, update this test to document the exception."
    )


def _fully_populated_snapshot() -> SymbolSnapshot:
    """A SymbolSnapshot with every field set to a distinct, type-correct value."""
    values: dict[str, object] = {}
    for f in dataclasses.fields(SymbolSnapshot):
        if f.name == "analyst_upgrades":
            values[f.name] = ("NVDA|dan ives|2026-11-01T12:00:00Z",)
            continue
        if f.name == "sec_filings":
            values[f.name] = (
                SecFiling(
                    score=82,
                    direction="bearish",
                    reason="Priced offering",
                    form="8-K",
                    date="2026-09-30",
                    url="https://www.sec.gov/x",
                ),
            )
            continue
        if f.type in ("str", "str | None"):
            values[f.name] = f"{f.name}-value"
        elif f.type in ("int", "int | None"):
            values[f.name] = 1
        elif f.type in ("float", "float | None"):
            values[f.name] = 1.5
        else:  # pragma: no cover - guard against future field types
            raise AssertionError(
                f"Unhandled type {f.type!r} for SymbolSnapshot.{f.name}; "
                "extend this test helper to cover it."
            )
    return SymbolSnapshot(**values)


class _FakeItem:
    """Minimal stand-in for db.models.WatchlistItem, avoids needing a DB session."""

    def __init__(self, symbol: str, added_at: datetime):
        self.symbol = symbol
        self.added_at = added_at


def test_build_row_carries_every_snapshot_value_through_unchanged():
    snap = _fully_populated_snapshot()
    added_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    item = _FakeItem(symbol=snap.symbol, added_at=added_at)

    row = build_row(item, snap)  # type: ignore[arg-type]

    for f in dataclasses.fields(snap):
        got = getattr(row, f.name)
        want = getattr(snap, f.name)
        if f.name == "analyst_upgrades":
            assert list(got) == list(want)
            continue
        if f.name == "sec_filings":
            assert [item.model_dump() for item in got] == [dataclasses.asdict(item) for item in want]
            continue
        assert got == want, f"build_row() did not carry '{f.name}' through unchanged"
    assert row.added_at == added_at
