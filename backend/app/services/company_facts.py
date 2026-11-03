"""EDGAR company facts (XBRL) for the Dilution Risk score.

One ``/api/xbrl/companyfacts/CIK##########.json`` call per symbol, parsed down
to the few facts the score uses:

- cover-page shares outstanding (``dei:EntityCommonStockSharesOutstanding``),
  usually fresher than the latest balance sheet;
- warrants outstanding (``ClassOfWarrantOrRightOutstanding``);
- convertible debt principal and its conversion price.

Only that parsed subset is cached (the raw payload is several MB), keyed on
the accession of the company's latest periodic filing, so a refetch happens
only after a new 10-K/10-Q. The subset is also persisted in SQLite
(``sec_store``) for ``_STORE_MAX_AGE`` so a restart doesn't refetch. Per tag the value is the latest ``end`` (ignoring
facts more than ~15 months old), from the latest ``filed`` accession; when
one accession reports different values for that ``end`` (per-class or
per-series facts) the value is ambiguous and not used, never a max or a sum.
Every fact keeps its ``filed`` date for backtesting.

Never raises. A 404 means the company has no XBRL facts (cached as empty); a
403, 429, 5xx, or network error is a failed lookup (``ok=False``, not cached).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from app.services import edgar, sec_store
from app.services.edgar import FilingRecord
from app.services.lru import BoundedLRU

logger = logging.getLogger(__name__)

COMPANY_FACTS_URL = f"{edgar.DATA_BASE}/api/xbrl/companyfacts/CIK{{cik10}}.json"

_STALE_DAYS = 456  # ~15 months
_PERIODIC_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}

_COVER_TAG = ("dei", "EntityCommonStockSharesOutstanding", "shares")
_WARRANT_TAG = ("us-gaap", "ClassOfWarrantOrRightOutstanding", "shares")
_CONVERSION_PRICE_TAG = ("us-gaap", "DebtInstrumentConvertibleConversionPrice1", "USD/shares")
# Convertible principal, in order of preference when several report the same
# latest ``end``: a total tag, else a current + noncurrent pair summed. A
# total is never added to its own components.
_CONVERTIBLE_MEASURES: tuple[tuple[str, ...], ...] = (
    ("ConvertibleDebt",),
    ("ConvertibleNotesPayable",),
    ("ConvertibleDebtCurrent", "ConvertibleDebtNoncurrent"),
    ("ConvertibleNotesPayableCurrent", "ConvertibleLongTermNotesPayable"),
)

# Names in ``CompanyFacts.ambiguous``.
COVER_SHARES = "cover_shares"
WARRANTS = "warrants"
CONVERTIBLES = "convertibles"
CONVERSION_PRICE = "conversion_price"

_CACHE_MAX_ENTRIES = 1024

# Parsing drops facts by age relative to today, so a stored subset expires.
_STORE_MAX_AGE = timedelta(days=30)
_STORE_KIND = "company_facts"
# Bump when parsing changes in a way the constants below don't capture.
_LOGIC_REVISION = 1


@dataclass(frozen=True)
class Fact:
    value: float
    end: date
    filed: date
    accession: str


@dataclass(frozen=True)
class CompanyFacts:
    """Parsed company-facts subset.

    ``ok`` is False when the lookup failed; ``found`` is False when the company
    has no XBRL facts. ``ambiguous`` names the facts that were skipped because
    one accession reported conflicting values.
    """

    ok: bool = True
    found: bool = False
    cover_shares: Fact | None = None
    warrants: Fact | None = None
    convertible_principal: Fact | None = None
    conversion_price: Fact | None = None
    ambiguous: frozenset[str] = field(default_factory=frozenset)


NO_FACTS = CompanyFacts()
FAILED_FACTS = CompanyFacts(ok=False)

_RULES_VERSION = sec_store.rules_version(
    _LOGIC_REVISION,
    _STALE_DAYS,
    _COVER_TAG,
    _WARRANT_TAG,
    _CONVERSION_PRICE_TAG,
    _CONVERTIBLE_MEASURES,
)
_FACT_FIELDS = ("cover_shares", "warrants", "convertible_principal", "conversion_price")

# (cik, latest periodic accession) -> parsed facts
_cache: BoundedLRU[tuple[int, str], CompanyFacts] = BoundedLRU(_CACHE_MAX_ENTRIES)


def clear_cache() -> None:
    """Test helper: drop cached company facts."""
    _cache.clear()


def cache_key(records: Iterable[FilingRecord]) -> str:
    """Accession of the newest periodic filing, else of the newest filing ("" if none)."""
    newest = ""
    for r in records:  # newest-first
        if r.form in _PERIODIC_FORMS:
            return r.accession
        newest = newest or r.accession
    return newest


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _rows(facts: dict[str, Any], taxonomy: str, tag: str, unit: str) -> list[dict[str, Any]]:
    node = facts.get(taxonomy, {}).get(tag, {})
    rows = node.get("units", {}).get(unit) if isinstance(node, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


@dataclass(frozen=True)
class _Pick:
    end: date
    fact: Fact | None  # None when ambiguous

    @property
    def ambiguous(self) -> bool:
        return self.fact is None


def _pick(rows: list[dict[str, Any]], today: date) -> _Pick | None:
    """The latest ``end``'s value from its latest-filed accession; None without fresh facts."""
    parsed: list[tuple[date, date, str, float]] = []
    for r in rows:
        end, filed = _parse_date(r.get("end")), _parse_date(r.get("filed"))
        try:
            value = float(r["val"])
        except (KeyError, TypeError, ValueError):
            continue
        if end is None or filed is None or (today - end).days > _STALE_DAYS:
            continue
        parsed.append((end, filed, str(r.get("accn") or ""), value))
    if not parsed:
        return None
    latest_end = max(p[0] for p in parsed)
    at_end = [p for p in parsed if p[0] == latest_end]
    _, filed, accession, value = max(at_end, key=lambda p: p[1])
    if len({p[3] for p in at_end if p[2] == accession}) > 1:
        return _Pick(end=latest_end, fact=None)
    return _Pick(end=latest_end, fact=Fact(value=value, end=latest_end, filed=filed, accession=accession))


