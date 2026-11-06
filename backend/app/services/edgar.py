"""SEC EDGAR filing lookups for the watchlist SEC column.

It reads the free SEC EDGAR APIs (no key; EDGAR only asks for a descriptive
``User-Agent`` and a 10 req/s cap):

- ticker -> CIK map: ``/files/company_tickers.json`` (one global map, cached).
- recent filings: ``/submissions/CIK##########.json`` -> ``filings.recent``.

From those it returns a capped, newest-first list of *significant* filing
candidates. This module holds no scoring logic. Which of those candidates is
actually surfaced in the UI is decided downstream by
``app.services.sec_significance`` (it only surfaces filings scoring above a
threshold).

This module can also fetch a filing's primary document text for the LLM
significance scorer, keeping a small LRU of recent texts, and exposes the
throttled JSON GET. All EDGAR requests pass through a small shared throttle
to respect the 10 req/s cap.

Results are cached for ``TF_SEC_CACHE_TTL`` seconds (filings update at most
daily). Lookups never raise: a missing CIK yields an empty ``FilingInfo``
(``found=False``) and an EDGAR outage a failed one (``ok=False``, not cached),
so the rest of the row still renders and callers can tell the two apart.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.services.lru import BoundedLRU

logger = logging.getLogger(__name__)

SEC_BASE = "https://www.sec.gov"
DATA_BASE = "https://data.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_BASE}/files/company_tickers.json"
SUBMISSIONS_URL = f"{DATA_BASE}/submissions/CIK{{cik10}}.json"

# Forms that count as "significant" for the SEC column.
# Everything else (Form 4, 13F, SC 13D/G, ...) is noise for this feature.
# Form D (private placements) is handled separately in _is_significant so the
# "D" prefix can't swallow DEF 14A / DEFA14A.
_SIGNIFICANT_FORMS = {
    "10-K",
    "10-Q",
    "8-K",
    "S-1",
    "S-3",
    "S-3ASR",
    "S-8",
    "DEF 14A",
    "PRE 14A",
    "DEF 14C",
    "PRE 14C",
}
_FORM_D_FORMS = {"D", "D/A"}

# How many newest in-window significant filings the significance scorer may
# consider. Older forms are not counted, so a stale filing cannot crowd out
# one from the last TF_SEC_SIGNIFICANCE_RECENT_DAYS days.
_MAX_CANDIDATES = 12
# Default cap on stripped document text (cover page / 8-K item text / offering
# summary live near the top; this keeps LLM token cost bounded).
DOC_MAX_CHARS = 50_000
# Recently fetched document texts (url -> (text, chars kept)). Filings are
# immutable; this only needs to span one scoring pass, so it stays small: proxy
# texts can be ~1M characters.
_DOC_CACHE_MAX_ENTRIES = 32
_doc_cache: BoundedLRU[str, tuple[str, int]] = BoundedLRU(_DOC_CACHE_MAX_ENTRIES)

# Document-fetch failure kinds. Transient failures (network, timeout, 403 SEC
# throttling or a bad User-Agent, 429, 5xx) may succeed on retry and must not
# be cached; permanent ones (no URL, 404, 410, other 4xx) will not.
TRANSIENT = "transient"
PERMANENT = "permanent"

# Shared client-side throttle: EDGAR caps callers at 10 req/s, so keep a little
# headroom. Opening filing documents adds requests on top of the 2 JSON calls
# per symbol, so every outbound EDGAR GET passes through here.
_MIN_INTERVAL = 1.0 / 8.0
_rate_lock = threading.Lock()
_last_call = 0.0


class _SessionProtocol(Protocol):
    def get(
        self, url: str, *, headers: dict[str, str] | None = ..., params: Any = ...
    ) -> httpx.Response: ...


@dataclass(frozen=True)
class FilingRef:
    """A single significant filing: enough to link, fetch, and score it."""

    form: str
    date: str  # ISO filing date
    url: str  # EDGAR index page (the human link)
    accession: str
    primary_doc: str  # primary document filename (for fetching the body)
    doc_url: str = ""  # full URL to the primary document ("" when none)
    items: tuple[str, ...] = ()  # 8-K item codes, e.g. ("2.02", "9.01")


@dataclass(frozen=True)
class FilingInfo:
    """Significant-filing candidates for one symbol.

    ``ok`` is False when the lookup failed (outage), as opposed to a symbol
    that simply has no CIK (``found`` False, ``ok`` True).
    """

    ok: bool = True
    found: bool = False
    candidates: tuple[FilingRef, ...] = field(default_factory=tuple)
    cik: int | None = None


@dataclass(frozen=True)
class DocText:
    """A fetched document's stripped text, or why it could not be fetched."""

    text: str = ""
    failure: str | None = None  # None, TRANSIENT, or PERMANENT


