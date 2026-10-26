"""Watchlist Dilution Risk score (0-100) + most-recent SEC filing link.

Blends two sources:

- ``app.services.edgar`` — SEC EDGAR filing signals (active S-3 shelf, recent
  424B offering, offering frequency, recent S-1) and the link to the most
  recent significant filing. Free, keyless.
- Unusual Whales financial statements (``/api/stock/{ticker}/financials``) —
  the *behavioral* signals: share-count growth (the ground truth — it already
  nets issuance against buybacks via the actual share count), stock-based
  compensation intensity, and cash runway (the motive to dilute).

Note: UW's ``proceeds_from_issuance_of_common_stock`` /
``payments_for_repurchase_of_common_stock`` line items are frequently null, so
we lean on ``common_stock_shares_outstanding`` growth rather than those fields.
The endpoint also returns ``annual`` and ``quarterly`` rows in one list; we use
the quarterly series (falling back to annual) so trailing-twelve-month sums and
the year-over-year share comparison stay consistent.

The score is a transparent, tunable heuristic (weights are module constants).
It is deliberately dominated by what a company *actually does* (net share
growth, burn) rather than the mere *capacity* to dilute (a boilerplate shelf),
so serial diluters score high while buyback-heavy blue chips score near zero.

Read on every watchlist refresh, so results are cached for ``TF_SEC_CACHE_TTL``
seconds. Lookups never raise: a UW/EDGAR outage yields partial or empty data so
the rest of the row still renders.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.config import get_settings
from app.services import edgar, gex, sec_significance
from app.services.edgar import FilingInfo
from app.services.uw import UnusualWhalesClient, UnusualWhalesClientProtocol

logger = logging.getLogger(__name__)

# --- score weights / thresholds (tunable) -----------------------------------
# Share-count growth (YoY) is the primary signal: it already nets issuance
# against buybacks via the actual share count. Full weight at/above _GROWTH_FULL_PCT.
_GROWTH_FULL_PCT = 20.0
_GROWTH_POINTS = 45.0
_BUYBACK_NOTE_PCT = -1.0  # share count shrinking by more than this -> "net buybacks"
# Stock-based-comp intensity (TTM SBC / total assets), a secondary drip signal.
# Only counts when the company is actually growing its share count.
_SBC_FULL_RATIO = 0.04
_SBC_POINTS = 10.0
# Cash runway (only when the company is burning operating cash).
_RUNWAY_POINTS = {0.5: 15.0, 1.0: 10.0, 2.0: 5.0}  # years -> points (first match)
# Filing signals.
_SHELF_BASE_POINTS = 5.0  # a shelf exists (capacity)
_SHELF_ACTIVE_BONUS = 10.0  # ...and the company is actually issuing/growing shares
_OFFERING_POINTS = 15.0  # a 424B* within _OFFERING_DAYS
_OFFERING_DAYS = 90
_SERIAL_OFFERING_POINTS = 5.0  # repeated 424B* offerings
_SERIAL_OFFERING_MIN = 3
_REGISTRATION_POINTS = 10.0  # a recent S-1
_FORM_D_POINTS = 10.0  # a recent Form D (private placement) within _FORM_D_DAYS
_FORM_D_DAYS = 90


@dataclass(frozen=True)
class DilutionInfo:
    """SEC/UW-derived dilution signals + most-recent significant filing link."""

    dilution_risk: float | None = None
    dilution_note: str | None = None
    sec_filing_url: str | None = None
    sec_filing_form: str | None = None
    sec_filing_date: str | None = None
    sec_significance: int | None = None
    sec_significance_note: str | None = None
    sec_significance_direction: str | None = None


EMPTY = DilutionInfo()


@dataclass(frozen=True)
class FinSignals:
    """Behavioral dilution signals distilled from UW financial statements."""

    share_growth_pct: float | None = None  # YoY change in shares outstanding
    sbc_ratio: float | None = None  # TTM stock-based comp / total assets
    runway_years: float | None = None  # None unless the company is burning cash
    has_data: bool = False


EMPTY_FIN = FinSignals()

# symbol -> (expires_at, info)
_cache: dict[str, tuple[float, DilutionInfo]] = {}
_cache_lock = threading.Lock()


def _cache_get(symbol: str) -> DilutionInfo | None:
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        expires_at, info = entry
        if time.monotonic() >= expires_at:
            _cache.pop(symbol, None)
            return None
        return info


def _cache_put(symbol: str, info: DilutionInfo) -> None:
    with _cache_lock:
        _cache[symbol] = (time.monotonic() + get_settings().sec_cache_ttl, info)


def clear_cache() -> None:
    """Test helper: drop the per-symbol cache."""
    with _cache_lock:
        _cache.clear()


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _one_report_type(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest-first rows of a single report type (quarterly preferred).

    UW returns ``annual`` and ``quarterly`` rows in one list; mixing them would
    double-count TTM sums and confuse the year-over-year share comparison.
    """
    clean = [r for r in rows if isinstance(r, dict)]
    quarterly = [r for r in clean if r.get("report_type") == "quarterly"]
    chosen = quarterly if quarterly else clean
    return sorted(chosen, key=lambda r: str(r.get("fiscal_date_ending") or ""), reverse=True)