def _measure(facts: dict[str, Any], tags: tuple[str, ...], today: date) -> _Pick | None:
    """One convertible measure: a single total tag, or its components summed at their latest ``end``."""
    picks = [p for p in (_pick(_rows(facts, "us-gaap", tag, "USD"), today) for tag in tags) if p]
    if not picks:
        return None
    end = max(p.end for p in picks)
    parts = [p for p in picks if p.end == end]
    if any(p.ambiguous for p in parts):
        return _Pick(end=end, fact=None)
    newest = max((p.fact for p in parts if p.fact), key=lambda f: f.filed)
    total = sum(p.fact.value for p in parts if p.fact)
    return _Pick(end=end, fact=Fact(value=total, end=end, filed=newest.filed, accession=newest.accession))


def _convertible_principal(facts: dict[str, Any], today: date) -> _Pick | None:
    """The freshest convertible measure; preference order breaks ``end`` ties."""
    measures = [_measure(facts, tags, today) for tags in _CONVERTIBLE_MEASURES]
    ranked = [(m.end, -i, m) for i, m in enumerate(measures) if m is not None]
    return max(ranked, key=lambda r: (r[0], r[1]))[2] if ranked else None


def parse(payload: dict[str, Any], today: date | None = None) -> CompanyFacts:
    """The scored subset of a company-facts payload."""
    today = today or date.today()
    facts = payload.get("facts")
    if not isinstance(facts, dict) or not facts:
        return NO_FACTS
    ambiguous: set[str] = set()

    def keep(p: _Pick | None, name: str) -> Fact | None:
        if p is not None and p.ambiguous:
            ambiguous.add(name)
        return p.fact if p is not None else None

    return CompanyFacts(
        ok=True,
        found=True,
        cover_shares=keep(_pick(_rows(facts, *_COVER_TAG), today), COVER_SHARES),
        warrants=keep(_pick(_rows(facts, *_WARRANT_TAG), today), WARRANTS),
        convertible_principal=keep(_convertible_principal(facts, today), CONVERTIBLES),
        conversion_price=keep(_pick(_rows(facts, *_CONVERSION_PRICE_TAG), today), CONVERSION_PRICE),
        ambiguous=frozenset(ambiguous),
    )


def _encode_fact(f: Fact | None) -> dict[str, Any] | None:
    if f is None:
        return None
    return {"value": f.value, "end": f.end.isoformat(), "filed": f.filed.isoformat(), "accession": f.accession}


def _decode_fact(d: dict[str, Any] | None) -> Fact | None:
    if d is None:
        return None
    return Fact(
        value=float(d["value"]),
        end=date.fromisoformat(d["end"]),
        filed=date.fromisoformat(d["filed"]),
        accession=str(d["accession"]),
    )


def _encode(info: CompanyFacts) -> dict[str, Any]:
    payload: dict[str, Any] = {name: _encode_fact(getattr(info, name)) for name in _FACT_FIELDS}
    payload["found"] = info.found
    payload["ambiguous"] = sorted(info.ambiguous)
    return payload


def _decode(payload: dict[str, Any]) -> CompanyFacts | None:
    try:
        return CompanyFacts(
            ok=True,
            found=bool(payload["found"]),
            ambiguous=frozenset(str(a) for a in payload["ambiguous"]),
            **{name: _decode_fact(payload[name]) for name in _FACT_FIELDS},
        )
    except (KeyError, TypeError, ValueError):
        return None


def get_company_facts(cik: int, records: Iterable[FilingRecord], *, session: Any) -> CompanyFacts:
    """Parsed company facts for ``cik``, refetched only after a new periodic filing."""
    key = (int(cik), cache_key(records))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    store_key = f"{key[0]}:{key[1]}"
    payload = sec_store.load(_STORE_KIND, store_key, _RULES_VERSION, max_age=_STORE_MAX_AGE)
    stored = _decode(payload) if payload is not None else None
    if stored is not None:
        _cache.put(key, stored)
        return stored
    try:
        payload = edgar.http_get_json(COMPANY_FACTS_URL.format(cik10=edgar.cik10(cik)), session=session)
        info = parse(payload)
    except Exception as exc:
        if edgar.failure_kind(exc) == edgar.TRANSIENT:
            logger.warning("SEC company facts lookup failed for CIK %s: %s", cik, exc)
            return FAILED_FACTS
        info = NO_FACTS  # 404: no XBRL facts for this company
    _cache.put(key, info)
    sec_store.save(_STORE_KIND, store_key, _RULES_VERSION, _encode(info))
    return info