EMPTY_FILINGS = FilingInfo()
FAILED_FILINGS = FilingInfo(ok=False)

# symbol -> (expires_at, info)
_cache: dict[str, tuple[float, FilingInfo]] = {}
_cache_lock = threading.Lock()

# Global ticker -> CIK map (one EDGAR file for every symbol).
_cik_map: dict[str, int] | None = None
_cik_expires_at: float = 0.0
_cik_lock = threading.Lock()


def _cache_get(symbol: str) -> FilingInfo | None:
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        expires_at, info = entry
        if time.monotonic() >= expires_at:
            _cache.pop(symbol, None)
            return None
        return info


def _cache_put(symbol: str, info: FilingInfo) -> None:
    with _cache_lock:
        _cache[symbol] = (time.monotonic() + get_settings().sec_cache_ttl, info)


def clear_cache() -> None:
    """Test helper: drop the per-symbol cache, document texts, and the CIK map."""
    global _cik_map, _cik_expires_at
    with _cache_lock:
        _cache.clear()
    _doc_cache.clear()
    with _cik_lock:
        _cik_map = None
        _cik_expires_at = 0.0


def _throttle() -> None:
    """Block briefly so outbound EDGAR requests stay under the 10 req/s cap."""
    global _last_call
    with _rate_lock:
        now = time.monotonic()
        wait = _last_call + _MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def http_get_json(url: str, *, session: _SessionProtocol) -> dict[str, Any]:
    """Throttled EDGAR JSON GET. Raises on HTTP or network errors (see ``failure_kind``)."""
    headers = {
        "User-Agent": get_settings().sec_user_agent,
        "Accept": "application/json",
    }
    _throttle()
    response = session.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def _document_url(cik: int, accession: str, primary_doc: str) -> str:
    acc_nodash = accession.replace("-", "")
    return f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{acc_nodash}/{primary_doc}"


