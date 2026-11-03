"""Watchlist Dilution Risk score (0-100) + most-recent SEC filing link.

Blends three sources:

- ``app.services.edgar`` — dated SEC EDGAR filing records (shelves, 424B
  prospectuses, S-1/F-1 registrations, Form D, 8-K Item 3.02 unregistered
  sales), scored here, and the significant-filing candidates for the SEC
  column. Free, keyless. ``company_facts`` adds XBRL overhang data (warrants,
  convertibles, cover-page share count) and ``filing_checks`` reads filing
  text (ATM / debt-takedown covers, proxy proposals, 8-K reverse splits).
- Unusual Whales financial statements (``/api/stock/{ticker}/financials``) —
  the *behavioral* signals: share-count growth (the ground truth — it already
  nets issuance against buybacks via the actual share count), stock-based
  compensation intensity, and cash runway (the motive to dilute).
- yfinance split history, passed in from the watchlist snapshot — a recent
  reverse split (a distress signal that often precedes issuance).

UW data conventions (verified live): ``common_stock_shares_outstanding`` is
back-adjusted for splits and covers all share classes; quarterly cash-flow rows
are discrete quarters (not year-to-date); capex is ``capital_expenditures``,
reported positive. UW's ``proceeds_from_issuance_of_common_stock`` /
``payments_for_repurchase_of_common_stock`` line items are frequently null, so
we lean on share-count growth rather than those fields. The endpoint returns
``annual`` and ``quarterly`` rows in one list; we use the quarterly series
(falling back to annual) so TTM sums and the YoY share comparison stay
consistent.

The score is a transparent, tunable heuristic (weights are module constants).
It is deliberately dominated by what a company *actually does* (net share
growth, burn) rather than the mere *capacity* to dilute (a boilerplate shelf),
so serial diluters score high while buyback-heavy blue chips score near zero.

Points fall into four capped groups — Behavior (45), Overhang (15), Filings
(30, scaled by company size), Distress (15) — and the total is clamped to 100.
Every point is a whole number and appears in the hover note, so the note always
adds up to the score.

Read on every watchlist refresh, so results are cached for ``TF_SEC_CACHE_TTL``
seconds (10 minutes when a lookup failed). Lookups never raise: a UW/EDGAR
outage yields partial or empty data so the rest of the row still renders, and
the score is None (not 0) when no source has usable data.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Callable, Iterable, Sequence

import httpx

from app.config import get_settings
from app.services import company_facts, edgar, filing_checks, gex, sec_significance
from app.services.company_facts import CompanyFacts
from app.services.edgar import FilingInfo, FilingRecord
from app.services.uw import UnusualWhalesClient, UnusualWhalesClientProtocol

logger = logging.getLogger(__name__)

# --- score weights / thresholds (tunable) -----------------------------------
_TOTAL_CAP = 100
_BEHAVIOR_CAP = 45
_OVERHANG_CAP = 15
_FILINGS_CAP = 30
_DISTRESS_CAP = 15

# One definition of "issuing" (growth points, SBC, shelf bonus): YoY share growth
# at or above this percent.
_ISSUING_MIN_PCT = 1.0
# Share-count growth (YoY) is the primary signal: it already nets issuance
# against buybacks via the actual share count. Log-scaled so the first +20%
# counts most: points = _GROWTH_POINTS * min(1, ln(1 + g/10) / ln(11)),
# about 16 at +20%, 25 at +45%, full at +100%.
_GROWTH_POINTS = 35.0
_GROWTH_SCALE_PCT = 10.0
_GROWTH_FULL_PCT = 100.0
_BUYBACK_NOTE_PCT = -1.0  # share count shrinking by more than this -> "net buybacks"
_SPLIT_GUARD_PCT = 40.0  # a bigger YoY change across a split is not trusted
_GROWTH_LABEL_MAX_PCT = 1000.0  # beyond this the label reads ">+1000%" (points are capped anyway)
# The prior share count must fall in this window (days before share_date), so a
# quarter-over-quarter or two-year comparison is never passed off as YoY.
_YOY_MIN_DAYS = 300
_YOY_MAX_DAYS = 430
# Stock-based comp as a share of market cap (a slow drip), only when issuing.
_SBC_MIN_RATIO = 0.01
_SBC_FULL_RATIO = 0.03
_SBC_POINTS = 5.0
# Cash runway (only when burning free cash flow, and only below the market-cap
# gate or when market cap is unknown).
_RUNWAY_POINTS = {0.5: 15, 1.0: 10, 2.0: 5}  # years -> points (first match)
_RUNWAY_MAX_MCAP = 10e9

# Overhang signals (EDGAR company facts): securities that can turn into shares.
_WARRANT_MIN_RATIO = 0.05  # warrants / shares outstanding
_WARRANT_FULL_RATIO = 0.25
_WARRANT_POINTS = 10.0
_CONVERTIBLE_MIN_RATIO = 0.05  # convertible principal / market cap
_CONVERTIBLE_FULL_RATIO = 0.25
_CONVERTIBLE_POINTS = 7.0
_CONVERSION_NEAR_FACTOR = 1.25  # conversion price at most 25% above today's price
_CONVERSION_NEAR_POINTS = 3

# Filing signals. Only the highest of Active ATM / Offering / Resale counts.
_ATM_FORMS = {"424B5"}  # a 424B5 whose cover page describes an at-the-market program
_ATM_POINTS = 12
_ATM_DAYS = 548  # ~18 months
_OFFERING_FORMS = {"424B5", "424B4"}  # 424B2 (bank structured notes) never counts
_RESALE_FORMS = {"424B3"}  # usually a resale by existing holders, not new issuance
_OFFERING_POINTS = 10
_RESALE_POINTS = 4
_OFFERING_DAYS = 90
_SERIAL_OFFERING_POINTS = 5
_SERIAL_OFFERING_MIN = 3
_SERIAL_WINDOW_DAYS = 2 * 365
_SHELF_FORMS = {"S-3", "S-3/A", "F-3", "F-3/A"}  # S-3ASR (seasoned-issuer auto-shelf) scores zero
_SHELF_DAYS = 3 * 365  # a shelf is "active" for ~3 years
_SHELF_POINTS = 3  # capacity alone
_SHELF_ISSUING_POINTS = 8  # ...when the company is actually issuing
_REGISTRATION_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A"}
_REGISTRATION_POINTS = 8
_REGISTRATION_DAYS = 180
_FORM_D_FORMS = {"D", "D/A"}
_FORM_D_POINTS = 4  # a private placement
_FORM_D_DAYS = 90
_EIGHT_K_FORMS = {"8-K", "8-K/A"}
_UNREGISTERED_SALE_ITEM = "3.02"  # 8-K item: unregistered sale of equity (PIPE)
_UNREGISTERED_SALE_POINTS = 8
_UNREGISTERED_SALE_DAYS = 90

# Distress signals.
_REVERSE_SPLIT_POINTS = 10
_REVERSE_SPLIT_DAYS = 365
_REVERSE_SPLIT_ITEM = "5.03"  # 8-K item: charter amendment (confirmed by its text)
_PROXY_FORMS = {"DEF 14A", "PRE 14A"}
_PROXY_DAYS = 365
_AUTHORIZED_INCREASE_POINTS = 8
_REVERSE_SPLIT_PROPOSED_POINTS = 8

# Document-check budget per symbol (results are cached per accession, so only
# new filings cost a fetch): at most 10 fetches on a cold load.
_OFFERING_CHECK_FORMS = _OFFERING_FORMS | _RESALE_FORMS
_OFFERING_CHECK_MAX = 6  # older ones inherit the majority class of these
_OFFERING_CHECK_DAYS = 2 * 365
_PROXY_CHECK_MAX = 2
_REVERSE_SPLIT_8K_CHECK_MAX = 2

# Offering classes, from the 424B cover page. An unknown check counts as equity,
# so an outage never hides a real issuer.
_ATM = "atm"
_EQUITY = "equity"
_DEBT = "debt"
# Filing points are scaled by market cap before the group cap: the same filing
# means far more for a small company. (upper bound, multiplier); unknown = 1.0.
_SIZE_MULTIPLIERS = ((300e6, 1.25), (2e9, 1.0), (10e9, 0.6))
_LARGE_CAP_MULTIPLIER = 0.3

# Results built on a failed lookup are retried sooner than the full TTL.
_DEGRADED_TTL_SECONDS = 600.0


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
    """Behavioral dilution signals distilled from UW financial statements.

    ``ok`` is False when the lookup failed (outage), as opposed to a symbol UW
    simply has no statements for (``has_data`` False, ``ok`` True).
    """

    share_growth_pct: float | None = None  # YoY change in shares outstanding, to share_date
    share_date: date | None = None  # date of the latest share count
    latest_shares: float | None = None  # latest share count (split-adjusted by UW)
    ttm_sbc: float | None = None  # TTM stock-based compensation (USD)
    runway_years: float | None = None  # None unless the company is burning cash
    runway_ocf_basis: bool = False  # capex missing: burn from operating cash flow alone
    has_data: bool = False
    ok: bool = True
    # (date, shares) balance-sheet counts, oldest first, so a fresher cover-page
    # count can be measured against the same history.
    share_history: tuple[tuple[date, float], ...] = ()
    share_prior_date: date | None = None  # date of the prior count behind share_growth_pct
    share_growth_unverified: bool = False  # growth dropped: a split may distort it


EMPTY_FIN = FinSignals()
FAILED_FIN = FinSignals(ok=False)

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


def _cache_put(symbol: str, info: DilutionInfo, *, degraded: bool) -> None:
    ttl = float(get_settings().sec_cache_ttl)
    if degraded:
        ttl = min(ttl, _DEGRADED_TTL_SECONDS)
    with _cache_lock:
        _cache[symbol] = (time.monotonic() + ttl, info)


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


def _statements(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Newest-first rows of a single report type, and whether they are quarterly.

    UW returns ``annual`` and ``quarterly`` rows in one list; mixing them would
    double-count TTM sums and confuse the year-over-year share comparison.
    """
    clean = [r for r in rows if isinstance(r, dict)]
    quarterly = [r for r in clean if r.get("report_type") == "quarterly"]
    chosen = quarterly if quarterly else clean
    ordered = sorted(chosen, key=lambda r: str(r.get("fiscal_date_ending") or ""), reverse=True)
    return ordered, bool(quarterly)