def _ttm(rows: list[dict[str, Any]], field: str) -> float | None:
    """Sum ``field`` over the last four (newest) statements. None if all missing."""
    vals = [_as_float(r.get(field)) for r in rows[:4]]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _fiscal_date(row: dict[str, Any]) -> date | None:
    raw = str(row.get("fiscal_date_ending") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _share_growth(balance_sheets: list[dict[str, Any]]) -> float | None:
    """YoY percent change in common shares outstanding (latest vs ~1yr prior)."""
    rows: list[tuple[date, float]] = []
    for r in balance_sheets:
        d = _fiscal_date(r)
        shares = _as_float(r.get("common_stock_shares_outstanding"))
        if d is not None and shares:
            rows.append((d, shares))
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: r[0])
    latest_date, latest_val = rows[-1]
    if not latest_val:
        return None
    target = latest_date.toordinal() - 365
    prior_date, prior_val = min(rows[:-1], key=lambda r: abs(r[0].toordinal() - target))
    if not prior_val:
        return None
    return (latest_val - prior_val) / prior_val * 100.0


def financial_signals(data: dict[str, Any]) -> FinSignals:
    """Distil UW ``financials`` ``data`` into the behavioral dilution signals."""
    balance_sheets = _one_report_type(data.get("balance_sheets") or [])
    cash_flows = _one_report_type(data.get("cash_flows") or [])
    if not balance_sheets and not cash_flows:
        return EMPTY_FIN

    latest_bs = balance_sheets[0] if balance_sheets else {}
    total_assets = _as_float(latest_bs.get("total_assets"))
    cash = _as_float(latest_bs.get("cash_and_short_term_investments")) or _as_float(
        latest_bs.get("cash_and_cash_equivalents")
    )

    share_growth_pct = _share_growth(balance_sheets)

    sbc_ratio: float | None = None
    if total_assets:
        sbc = _ttm(cash_flows, "stock_based_compensation")
        if sbc is not None:
            sbc_ratio = sbc / total_assets

    runway_years: float | None = None
    op_cf = _ttm(cash_flows, "operating_cashflow")
    if op_cf is not None and op_cf < 0 and cash and cash > 0:
        runway_years = cash / (-op_cf)

    return FinSignals(
        share_growth_pct=share_growth_pct,
        sbc_ratio=sbc_ratio,
        runway_years=runway_years,
        has_data=True,
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score(filings: FilingInfo, fin: FinSignals) -> tuple[float, str]:
    """Combine filing + financial signals into a 0-100 score and a breakdown note.

    A single ordered list of ``(points, label)`` contributions is the source of
    truth for both the total and the hover-over explanation, so the note always
    adds up to the score.
    """
    contribs: list[tuple[float, str]] = []  # scored drivers (points, label)
    infos: list[str] = []  # zero-point informational lines

    is_issuing = fin.share_growth_pct is not None and fin.share_growth_pct > 0

    # Share-count growth is the primary behavioral signal: it already nets
    # issuance against buybacks via the actual share count. Points gate on the
    # same threshold as the note, so a nonzero score always has an explanation.
    if is_issuing and fin.share_growth_pct >= 1.0:
        pts = _clamp(fin.share_growth_pct / _GROWTH_FULL_PCT * _GROWTH_POINTS, 0.0, _GROWTH_POINTS)
        contribs.append((pts, f"Shares {fin.share_growth_pct:+.0f}% YoY"))
    elif fin.share_growth_pct is not None and fin.share_growth_pct < _BUYBACK_NOTE_PCT:
        infos.append(f"Net buybacks (shares {fin.share_growth_pct:+.0f}% YoY)")

    # Stock-based-comp drip, but only when the company is actually diluting
    # (a buyback-heavy name with large SBC is still net-shrinking its shares).
    if is_issuing and fin.sbc_ratio is not None and fin.sbc_ratio >= 0.02:
        pts = _clamp(fin.sbc_ratio / _SBC_FULL_RATIO * _SBC_POINTS, 0.0, _SBC_POINTS)
        contribs.append((pts, f"SBC {fin.sbc_ratio * 100:.0f}% of assets"))

    # Cash runway: the motive. Only applies when burning operating cash.
    if fin.runway_years is not None:
        for years, pts in sorted(_RUNWAY_POINTS.items()):
            if fin.runway_years < years:
                contribs.append((pts, f"~{fin.runway_years * 12:.0f}mo cash runway"))
                break

    # Filing signals. Softer shelf: a baseline for capacity, full weight only
    # when the company is actually issuing/growing shares.
    if filings.shelf_active:
        if is_issuing:
            contribs.append((_SHELF_BASE_POINTS + _SHELF_ACTIVE_BONUS, "Active S-3 shelf (issuing)"))
        else:
            contribs.append((_SHELF_BASE_POINTS, "S-3 shelf (unused capacity)"))

    if filings.days_since_424b is not None and 0 <= filings.days_since_424b <= _OFFERING_DAYS:
        contribs.append((_OFFERING_POINTS, f"424B {filings.days_since_424b}d ago"))

    if filings.count_424b_24mo >= _SERIAL_OFFERING_MIN:
        contribs.append((_SERIAL_OFFERING_POINTS, f"{filings.count_424b_24mo} offerings/24mo"))

    if filings.recent_s1:
        contribs.append((_REGISTRATION_POINTS, "Recent S-1"))

    if filings.days_since_form_d is not None and 0 <= filings.days_since_form_d <= _FORM_D_DAYS:
        contribs.append((_FORM_D_POINTS, f"Form D {filings.days_since_form_d}d ago"))

    if not fin.has_data:
        infos.append("Financials unavailable")

    total = round(_clamp(sum(p for p, _ in contribs), 0.0, 100.0), 1)

    lines = [f"Dilution Risk {total:.0f}/100"]
    lines += [f"+{p:.0f} {label}" for p, label in contribs]
    lines += [f"- {n}" for n in infos]
    if not contribs and not infos:
        lines.append("No dilution signals")
    return total, "\n".join(lines)


def _get_financials(
    symbol: str, *, client: UnusualWhalesClientProtocol | None = None
) -> FinSignals:
    """Fetch + distil UW financials for ``symbol``; empty on no-key/outage."""
    if client is None and not gex.has_uw_key():
        return EMPTY_FIN

    owned: UnusualWhalesClient | None = None
    try:
        active = client
        if active is None:
            owned = UnusualWhalesClient((get_settings().uw_api_key or "").strip())
            active = owned
        return financial_signals(active.financials(symbol))
    except Exception as exc:
        # A UW outage must not blank the row; fall back to filing-only signals.
        logger.warning("UW financials lookup failed for %s: %s", symbol, exc)
        return EMPTY_FIN
    finally:
        if owned is not None:
            owned.close()


def get_dilution(
    symbol: str,
    *,
    uw_client: UnusualWhalesClientProtocol | None = None,
    edgar_session=None,
) -> DilutionInfo:
    """Return the blended dilution score + SEC link for ``symbol``."""
    symbol = symbol.strip().upper()
    cached = _cache_get(symbol)
    if cached is not None:
        return cached

    filings = edgar.get_filings(symbol, session=edgar_session)
    fin = _get_financials(symbol, client=uw_client)

    if filings == edgar.EMPTY_FILINGS and not fin.has_data:
        # Nothing to say (e.g. an ETF with no CIK and no UW financials).
        _cache_put(symbol, EMPTY)
        return EMPTY

    # Surface the newest filing that clears the significance bar (blank if none).
    sig = sec_significance.evaluate(symbol, filings.candidates, session=edgar_session)

    risk, note = score(filings, fin)
    info = DilutionInfo(
        dilution_risk=risk,
        dilution_note=note,
        sec_filing_url=sig.url,
        sec_filing_form=sig.form,
        sec_filing_date=sig.date,
        sec_significance=sig.score,
        sec_significance_note=sig.note,
        sec_significance_direction=sig.direction,
    )
    _cache_put(symbol, info)
    return info