# Blocks whose *content* is not filing prose: CSS, JS, and the Inline XBRL
# header (hidden facts that can push an iXBRL cover page far down the text).
_HIDDEN_BLOCK_RE = re.compile(
    r"<(style|script|ix:header)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(raw: str) -> str:
    """Crude HTML -> plain text: drop hidden blocks and tags, decode entities."""
    text = _TAG_RE.sub(" ", _HIDDEN_BLOCK_RE.sub(" ", raw))
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def failure_kind(exc: Exception) -> str:
    """``TRANSIENT`` (network, timeout, 403, 429, 5xx) or ``PERMANENT`` (other 4xx)."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (403, 429) or status >= 500:
            return TRANSIENT
        return PERMANENT
    return TRANSIENT


def fetch_document_text(
    url: str, *, session: _SessionProtocol, max_chars: int = DOC_MAX_CHARS
) -> DocText:
    """Fetch a filing's primary document as stripped, truncated text. Never raises.

    On failure the text is empty and ``failure`` says whether a retry could
    help (``TRANSIENT``) or not (``PERMANENT``, including a missing URL).
    """
    if not url:
        return DocText(failure=PERMANENT)
    cached = _doc_cache.get(url)
    if cached is not None and max_chars <= cached[1]:
        return DocText(text=cached[0][:max_chars])
    headers = {"User-Agent": get_settings().sec_user_agent}
    try:
        _throttle()
        response = session.get(url, headers=headers)
        response.raise_for_status()
        raw = response.text
    except Exception as exc:
        kind = failure_kind(exc)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
            logger.warning(
                "SEC EDGAR returned 403 for %s; check TF_SEC_USER_AGENT or throttling", url
            )
        else:
            logger.warning("SEC EDGAR document fetch failed (%s, %s): %s", url, kind, exc)
        return DocText(failure=kind)
    kept = max(max_chars, DOC_MAX_CHARS)
    text = strip_html(raw)[:kept]
    _doc_cache.put(url, (text, kept))
    return DocText(text=text[:max_chars])


def cik10(cik: int) -> str:
    return str(cik).zfill(10)


def _get_cik(symbol: str, session: _SessionProtocol) -> int | None:
    """Return the CIK for ``symbol`` (or None), loading EDGAR's map once per TTL."""
    global _cik_map, _cik_expires_at
    with _cik_lock:
        if _cik_map is None or time.monotonic() >= _cik_expires_at:
            data = http_get_json(COMPANY_TICKERS_URL, session=session)
            mapping: dict[str, int] = {}
            for row in data.values():
                if not isinstance(row, dict):
                    continue
                ticker = row.get("ticker")
                cik = row.get("cik_str")
                if ticker and cik is not None:
                    try:
                        mapping[str(ticker).upper()] = int(cik)
                    except (TypeError, ValueError):
                        continue
            _cik_map = mapping
            _cik_expires_at = time.monotonic() + get_settings().sec_cache_ttl
        return _cik_map.get(symbol)


def _parse_recent_filings(submissions: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``filings.recent`` into a list of filings sorted newest-first."""
    recent = submissions.get("filings", {})
    recent = recent.get("recent", {}) if isinstance(recent, dict) else {}
    forms = recent.get("form", []) or []
    dates = recent.get("filingDate", []) or []
    accessions = recent.get("accessionNumber", []) or []
    primary = recent.get("primaryDocument", []) or []
    items = recent.get("items", []) or []
    out: list[dict[str, Any]] = []
    for i in range(min(len(forms), len(dates), len(accessions))):
        try:
            filed = date.fromisoformat(dates[i])
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "form": str(forms[i]).upper(),
                "date": filed,
                "iso": dates[i],
                "accession": accessions[i],
                "primary_doc": primary[i] if i < len(primary) else "",
                "items": _parse_items(items[i] if i < len(items) else ""),
            }
        )
    out.sort(key=lambda f: f["date"], reverse=True)
    return out


def _parse_items(raw: Any) -> tuple[str, ...]:
    """Split an 8-K ``items`` string (e.g. '2.02,9.01') into a tuple."""
    if not raw:
        return ()
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def _is_significant(form: str) -> bool:
    form = form.upper()
    return form in _SIGNIFICANT_FORMS or form.startswith("424B") or form in _FORM_D_FORMS


def _filing_url(cik: int, accession: str) -> str:
    acc_nodash = accession.replace("-", "")
    return f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{acc_nodash}/{accession}-index.htm"


def _build_candidates(
    filings: list[dict[str, Any]], cik: int, today: date
) -> tuple[FilingRef, ...]:
    """Newest-first significant filings inside the significance window.

    The list stops at ``_MAX_CANDIDATES``. Filings are newest-first, so the
    first one older than the window ends the scan and does not take a slot.
    """
    window = get_settings().sec_significance_recent_days
    refs: list[FilingRef] = []
    for f in filings:
        if (today - f["date"]).days > window:
            break
        if not _is_significant(f["form"]):
            continue
        primary_doc = f.get("primary_doc", "")
        doc_url = _document_url(cik, f["accession"], primary_doc) if primary_doc else ""
        refs.append(
            FilingRef(
                form=f["form"],
                date=f["iso"],
                url=_filing_url(cik, f["accession"]),
                accession=f["accession"],
                primary_doc=primary_doc,
                doc_url=doc_url,
                items=f.get("items", ()),
            )
        )
        if len(refs) >= _MAX_CANDIDATES:
            break
    return tuple(refs)


def _build_filings(cik: int, session: _SessionProtocol, today: date) -> FilingInfo:
    submissions = http_get_json(SUBMISSIONS_URL.format(cik10=cik10(cik)), session=session)
    filings = _parse_recent_filings(submissions)
    return FilingInfo(
        ok=True,
        found=True,
        candidates=_build_candidates(filings, cik, today),
        cik=cik,
    )


def get_filings(symbol: str, *, session: _SessionProtocol | None = None) -> FilingInfo:
    """Return significant-filing candidates for ``symbol``.

    Empty (``found=False``) when the symbol has no CIK; failed (``ok=False``,
    not cached) on an EDGAR outage.
    """
    symbol = symbol.strip().upper()
    cached = _cache_get(symbol)
    if cached is not None:
        return cached

    owned: httpx.Client | None = None
    try:
        active = session
        if active is None:
            owned = httpx.Client(timeout=30.0)
            active = owned
        cik = _get_cik(symbol, active)
        info = EMPTY_FILINGS if cik is None else _build_filings(cik, active, date.today())
    except Exception as exc:
        # An EDGAR outage must not blank out the rest of the watchlist row, and
        # must not be cached so the next refresh can retry.
        logger.warning("SEC EDGAR lookup failed for %s: %s", symbol, exc)
        return FAILED_FILINGS
    finally:
        if owned is not None:
            owned.close()

    _cache_put(symbol, info)
    return info