def _ttm_rows(rows: list[dict[str, Any]], is_quarterly: bool) -> list[dict[str, Any]]:
    """The rows making up the trailing twelve months: four quarters or one year."""
    return rows[:4] if is_quarterly else rows[:1]


def _ttm(rows: list[dict[str, Any]], field: str, is_quarterly: bool) -> float | None:
    """TTM sum of ``field``. None if all missing."""
    vals = [_as_float(r.get(field)) for r in _ttm_rows(rows, is_quarterly)]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _fiscal_date(row: dict[str, Any]) -> date | None:
    raw = str(row.get("fiscal_date_ending") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _share_history(balance_sheets: list[dict[str, Any]]) -> tuple[tuple[date, float], ...]:
    """``(date, shares)`` balance-sheet share counts, oldest first."""
    rows: list[tuple[date, float]] = []
    for r in balance_sheets:
        d = _fiscal_date(r)
        shares = _as_float(r.get("common_stock_shares_outstanding"))
        if d is not None and shares and shares > 0:
            rows.append((d, shares))
    return tuple(sorted(rows, key=lambda r: r[0]))


def _yoy_growth(
    history: Sequence[tuple[date, float]], share_date: date, latest: float
) -> tuple[float | None, date | None]:
    """``(yoy_pct, prior_date)`` from the prior count closest to a year before ``share_date``.

    The prior must fall within ``_YOY_MIN_DAYS``..``_YOY_MAX_DAYS``; growth is
    None when none qualifies.
    """
    window = [(d, v) for d, v in history if _YOY_MIN_DAYS <= (share_date - d).days <= _YOY_MAX_DAYS]
    if not window:
        return None, None
    prior_date, prior = min(window, key=lambda r: abs((share_date - r[0]).days - 365))
    return (latest - prior) / prior * 100.0, prior_date


def _fcf(row: dict[str, Any], *, ocf_basis: bool) -> float | None:
    ocf = _as_float(row.get("operating_cashflow"))
    if ocf is None:
        return None
    if ocf_basis:
        return ocf
    return ocf - abs(_as_float(row.get("capital_expenditures")) or 0.0)


def _runway(
    cash_flows: list[dict[str, Any]], is_quarterly: bool, cash: float | None
) -> tuple[float | None, bool]:
    """``(runway_years, ocf_basis)``: cash / burn, None unless burning.

    ``burn = max(-TTM_FCF, -4 x latest_quarter_FCF)``, so a company whose latest
    quarter turned negative still counts even if the TTM is positive.
    """
    rows = _ttm_rows(cash_flows, is_quarterly)
    rows = [r for r in rows if _as_float(r.get("operating_cashflow")) is not None]
    if not rows:
        return None, False
    ocf_basis = any(_as_float(r.get("capital_expenditures")) is None for r in rows)
    burn = -sum(_fcf(r, ocf_basis=ocf_basis) or 0.0 for r in rows)
    latest = _fcf(cash_flows[0], ocf_basis=ocf_basis) if is_quarterly else None
    if latest is not None:
        burn = max(burn, -4.0 * latest)
    if burn <= 0 or not cash or cash <= 0:
        return None, False
    return cash / burn, ocf_basis


def financial_signals(data: dict[str, Any]) -> FinSignals:
    """Distil UW ``financials`` ``data`` into the behavioral dilution signals."""
    balance_sheets, _ = _statements(data.get("balance_sheets") or [])
    cash_flows, cf_quarterly = _statements(data.get("cash_flows") or [])
    if not balance_sheets and not cash_flows:
        return EMPTY_FIN

    latest_bs = balance_sheets[0] if balance_sheets else {}
    cash = _as_float(latest_bs.get("cash_and_short_term_investments")) or _as_float(
        latest_bs.get("cash_and_cash_equivalents")
    )

    history = _share_history(balance_sheets)
    share_date, latest_shares = history[-1] if history else (None, None)
    share_growth_pct, prior_date = _yoy_growth(history, share_date, latest_shares) if history else (None, None)
    runway_years, runway_ocf_basis = _runway(cash_flows, cf_quarterly, cash)

    return FinSignals(
        share_growth_pct=share_growth_pct,
        share_prior_date=prior_date,
        share_date=share_date,
        latest_shares=latest_shares,
        ttm_sbc=_ttm(cash_flows, "stock_based_compensation", cf_quarterly),
        runway_years=runway_years,
        runway_ocf_basis=runway_ocf_basis,
        has_data=True,
        share_history=history,
    )


def split_factor(splits: Sequence[tuple[date, float]], after: date, today: date | None = None) -> float:
    """Product of new/old split ratios effective after ``after`` (through ``today``).

    Multiplying a raw share count dated ``after`` by this puts it on today's
    split basis, the basis UW back-adjusts its counts to.
    """
    today = today or date.today()
    factor = 1.0
    for d, ratio in splits:
        if after < d <= today and ratio > 0:
            factor *= ratio
    return factor


def apply_cover_count(
    fin: FinSignals,
    facts: CompanyFacts | None,
    splits: Sequence[tuple[date, float]] | None,
    today: date | None = None,
) -> FinSignals:
    """Use the cover-page share count when it is newer than the latest balance sheet.

    Its date becomes ``share_date`` (driving the YoY window and the
    no-double-counting rule), and the raw count is split-adjusted to UW's
    basis. Without split data (``splits=None``) the cover count can't be put on
    that basis, so the balance sheet stands; an ambiguous cover fact (per-class
    counts) falls back to it too.
    """
    cover = facts.cover_shares if facts is not None else None
    if cover is None or splits is None or cover.value <= 0:
        return fin
    if fin.share_date is not None and cover.end <= fin.share_date:
        return fin
    latest = cover.value * split_factor(splits, cover.end, today)
    growth, prior_date = _yoy_growth(fin.share_history, cover.end, latest)
    return replace(
        fin,
        share_growth_pct=growth,
        share_prior_date=prior_date,
        share_date=cover.end,
        latest_shares=latest,
    )


def verify_share_growth(fin: FinSignals, splits: Sequence[tuple[date, float]] | None) -> FinSignals:
    """Drop a large share change that a split may have distorted.

    UW back-adjusts counts for splits, but not reliably for serial
    reverse-splitters (verified 2026-10-27: SMX reports quarters of 0-8 shares,
    ASST 38,979 -> 75M across a 1-for-20). So a change beyond
    ``_SPLIT_GUARD_PCT`` is dropped when a split falls inside its YoY window,
    or when split data failed (``splits=None``) and one could be hiding.
    """
    g = fin.share_growth_pct
    if g is None or abs(g) <= _SPLIT_GUARD_PCT:
        return fin
    if splits is not None:
        start, end = fin.share_prior_date, fin.share_date
        if start is None or end is None or not any(start < d <= end for d, _ in splits):
            return fin
    return replace(fin, share_growth_pct=None, share_growth_unverified=True)


def resolve_market_cap(
    market_cap: float | None, price: float | None, latest_shares: float | None
) -> float | None:
    """Snapshot market cap, else price x latest share count, else None (unknown).

    UW back-adjusts share counts for splits, so the latest count is already on
    the same basis as today's price.
    """
    if market_cap is not None and market_cap > 0:
        return market_cap
    if price is not None and price > 0 and latest_shares:
        return price * latest_shares
    return None


def _round(value: float) -> int:
    """Round half up (Python's round() sends 2.5 to 2)."""
    return math.floor(value + 0.5)


def size_multiplier(market_cap: float | None) -> float:
    """Filing-points multiplier for a company of ``market_cap`` (unknown = 1.0)."""
    if market_cap is None:
        return 1.0
    for upper, multiplier in _SIZE_MULTIPLIERS:
        if market_cap < upper:
            return multiplier
    return _LARGE_CAP_MULTIPLIER


def growth_points(g: float) -> float:
    """Log-scaled share-growth points (unrounded); see ``_GROWTH_POINTS``."""
    scaled = math.log(1 + g / _GROWTH_SCALE_PCT) / math.log(1 + _GROWTH_FULL_PCT / _GROWTH_SCALE_PCT)
    return _GROWTH_POINTS * min(1.0, scaled)


@dataclass
class _Group:
    """One capped group of whole-number signal points."""

    name: str
    cap: int
    multiplier: float = 1.0
    lines: list[tuple[int, str]] = field(default_factory=list)

    def add(self, points: float, label: str) -> None:
        pts = _round(points)
        if pts > 0:
            self.lines.append((pts, label))

    @property
    def raw(self) -> int:
        return sum(p for p, _ in self.lines)

    @property
    def scaled(self) -> int:
        return _round(self.raw * self.multiplier)

    @property
    def subtotal(self) -> int:
        return min(self.scaled, self.cap)

    def describe(self) -> str:
        parts = ", ".join(f"{label} (+{p})" for p, label in self.lines)
        text = f"{self.name} {self.subtotal}: {parts}"
        if self.multiplier != 1.0:
            text += f" = {self.raw} x{self.multiplier:g} = {self.scaled}"
        elif self.scaled > self.cap:
            text += f" = {self.scaled}"
        if self.scaled > self.cap:
            text += f", capped at {self.cap}"
        return text


def _within(
    records: Iterable[FilingRecord],
    forms: set[str],
    today: date,
    days: int,
    keep: Callable[[FilingRecord], bool] = lambda r: True,
) -> FilingRecord | None:
    """Newest record with a form in ``forms``, filed within ``days``, passing ``keep``."""
    for r in records:
        age = (today - r.filed).days
        if age > days:
            return None  # newest-first: everything after is older still
        if age >= 0 and r.form in forms and keep(r):
            return r
    return None


def _recent(
    records: Iterable[FilingRecord],
    forms: set[str],
    today: date,
    days: int,
    keep: Callable[[FilingRecord], bool] = lambda r: True,
) -> list[FilingRecord]:
    """Newest-first records with a form in ``forms``, filed within ``days``, passing ``keep``."""
    out: list[FilingRecord] = []
    for r in records:
        age = (today - r.filed).days
        if age > days:
            break
        if age >= 0 and r.form in forms and keep(r):
            out.append(r)
    return out


@dataclass(frozen=True)
class DocChecks:
    """What the filing-text checks found (``app.services.filing_checks``).

    ``offering_class`` maps 424B accessions to ``_ATM`` / ``_EQUITY`` /
    ``_DEBT``; a filing it doesn't list counts as equity. ``unknown`` counts
    checks whose text was unavailable (a transient fetch failure).
    """

    offering_class: dict[str, str] = field(default_factory=dict)
    authorized_increase: FilingRecord | None = None
    reverse_split_proposed: FilingRecord | None = None
    reverse_split_8k: FilingRecord | None = None
    unknown: int = 0


def run_document_checks(
    records: Sequence[FilingRecord],
    *,
    session: Any,
    today: date | None = None,
    check_reverse_split_8k: bool = True,
) -> DocChecks:
    """Read the filings whose text matters, within the per-symbol fetch budget."""
    today = today or date.today()
    unknown = 0

    # One cover-page fetch per 424B answers both ATM and debt takedown. Beyond
    # the budget, filings inherit the majority class of the classified ones,
    # so a frequent bond issuer doesn't cost a fetch per note.
    offerings = _recent(records, _OFFERING_CHECK_FORMS, today, _OFFERING_CHECK_DAYS)
    classes: dict[str, str] = {}
    classified: list[str] = []
    for r in offerings[:_OFFERING_CHECK_MAX]:
        cover = filing_checks.check_offering(r, session=session)
        if cover is None:
            unknown += 1
            classes[r.accession] = _EQUITY
            continue
        if cover.atm and r.form in _ATM_FORMS:
            cls = _ATM
        elif cover.debt:
            cls = _DEBT
        else:
            cls = _EQUITY
        classes[r.accession] = cls
        classified.append(cls)
    debt = classified.count(_DEBT)
    inherited = _DEBT if debt > len(classified) - debt else _EQUITY
    for r in offerings[_OFFERING_CHECK_MAX:]:
        classes[r.accession] = inherited

    authorized: FilingRecord | None = None
    proposed_split: FilingRecord | None = None
    for r in _recent(records, _PROXY_FORMS, today, _PROXY_DAYS)[:_PROXY_CHECK_MAX]:
        if authorized is not None and proposed_split is not None:
            break
        proposals = filing_checks.check_proxy(r, session=session)
        if proposals is None:
            unknown += 1
            continue
        if proposals.authorized_increase and authorized is None:
            authorized = r
        if proposals.reverse_split and proposed_split is None:
            proposed_split = r

    split_8k: FilingRecord | None = None
    if check_reverse_split_8k:
        item_503 = _recent(
            records, _EIGHT_K_FORMS, today, _REVERSE_SPLIT_DAYS, lambda r: _REVERSE_SPLIT_ITEM in r.items
        )
        for r in item_503[:_REVERSE_SPLIT_8K_CHECK_MAX]:
            found = filing_checks.check_reverse_split_8k(r, session=session)
            if found is None:
                unknown += 1
            elif found:
                split_8k = r
                break

    return DocChecks(
        offering_class=classes,
        authorized_increase=authorized,
        reverse_split_proposed=proposed_split,
        reverse_split_8k=split_8k,
        unknown=unknown,
    )


def _ago(days: int) -> str:
    return f"{days // 30}mo ago" if days >= 60 else f"{days}d ago"


def _recent_reverse_split(
    splits: Sequence[tuple[date, float]], today: date
) -> tuple[date, float] | None:
    """Newest split with a new/old ratio below 1 within ``_REVERSE_SPLIT_DAYS``."""
    recent = [
        (d, ratio)
        for d, ratio in splits
        if 0 < ratio < 1 and 0 <= (today - d).days <= _REVERSE_SPLIT_DAYS
    ]
    return max(recent, default=None)


def _add_overhang(
    overhang: _Group,
    infos: list[str],
    facts: CompanyFacts,
    fin: FinSignals,
    market_cap: float | None,
    price: float | None,
    price_time: str | None,
    splits: Sequence[tuple[date, float]],
    today: date,
) -> None:
    """Warrant and convertible overhang from loaded company facts.

    Most companies without these securities omit the tags rather than report
    0, so an absent tag scores 0 silently; only an ambiguous one (per-series
    values in one filing) gets an "unavailable" line. Raw counts and prices
    are put on today's split basis.
    """
    if company_facts.WARRANTS in facts.ambiguous:
        infos.append("Warrant data unavailable")
    elif facts.warrants is not None and fin.latest_shares:
        warrants = facts.warrants.value * split_factor(splits, facts.warrants.end, today)
        ratio = warrants / fin.latest_shares
        if ratio >= _WARRANT_MIN_RATIO:
            pts = min(_WARRANT_POINTS, ratio / _WARRANT_FULL_RATIO * _WARRANT_POINTS)
            overhang.add(pts, f"Warrants {ratio * 100:.0f}% of shares")

    if company_facts.CONVERTIBLES in facts.ambiguous:
        infos.append("Convertible data unavailable")
        return
    if facts.convertible_principal is None or not market_cap:
        return
    ratio = facts.convertible_principal.value / market_cap
    if ratio < _CONVERTIBLE_MIN_RATIO:
        return
    pts = min(_CONVERTIBLE_POINTS, ratio / _CONVERTIBLE_FULL_RATIO * _CONVERTIBLE_POINTS)
    overhang.add(pts, f"Convertibles {ratio * 100:.0f}% of mkt cap")
    if company_facts.CONVERSION_PRICE in facts.ambiguous:
        infos.append("Conversion price unavailable")
    elif facts.conversion_price is not None and price:
        conversion = facts.conversion_price.value / split_factor(splits, facts.conversion_price.end, today)
        if 0 < conversion <= _CONVERSION_NEAR_FACTOR * price:
            as_of = f" as of {price_time}" if price_time else ""
            overhang.add(
                _CONVERSION_NEAR_POINTS,
                f"Conversion ${conversion:,.2f} near ${price:,.2f} price{as_of}",
            )


def score(
    filings: FilingInfo,
    fin: FinSignals,
    *,
    market_cap: float | None = None,
    splits: Sequence[tuple[date, float]] | None = (),
    facts: CompanyFacts | None = None,
    checks: DocChecks | None = None,
    price: float | None = None,
    price_time: str | None = None,
    today: date | None = None,
) -> tuple[int, str]:
    """Combine filing + financial signals into a 0-100 score and a breakdown note.

    ``facts`` (company facts) and ``checks`` (filing-text checks) are None when
    not consulted; without ``checks`` every 424B counts as equity. ``fin``
    should already carry any fresher cover-page count (``apply_cover_count``)
    and the split check (``verify_share_growth``).

    Each signal's points are rounded to a whole number, summed per group,
    (for filings) scaled by company size and rounded again, then capped per
    group; the total is clamped to 100. The note lists every group's lines and
    says when the size multiplier or a cap changed a subtotal, so it always
    adds up to the score.
    """
    today = today or date.today()
    records = filings.records
    known_splits = splits or ()
    behavior = _Group("Behavior", _BEHAVIOR_CAP)
    overhang = _Group("Overhang", _OVERHANG_CAP)
    filing = _Group("Filings", _FILINGS_CAP, multiplier=size_multiplier(market_cap))
    distress = _Group("Distress", _DISTRESS_CAP)
    infos: list[str] = []  # zero-point informational lines

    # --- Behavior ---
    g = fin.share_growth_pct
    is_issuing = g is not None and g >= _ISSUING_MIN_PCT
    if is_issuing:
        shown = f">+{_GROWTH_LABEL_MAX_PCT:.0f}%" if g > _GROWTH_LABEL_MAX_PCT else f"{g:+.0f}%"
        behavior.add(growth_points(g), f"Shares {shown} YoY")
    elif g is not None and g < _BUYBACK_NOTE_PCT:
        infos.append(f"Net buybacks (shares {g:+.0f}% YoY)")
    elif fin.share_growth_unverified:
        infos.append("Share change unverified: possible split")
    growth_scored = bool(behavior.lines)

    if fin.runway_years is not None and (market_cap is None or market_cap < _RUNWAY_MAX_MCAP):
        for years, pts in sorted(_RUNWAY_POINTS.items()):
            if fin.runway_years < years:
                basis = ", OCF basis" if fin.runway_ocf_basis else ""
                behavior.add(pts, f"~{fin.runway_years * 12:.0f}mo runway{basis}")
                break

    # Stock-based-comp drip, only when the company is actually diluting (a
    # buyback-heavy name with large SBC is still net-shrinking its shares).
    if is_issuing and fin.ttm_sbc is not None and market_cap:
        sbc_ratio = fin.ttm_sbc / market_cap
        if sbc_ratio >= _SBC_MIN_RATIO:
            pts = min(_SBC_POINTS, sbc_ratio / _SBC_FULL_RATIO * _SBC_POINTS)
            behavior.add(pts, f"SBC {sbc_ratio * 100:.1f}% of mkt cap")

    # --- Overhang ---
    if facts is not None:
        if not facts.ok or not facts.found:
            infos.append("Warrant/convertible data unavailable")
        else:
            _add_overhang(overhang, infos, facts, fin, market_cap, price, price_time, known_splits, today)

    # --- Filings ---
    # No double counting: once share growth scored, a one-off offering filed on
    # or before share_date is already in g, so only later ones count. Active
    # ATM and Serial offerings always count (an ongoing program or a pattern).
    def after_share_date(r: FilingRecord) -> bool:
        return not growth_scored or fin.share_date is None or r.filed > fin.share_date

    classes = checks.offering_class if checks is not None else {}

    def cls(r: FilingRecord) -> str:
        return classes.get(r.accession, _EQUITY)

    atm = _within(records, _ATM_FORMS, today, _ATM_DAYS, lambda r: cls(r) == _ATM)
    offering = _within(
        records, _OFFERING_FORMS, today, _OFFERING_DAYS, lambda r: cls(r) == _EQUITY and after_share_date(r)
    )
    resale = _within(
        records, _RESALE_FORMS, today, _OFFERING_DAYS, lambda r: cls(r) != _DEBT and after_share_date(r)
    )
    if atm is not None:
        filing.add(_ATM_POINTS, f"Active ATM, {atm.form} {_ago((today - atm.filed).days)}")
    elif offering is not None:
        filing.add(_OFFERING_POINTS, f"{offering.form} offering {(today - offering.filed).days}d ago")
    elif resale is not None:
        filing.add(_RESALE_POINTS, f"{resale.form} resale {(today - resale.filed).days}d ago")

    serial = len(_recent(records, _OFFERING_FORMS, today, _SERIAL_WINDOW_DAYS, lambda r: cls(r) != _DEBT))
    if serial >= _SERIAL_OFFERING_MIN:
        filing.add(_SERIAL_OFFERING_POINTS, f"{serial} offerings/24mo")

    shelf = _within(records, _SHELF_FORMS, today, _SHELF_DAYS)
    if shelf is not None:
        if is_issuing:
            filing.add(_SHELF_ISSUING_POINTS, f"{shelf.form} shelf, issuing")
        else:
            filing.add(_SHELF_POINTS, f"{shelf.form} shelf")

    registration = _within(records, _REGISTRATION_FORMS, today, _REGISTRATION_DAYS)
    if registration is not None:
        filing.add(_REGISTRATION_POINTS, f"{registration.form} {(today - registration.filed).days}d ago")

    unregistered = _within(
        records,
        _EIGHT_K_FORMS,
        today,
        _UNREGISTERED_SALE_DAYS,
        lambda r: _UNREGISTERED_SALE_ITEM in r.items,
    )
    if unregistered is not None:
        filing.add(_UNREGISTERED_SALE_POINTS, f"Item 3.02 {(today - unregistered.filed).days}d ago")

    form_d = _within(records, _FORM_D_FORMS, today, _FORM_D_DAYS)
    if form_d is not None:
        filing.add(_FORM_D_POINTS, f"Form D {(today - form_d.filed).days}d ago")

    # --- Distress ---
    reverse = _recent_reverse_split(known_splits, today)
    if reverse is not None:
        split_date, ratio = reverse
        distress.add(
            _REVERSE_SPLIT_POINTS,
            f"Reverse split 1:{_round(1 / ratio)} {_ago((today - split_date).days)}",
        )
    elif checks is not None and checks.reverse_split_8k is not None:
        distress.add(
            _REVERSE_SPLIT_POINTS, f"Reverse split per 8-K {_ago((today - checks.reverse_split_8k.filed).days)}"
        )
    if checks is not None and checks.authorized_increase is not None:
        distress.add(
            _AUTHORIZED_INCREASE_POINTS,
            f"Authorized-share increase proposed {_ago((today - checks.authorized_increase.filed).days)}",
        )
    if checks is not None and checks.reverse_split_proposed is not None:
        distress.add(
            _REVERSE_SPLIT_PROPOSED_POINTS,
            f"Reverse split proposed {_ago((today - checks.reverse_split_proposed.filed).days)}",
        )

    # --- Missing / failed sources ---
    if not fin.ok:
        infos.append("Financials lookup failed")
    elif not fin.has_data:
        infos.append("Financials unavailable")
    if not filings.ok:
        infos.append("SEC filings lookup failed")
    elif not filings.found:
        infos.append("SEC filings unavailable")
    if splits is None:
        infos.append("Split history unavailable")
    if checks is not None and checks.unknown:
        infos.append("Filing text unavailable")

    groups = [grp for grp in (behavior, overhang, filing, distress) if grp.lines]
    total = sum(grp.subtotal for grp in groups)
    final = min(total, _TOTAL_CAP)

    header = f"Dilution Risk {final}/100"
    if total > _TOTAL_CAP:
        header += f" ({total}, capped at {_TOTAL_CAP})"
    lines = [header] + [g.describe() for g in groups] + [f"- {n}" for n in infos]
    if not groups and not infos:
        lines.append("No dilution signals")
    return final, "\n".join(lines)


def _get_financials(
    symbol: str, *, client: UnusualWhalesClientProtocol | None = None
) -> FinSignals:
    """Fetch + distil UW financials for ``symbol``.

    Empty (``has_data`` False) without a UW key or when UW has no statements;
    failed (``ok`` False) on an outage.
    """
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
        return FAILED_FIN
    finally:
        if owned is not None:
            owned.close()


def get_dilution(
    symbol: str,
    *,
    price: float | None = None,
    market_cap: float | None = None,
    splits: Sequence[tuple[date, float]] | None = None,
    uw_client: UnusualWhalesClientProtocol | None = None,
    edgar_session=None,
) -> DilutionInfo:
    """Return the blended dilution score + SEC link for ``symbol``.

    ``price``, ``market_cap`` and ``splits`` (``(date, new/old ratio)``; None
    when the history fetch failed) come from the watchlist snapshot's
    yfinance lookup (no extra calls here).
    """
    symbol = symbol.strip().upper()
    cached = _cache_get(symbol)
    if cached is not None:
        return cached

    owned: httpx.Client | None = None
    session = edgar_session
    if session is None:
        owned = httpx.Client(timeout=30.0)
        session = owned
    try:
        info, degraded = _build(symbol, price, market_cap, splits, uw_client, session)
    finally:
        if owned is not None:
            owned.close()
    _cache_put(symbol, info, degraded=degraded)
    return info


def _build(
    symbol: str,
    price: float | None,
    market_cap: float | None,
    splits: Sequence[tuple[date, float]] | None,
    uw_client: UnusualWhalesClientProtocol | None,
    session: Any,
) -> tuple[DilutionInfo, bool]:
    """``(info, degraded)`` for one uncached lookup; degraded results retry sooner."""
    today = date.today()
    filings = edgar.get_filings(symbol, session=session)
    fin = _get_financials(symbol, client=uw_client)
    degraded = not (filings.ok and fin.ok) or splits is None

    if not filings.found and not fin.has_data:
        # No usable data from any source (e.g. an ETF with no CIK and no UW
        # financials, or every lookup failed): a blank score, never a "safe" 0.
        return EMPTY, degraded

    facts: CompanyFacts | None = None
    if filings.cik is not None:
        facts = company_facts.get_company_facts(filings.cik, filings.records, session=session)
        degraded = degraded or not facts.ok
    fin = verify_share_growth(apply_cover_count(fin, facts, splits, today), splits)

    # Checks run before the significance scan so a filing both read (a proxy,
    # a 424B) is fetched once and served from edgar's document cache.
    checks = run_document_checks(
        filings.records,
        session=session,
        today=today,
        check_reverse_split_8k=_recent_reverse_split(splits or (), today) is None,
    )
    degraded = degraded or checks.unknown > 0

    # Surface the newest filing that clears the significance bar (blank if none).
    sig = sec_significance.evaluate(symbol, filings.candidates, session=session)

    mcap = resolve_market_cap(market_cap, price, fin.latest_shares)
    risk, note = score(
        filings,
        fin,
        market_cap=mcap,
        splits=splits,
        facts=facts,
        checks=checks,
        price=price,
        price_time=datetime.now().strftime("%H:%M"),
        today=today,
    )
    info = DilutionInfo(
        dilution_risk=float(risk),
        dilution_note=note,
        sec_filing_url=sig.url,
        sec_filing_form=sig.form,
        sec_filing_date=sig.date,
        sec_significance=sig.score,
        sec_significance_note=sig.note,
        sec_significance_direction=sig.direction,
    )
    return info, degraded
