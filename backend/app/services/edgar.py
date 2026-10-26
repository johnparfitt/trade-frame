"""SEC EDGAR filing lookups for the watchlist Dilution Risk / SEC columns.

This module owns the *filing* half of the dilution picture (the financial half
— share issuance, buybacks, cash runway — comes from Unusual Whales via
``app.services.dilution``, which combines the two). It reads the free SEC
EDGAR APIs (no key; EDGAR only asks for a descriptive ``User-Agent`` and a
10 req/s cap):

- ticker -> CIK map: ``/files/company_tickers.json`` (one global map, cached).
- recent filings: ``/submissions/CIK##########.json`` -> ``filings.recent``.

From those it derives a few dilution-relevant filing signals (active S-3 shelf,
recent 424B offering, offering frequency, recent S-1, recent Form D) plus a
capped, newest-first list of *significant* filing candidates. Which of those
candidates is actually surfaced in the UI is decided downstream by
``app.services.sec_significance`` (it only surfaces filings scoring above a
threshold), so this module no longer picks a single "latest significant" link.

This module can also fetch a filing's primary document text (for the LLM
significance scorer). All EDGAR requests pass through a small shared throttle to
respect the 10 req/s cap.

Results are cached for ``TF_SEC_CACHE_TTL`` seconds (filings update at most
daily). Lookups never raise: a missing CIK or an EDGAR outage yields an empty
``FilingInfo`` so the rest of the row still renders.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Protocol

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

SEC_BASE = "https://www.sec.gov"
DATA_BASE = "https://data.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_BASE}/files/company_tickers.json"
SUBMISSIONS_URL = f"{DATA_BASE}/submissions/CIK{{cik10}}.json"

# Forms that count as "significant" for the SEC column: dilution + catalyst.
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

# Lookback windows for the filing signals (days).
_SHELF_DAYS = 3 * 365  # an S-3 shelf is "active" for ~3 years
_REGISTRATION_DAYS = 180  # a recent S-1
_SERIAL_WINDOW_DAYS = 2 * 365  # window for counting repeat 424B offerings

# How many newest significant filings the significance scorer may consider.
_MAX_CANDIDATES = 5
# Cap on filing-document text handed to the LLM (cover page / 8-K item text /
# offering summary live near the top; this keeps token cost bounded).
_DOC_MAX_CHARS = 50_000

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
    """Dilution-relevant filing signals + newest-first significant candidates."""

    shelf_active: bool = False  # an S-3 shelf filed within _SHELF_DAYS
    days_since_424b: int | None = None  # age of the most recent 424B* offering
    count_424b_24mo: int = 0  # number of 424B* offerings in the serial window
    recent_s1: bool = False  # an S-1 filed within _REGISTRATION_DAYS
    days_since_form_d: int | None = None  # age of the most recent Form D (D/D-A)
    candidates: tuple[FilingRef, ...] = field(default_factory=tuple)


EMPTY_FILINGS = FilingInfo()

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
    """Test helper: drop the per-symbol cache and the shared CIK map."""
    global _cik_map, _cik_expires_at
    with _cache_lock:
        _cache.clear()
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


def _http_get_json(url: str, *, session: _SessionProtocol) -> dict[str, Any]:
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


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def fetch_document_text(url: str, *, session: _SessionProtocol) -> str:
    """Fetch a filing's primary document as plain text (truncated). Never raises.

    The body is handed to the LLM significance scorer, so crude HTML stripping
    is enough. Returns "" on any failure or when there is no document URL.
    """
    if not url:
        return ""
    headers = {"User-Agent": get_settings().sec_user_agent}
    try:
        _throttle()
        response = session.get(url, headers=headers)
        response.raise_for_status()
        raw = response.text
    except Exception as exc:  # pragma: no cover - defensive; live-only path
        logger.warning("SEC EDGAR document fetch failed (%s): %s", url, exc)
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()
    return text[:_DOC_MAX_CHARS]


def _cik10(cik: int) -> str:
    return str(cik).zfill(10)


def _get_cik(symbol: str, session: _SessionProtocol) -> int | None:
    """Return the CIK for ``symbol`` (or None), loading EDGAR's map once per TTL."""
    global _cik_map, _cik_expires_at
    with _cik_lock:
        if _cik_map is None or time.monotonic() >= _cik_expires_at:
            data = _http_get_json(COMPANY_TICKERS_URL, session=session)
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


def _build_candidates(filings: list[dict[str, Any]], cik: int) -> tuple[FilingRef, ...]:
    """Newest-first significant filings, capped at ``_MAX_CANDIDATES``."""
    refs: list[FilingRef] = []
    for f in filings:
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


def _most_recent(
    filings: list[dict[str, Any]], pred: Callable[[str], bool]
) -> dict[str, Any] | None:
    """First filing (newest-first) whose form matches ``pred``."""
    for f in filings:
        if pred(f["form"]):
            return f
    return None


def _build_filings(cik: int, session: _SessionProtocol, today: date) -> FilingInfo:
    submissions = _http_get_json(SUBMISSIONS_URL.format(cik10=_cik10(cik)), session=session)
    filings = _parse_recent_filings(submissions)

    shelf = _most_recent(filings, lambda f: f.startswith("S-3"))
    shelf_active = shelf is not None and (today - shelf["date"]).days <= _SHELF_DAYS

    recent_424b = _most_recent(filings, lambda f: f.startswith("424B"))
    days_since_424b = (today - recent_424b["date"]).days if recent_424b is not None else None

    count_424b = sum(
        1
        for f in filings
        if f["form"].startswith("424B") and 0 <= (today - f["date"]).days <= _SERIAL_WINDOW_DAYS
    )

    s1 = _most_recent(filings, lambda f: f == "S-1")
    recent_s1 = s1 is not None and 0 <= (today - s1["date"]).days <= _REGISTRATION_DAYS

    form_d = _most_recent(filings, lambda f: f in _FORM_D_FORMS)
    days_since_form_d = (today - form_d["date"]).days if form_d is not None else None

    return FilingInfo(
        shelf_active=shelf_active,
        days_since_424b=days_since_424b,
        count_424b_24mo=count_424b,
        recent_s1=recent_s1,
        days_since_form_d=days_since_form_d,
        candidates=_build_candidates(filings, cik),
    )


def get_filings(symbol: str, *, session: _SessionProtocol | None = None) -> FilingInfo:
    """Return SEC filing signals + link for ``symbol``; empty when unavailable."""
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
        return EMPTY_FILINGS
    finally:
        if owned is not None:
            owned.close()

    _cache_put(symbol, info)
    return info
