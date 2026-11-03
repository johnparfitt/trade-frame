"""Keyword checks on SEC filing text for the Dilution Risk score.

Each check reads one filing's primary document via
``edgar.fetch_document_text`` and answers found (True), not found (False), or
unknown (None):

- ``check_offering``: a 424B prospectus cover page — is it an at-the-market
  (ATM) program, and is it a debt takedown (notes/bonds, not stock)?
- ``check_proxy``: a DEF 14A / PRE 14A — does it propose an authorized-share
  increase and/or a reverse split (not just recount a past one)?
- ``check_reverse_split_8k``: an 8-K Item 5.03 — does it carry out a reverse
  split?

A transient fetch failure (network, timeout, 403, 429, 5xx) is unknown and is
not cached, so the next refresh retries. A permanent one (no document URL,
404, 410, other 4xx) is not found. Found / not found results are cached per
accession (filings are immutable) in memory and in SQLite (``sec_store``,
under a hash of the keyword rules, so editing them re-checks). Never raises.

Which filings to check, and what the answers are worth, is decided by
``app.services.dilution``. All keyword lists are module constants (tunable).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Callable, TypeVar

from app.services import edgar, sec_store
from app.services.edgar import FilingRecord
from app.services.lru import BoundedLRU

logger = logging.getLogger(__name__)

# Text windows. ATM and debt keywords only count on the cover page, so an
# asset-sale contract or risk-factor boilerplate deeper in the document can't
# match. Proxies put proposals anywhere in a long document.
COVER_CHARS = 10_000
PROXY_MAX_CHARS = 1_000_000

_ATM_KEYWORDS = (
    "at-the-market",
    "at the market offering",
    "equity distribution agreement",
    "sales agreement",
)
# Debt takedown = a debt keyword, no equity keyword, and no dilutive-debt keyword.
_DEBT_KEYWORDS = (
    "notes due",
    "senior notes",
    "subordinated notes",
    "debentures",
    "medium-term notes",
    "aggregate principal amount",
)
_EQUITY_KEYWORDS = (
    "shares of common stock",
    "ordinary shares",
    "american depositary shares",
    "common units",
)
_DILUTIVE_DEBT_KEYWORDS = ("convertible", "exchangeable")

_AUTHORIZED_INCREASE_PHRASES = (
    "increase the number of authorized shares",
    "increase in the number of authorized shares",
    "increase our authorized shares",
)
# The phrase must name the common stock: proxies also carry corporate-law
# boilerplate ("may increase the number of authorized shares of such class")
# and equity-plan share increases ("authorized shares that can be issued under
# the 2023 Plan"), which are stock-comp dilution, not a charter change.
_AUTHORIZED_INCREASE_RE = re.compile(
    "(?:" + "|".join(re.escape(p) for p in _AUTHORIZED_INCREASE_PHRASES) + ")"
    r" of (?:our )?(?:class [a-z] )?(?:common|capital) stock"
)
_PLAN_RE = re.compile(r"\bplan\b")
_PLAN_LOOKAHEAD = 100
_AUTHORIZED_FROM_TO_RE = re.compile(r"authorized shares of common stock from ([\d,]+) to ([\d,]+)")

_REVERSE_SPLIT_RE = re.compile(r"reverse (?:stock )?split")
# A hyphen, Unicode hyphen/dash, or space; stripped inline tags can leave a
# stray space beside the dash ("1 -for-5").
_DASH = r"\s?[-\u2010-\u2014 ]\s?"
_REVERSE_SPLIT_RATIO_RE = re.compile(
    rf"\b1{_DASH}for{_DASH}\d+\b"
    rf"|\bone{_DASH}for{_DASH}\w+\b"
    r"|\b1:\d+\b"
    rf"|\bone \(1\){_DASH}for{_DASH}\w+ \(\d+\)"
)
# The ratio must sit this close to the reverse-split phrase, so boilerplate
# plus an unrelated ratio pages later doesn't match.
_REVERSE_SPLIT_WINDOW = 300
# A proxy must also *propose* the split: proxies often recount a split the
# company already did ("the company effected a one-for-twenty reverse stock
# split", "amounts reflect the 1-for-8 reverse stock split"), which split
# history already scores.
_PROPOSAL_RE = re.compile(r"\b(?:approve|approval|proposal|proposed)\b")
_PAST_SPLIT_RE = re.compile(r"\b(?:effected|reflects?|retroactively|adjusted for)\b")

_CACHE_MAX_ENTRIES = 4096

# Bump when check logic changes in a way the constants below don't capture.
_LOGIC_REVISION = 1
_RULES_VERSION = sec_store.rules_version(
    _LOGIC_REVISION,
    COVER_CHARS,
    PROXY_MAX_CHARS,
    edgar.DOC_MAX_CHARS,
    _ATM_KEYWORDS,
    _DEBT_KEYWORDS,
    _EQUITY_KEYWORDS,
    _DILUTIVE_DEBT_KEYWORDS,
    _AUTHORIZED_INCREASE_RE.pattern,
    _PLAN_RE.pattern,
    _PLAN_LOOKAHEAD,
    _AUTHORIZED_FROM_TO_RE.pattern,
    _REVERSE_SPLIT_RE.pattern,
    _REVERSE_SPLIT_RATIO_RE.pattern,
    _REVERSE_SPLIT_WINDOW,
    _PROPOSAL_RE.pattern,
    _PAST_SPLIT_RE.pattern,
)
_STORE_KIND = "filing_check"


@dataclass(frozen=True)
class OfferingCover:
    """What a 424B cover page says about the offering."""

    atm: bool = False
    debt: bool = False


@dataclass(frozen=True)
class ProxyProposals:
    """Dilution-relevant proposals found in a proxy statement."""

    authorized_increase: bool = False
    reverse_split: bool = False


_NO_COVER = OfferingCover()
_NO_PROPOSALS = ProxyProposals()

# (check kind, accession) -> found / not-found result. Unknowns are never stored.
_cache: BoundedLRU[tuple[str, str], Any] = BoundedLRU(_CACHE_MAX_ENTRIES)

T = TypeVar("T")


def clear_cache() -> None:
    """Test helper: drop cached check results."""
    _cache.clear()


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def classify_cover(text: str) -> OfferingCover:
    """ATM / debt-takedown flags from a 424B cover page (searched case-insensitively)."""
    cover = text[:COVER_CHARS].lower()
    debt = (
        _has_any(cover, _DEBT_KEYWORDS)
        and not _has_any(cover, _EQUITY_KEYWORDS)
        and not _has_any(cover, _DILUTIVE_DEBT_KEYWORDS)
    )
    return OfferingCover(atm=_has_any(cover, _ATM_KEYWORDS), debt=debt)


def _as_int(raw: str) -> int | None:
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None


def proposes_authorized_increase(text: str) -> bool:
    """An authorized-share increase of the common stock, or a from/to pattern that increases."""
    lower = text.lower()
    for m in _AUTHORIZED_INCREASE_RE.finditer(lower):
        if not _PLAN_RE.search(lower, m.end(), m.end() + _PLAN_LOOKAHEAD):
            return True
    for m in _AUTHORIZED_FROM_TO_RE.finditer(lower):
        old, new = _as_int(m.group(1)), _as_int(m.group(2))
        if old is not None and new is not None and new > old:
            return True
    return False


def mentions_reverse_split(text: str, *, proposed: bool = False) -> bool:
    """Reverse-split language with a split ratio within ``_REVERSE_SPLIT_WINDOW`` chars.

    With ``proposed``, proposal language must sit in the same window too, and
    no wording about a split already carried out.
    """
    lower = text.lower()
    for m in _REVERSE_SPLIT_RE.finditer(lower):
        start = max(0, m.start() - _REVERSE_SPLIT_WINDOW)
        end = m.end() + _REVERSE_SPLIT_WINDOW
        if not _REVERSE_SPLIT_RATIO_RE.search(lower, start, end):
            continue
        if not proposed:
            return True
        if _PROPOSAL_RE.search(lower, start, end) and not _PAST_SPLIT_RE.search(lower, start, end):
            return True
    return False


def _encode(result: Any) -> dict[str, Any]:
    return asdict(result) if is_dataclass(result) else {"found": bool(result)}


def _load_stored(kind: str, accession: str, decode: Callable[[dict[str, Any]], T]) -> T | None:
    payload = sec_store.load(_STORE_KIND, f"{kind}:{accession}", _RULES_VERSION)
    if payload is None:
        return None
    try:
        return decode(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _check(
    kind: str,
    record: FilingRecord,
    *,
    session: Any,
    max_chars: int,
    evaluate: Callable[[str], T],
    negative: T,
    decode: Callable[[dict[str, Any]], T],
) -> T | None:
    key = (kind, record.accession)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    if record.accession:
        stored = _load_stored(kind, record.accession, decode)
        if stored is not None:
            _cache.put(key, stored)
            return stored
    if not record.doc_url:
        result = negative
    else:
        doc = edgar.fetch_document_text(record.doc_url, session=session, max_chars=max_chars)
        if doc.failure == edgar.TRANSIENT:
            return None
        try:
            result = negative if doc.failure else evaluate(doc.text)
        except Exception as exc:  # a parsing bug must not blank the row
            logger.warning("Filing check %s failed for %s: %s", kind, record.accession, exc)
            return None
    _cache.put(key, result)
    if record.accession:
        sec_store.save(_STORE_KIND, f"{kind}:{record.accession}", _RULES_VERSION, _encode(result))
    return result


def check_offering(record: FilingRecord, *, session: Any) -> OfferingCover | None:
    """Classify a 424B5/424B4/424B3 from its cover page; None when the text is unavailable."""
    return _check(
        "offering",
        record,
        session=session,
        max_chars=COVER_CHARS,
        evaluate=classify_cover,
        negative=_NO_COVER,
        decode=lambda d: OfferingCover(atm=bool(d["atm"]), debt=bool(d["debt"])),
    )


def check_proxy(record: FilingRecord, *, session: Any) -> ProxyProposals | None:
    """Proposals in a DEF 14A / PRE 14A; None when the text is unavailable."""
    return _check(
        "proxy",
        record,
        session=session,
        max_chars=PROXY_MAX_CHARS,
        evaluate=lambda text: ProxyProposals(
            authorized_increase=proposes_authorized_increase(text),
            reverse_split=mentions_reverse_split(text, proposed=True),
        ),
        negative=_NO_PROPOSALS,
        decode=lambda d: ProxyProposals(
            authorized_increase=bool(d["authorized_increase"]),
            reverse_split=bool(d["reverse_split"]),
        ),
    )


def check_reverse_split_8k(record: FilingRecord, *, session: Any) -> bool | None:
    """Whether an 8-K Item 5.03 describes a reverse split; None when the text is unavailable."""
    return _check(
        "reverse_split_8k",
        record,
        session=session,
        max_chars=edgar.DOC_MAX_CHARS,
        evaluate=mentions_reverse_split,
        negative=False,
        decode=lambda d: bool(d["found"]),
    )
