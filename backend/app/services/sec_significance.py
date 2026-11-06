"""LLM-scored significance of a stock's recent *meaningful* SEC filings.

The watchlist SEC column is deliberately low-noise: it stays blank unless a
genuinely significant filing exists. Given the newest-first significant filing
candidates from ``app.services.edgar``, this module scores each (an LLM reads
the filing text and rates it 0-100) and surfaces every one filed within
``TF_SEC_SIGNIFICANCE_RECENT_DAYS`` that scores above
``TF_SEC_SIGNIFICANCE_MIN`` (default 50). Worthless filings are skipped, so a
real filing is never hidden behind a junk one filed moments later; if nothing
clears the bar, the column is blank. The cell still leads with the newest
qualifier.

Scoring is cached per accession: in memory, and LLM scores also in SQLite so a
restart doesn't pay for them again. Steady state is about one LLM call per
genuinely new filing in the window. A confirmed second read
(``apply_review``) replaces that stored score. A combined read of two or more
qualifiers is separate (``cluster_brief``) and runs only when asked, as does
the second read itself (``review_filing``). Everything in ``evaluate`` is
resilient: a missing key, disabled scoring, an outage, or an unparseable
response falls back to a deterministic form/item heuristic, and ``evaluate``
never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

import httpx
from sqlalchemy.orm import Session

from app.config import BACKEND_DIR, get_settings
from app.db.models import SecFilingBrief, SecSignificanceScore
from app.db.session import SessionLocal
from app.services import edgar, llm
from app.services.edgar import FilingRef
from app.services.lru import BoundedLRU

logger = logging.getLogger(__name__)

PROMPT_PATH = BACKEND_DIR.parent / "prompts" / "sec" / "significance.md"
CLUSTER_PROMPT_PATH = BACKEND_DIR.parent / "prompts" / "sec" / "cluster.md"
REVIEW_PROMPT_PATH = BACKEND_DIR.parent / "prompts" / "sec" / "review.md"
# Combined read only. The per-filing scorer keeps the larger document cap.
CLUSTER_DOC_CHARS = 8_000

# Heuristic fallback scores by form (used when the LLM is unavailable). Each
# entry is (score, direction, reason); still subject to the >min gate, so
# routine forms stay blank. Direction mirrors the LLM contract: supply/dilution
# events skew bearish, most procedural filings are neutral.
_FORM_HEURISTIC = {
    "S-1": (70, "bearish", "new registration (supply)"),
    "S-3": (55, "bearish", "shelf registration (supply)"),
    "S-3ASR": (55, "bearish", "shelf registration (supply)"),
    "S-8": (20, "neutral", "employee stock plan"),
    "D": (70, "bearish", "private placement (Form D)"),
    "D/A": (65, "bearish", "private placement amendment"),
    "10-K": (45, "neutral", "annual report"),
    "10-Q": (30, "neutral", "quarterly report"),
    "DEF 14A": (35, "neutral", "proxy statement"),
    "PRE 14A": (40, "neutral", "preliminary proxy"),
    "DEF 14C": (45, "neutral", "information statement (written consent)"),
    "PRE 14C": (45, "neutral", "preliminary information statement"),
}
# 8-K significance by item code (highest-scoring matching item wins). Earnings
# (2.02) is kept well below the surface threshold since earnings are ignored.
_ITEM_HEURISTIC = {
    "1.03": (75, "bearish", "bankruptcy"),
    "2.01": (70, "neutral", "completed acquisition/disposition"),
    "1.01": (65, "neutral", "material definitive agreement"),
    "3.02": (70, "bearish", "unregistered equity sale (dilution)"),
    "2.02": (25, "neutral", "results of operations"),
    "5.02": (50, "neutral", "management/board change"),
    "4.02": (75, "bearish", "non-reliance on prior financials"),
    "3.01": (70, "bearish", "delisting/listing-rule notice"),
}
_DEFAULT_HEURISTIC = (25, "neutral", "routine filing")

# Allowed direction values; anything else from the model normalizes to neutral.
_DIRECTIONS = ("bullish", "bearish", "neutral")

_FENCE_RE = re.compile(r"```(?:json)?|```", re.IGNORECASE)
_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class SecFiling:
    """One filing that cleared the significance bar."""

    score: int
    direction: str
    reason: str
    form: str
    date: str
    url: str


@dataclass(frozen=True)
class SigResult:
    """The newest surfaced filing, plus every qualifier in the window."""

    score: int | None = None
    note: str | None = None
    direction: str | None = None  # "bullish" | "bearish" | "neutral"
    url: str | None = None
    form: str | None = None
    date: str | None = None
    filings: tuple[SecFiling, ...] = ()


class InsufficientFilings(Exception):
    """Fewer than two filings cleared the bar in the current window."""


class BriefUnavailable(Exception):
    """The combined read failed. Nothing was persisted."""


class FilingNotFound(Exception):
    """No in-window filing for this symbol has the given URL."""


class ReviewUnavailable(Exception):
    """The second read failed, or the replacement score could not be stored."""


@dataclass(frozen=True)
class Brief:
    text: str
    generated_at: datetime


@dataclass(frozen=True)
class Review:
    """A second read of one filing. Not stored until the user confirms it."""

    agrees: bool
    score: int
    direction: str
    reason: str
    will_show: bool


@dataclass(frozen=True)
class _Hit:
    ref: FilingRef
    score: int
    direction: str
    reason: str


EMPTY = SigResult()

# accession -> (score, direction, reason) for already-scored filings. A confirmed
# review replaces the entry. The bound only evicts the least recent.
_CACHE_MAX_ENTRIES = 4096
_cache: BoundedLRU[str, tuple[int, str, str]] = BoundedLRU(_CACHE_MAX_ENTRIES)


_session_factory: Callable[[], Session] = SessionLocal


def clear_cache() -> None:
    """Test helper: drop the in-memory per-accession score cache."""
    _cache.clear()


def set_session_factory(factory: Callable[[], Session]) -> None:
    """Point the persisted score store at another database (tests)."""
    global _session_factory
    _session_factory = factory


def _prompt_key() -> str | None:
    """Hash of the model + prompt behind stored scores; None skips the store."""
    try:
        raw = f"{get_settings().llm_model}\n{_load_prompt()}"
    except Exception as exc:
        logger.warning("SEC significance: prompt unavailable for the score store: %s", exc)
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load_stored(accession: str, key: str) -> tuple[int, str, str] | None:
    """A persisted LLM score for ``accession`` under ``key``, else None. Never raises."""
    try:
        with _session_factory() as db:
            row = db.get(SecSignificanceScore, accession)
            if row is None or row.prompt_key != key:
                return None
            return (row.score, row.direction, row.reason)
    except Exception as exc:
        logger.warning("SEC significance: stored score lookup failed for %s: %s", accession, exc)
        return None


def _save_stored(accession: str, key: str, result: tuple[int, str, str]) -> bool:
    """Persist a score. Returns False when the write failed."""
    score, direction, reason = result
    try:
        with _session_factory() as db:
            db.merge(
                SecSignificanceScore(
                    accession=accession,
                    prompt_key=key,
                    score=score,
                    direction=direction,
                    reason=reason,
                    scored_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        return True
    except Exception as exc:
        logger.warning("SEC significance: storing score failed for %s: %s", accession, exc)
        return False


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def _clamp_score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _normalize_direction(value: Any) -> str:
    """Coerce a model-supplied direction to one of ``_DIRECTIONS`` (default neutral)."""
    text = str(value or "").strip().lower()
    return text if text in _DIRECTIONS else "neutral"


def _heuristic(form: str, items: Sequence[str]) -> tuple[int, str, str]:
    """Deterministic fallback (score, direction, reason) from form and 8-K items."""
    form = (form or "").upper()
    if form == "8-K":
        best: tuple[int, str, str] | None = None
        for item in items:
            hit = _ITEM_HEURISTIC.get(item.strip())
            if hit is not None and (best is None or hit[0] > best[0]):
                best = hit
        if best is not None:
            return best
        return (35, "neutral", "current report (8-K)")
    for prefix in ("424B",):
        if form.startswith(prefix):
            return (70, "bearish", "prospectus / offering (supply)")
    return _FORM_HEURISTIC.get(form, _DEFAULT_HEURISTIC)


def _parse_llm_json(text: str) -> tuple[int, str, str] | None:
    """Extract ``{"score", "direction", "reason"}`` from model output, or None."""
    cleaned = _FENCE_RE.sub("", text).strip()
    match = _OBJ_RE.search(cleaned)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "score" not in obj:
        return None
    reason = str(obj.get("reason") or "").strip()
    return (
        _clamp_score(obj.get("score")),
        _normalize_direction(obj.get("direction")),
        (reason or "no reason given"),
    )


def _score_one(
    cand: FilingRef,
    *,
    session: Any = None,
    llm_client: Any = None,
) -> tuple[int, str, str]:
    """Score one filing (cached by accession). Never raises."""
    hit = _cache.get(cand.accession)
    if hit is not None:
        return hit

    key = _prompt_key()
    stored = _load_stored(cand.accession, key) if key is not None else None
    if stored is not None:
        _cache.put(cand.accession, stored)
        return stored

    result, cacheable, from_llm = _score_one_uncached(cand, session=session, llm_client=llm_client)

    if cacheable:
        _cache.put(cand.accession, result)
    if from_llm and key is not None:
        # Only real LLM scores persist: a heuristic fallback (no key, outage)
        # should be re-scored once the LLM is available again.
        _save_stored(cand.accession, key, result)
    return result


def _score_one_uncached(
    cand: FilingRef, *, session: Any, llm_client: Any
) -> tuple[tuple[int, str, str], bool, bool]:
    """Return ``(result, cacheable, from_llm)``.

    A transient document failure is not cacheable; only a parsed LLM response
    is ``from_llm``.
    """
    settings = get_settings()
    if not settings.sec_significance_enabled:
        return _heuristic(cand.form, cand.items), True, False
    try:
        doc = edgar.fetch_document_text(cand.doc_url, session=session)
        if not doc.text:
            return _heuristic(cand.form, cand.items), doc.failure != edgar.TRANSIENT, False
        prompt = _build_prompt(cand, doc.text)
        raw = llm.run(prompt, client=llm_client)
        parsed = _parse_llm_json(raw)
        if parsed is None:
            logger.warning(
                "SEC significance: unparseable LLM output for %s (%s)",
                cand.accession,
                cand.form,
            )
            return _heuristic(cand.form, cand.items), True, False
        return parsed, True, True
    except Exception as exc:  # LLM/key/outage -> deterministic fallback
        logger.warning("SEC significance scoring failed for %s: %s", cand.accession, exc)
        return _heuristic(cand.form, cand.items), True, False


def _build_prompt(cand: FilingRef, text: str) -> str:
    items = ", ".join(cand.items) if cand.items else "none"
    header = (
        f"Form: {cand.form}\n"
        f"Filing date: {cand.date}\n"
        f"8-K items: {items}\n\n"
        "Filing text (truncated):\n"
    )
    return f"{_load_prompt()}\n\n{header}{text}"


def _build_note(score: int, direction: str, cand: FilingRef, reason: str) -> str:
    return "\n".join(
        [
            f"Significance {score}/100 ({direction})",
            f"{cand.form} filed {cand.date}",
            reason,
        ]
    )


def _collect(
    candidates: Sequence[FilingRef],
    *,
    session: Any = None,
    llm_client: Any = None,
) -> tuple[_Hit, ...]:
    """Score every in-window candidate and keep those above the minimum.

    Candidates are newest-first. The first one older than the window ends the
    scan, so a stale filing is never sent to the LLM. Never raises.
    """
    settings = get_settings()
    threshold = settings.sec_significance_min
    today = date.today()
    recent_days = settings.sec_significance_recent_days

    owned: httpx.Client | None = None
    active = session
    hits: list[_Hit] = []
    try:
        for cand in candidates:
            try:
                age = (today - date.fromisoformat(cand.date)).days
            except (TypeError, ValueError):
                continue  # unparseable date: skip, don't crash
            if age > recent_days:
                break

            is_cached = cand.accession in _cache
            # Open an EDGAR client lazily, only when a real (uncached, enabled)
            # document fetch is about to happen.
            if (
                not is_cached
                and active is None
                and owned is None
                and settings.sec_significance_enabled
            ):
                owned = httpx.Client(timeout=30.0)
                active = owned

            score, direction, reason = _score_one(
                cand, session=active, llm_client=llm_client
            )
            if score > threshold:
                hits.append(_Hit(ref=cand, score=score, direction=direction, reason=reason))
        return tuple(hits)
    finally:
        if owned is not None:
            owned.close()


def _to_filing(hit: _Hit) -> SecFiling:
    return SecFiling(
        score=hit.score,
        direction=hit.direction,
        reason=hit.reason,
        form=hit.ref.form,
        date=hit.ref.date,
        url=hit.ref.url,
    )


def lookup(symbol: str) -> SigResult:
    """SEC column for ``symbol``. Blank on a quiet window or an EDGAR outage.

    Never raises. Successful filing lists are cached inside ``edgar`` for
    ``TF_SEC_CACHE_TTL``. A failed lookup is not cached, so the next snapshot
    retries.
    """
    filings = edgar.get_filings(symbol)
    return evaluate(symbol, filings.candidates)


def evaluate(
    symbol: str,
    candidates: Sequence[FilingRef],
    *,
    session: Any = None,
    llm_client: Any = None,
) -> SigResult:
    """Surface every in-window filing scoring above the threshold, else blank.

    The top-level fields are the newest qualifier. ``filings`` is newest-first
    and omits scores at or below ``sec_significance_min``.
    """
    hits = _collect(candidates, session=session, llm_client=llm_client)
    if not hits:
        return EMPTY
    newest = hits[0]
    return SigResult(
        score=newest.score,
        note=_build_note(newest.score, newest.direction, newest.ref, newest.reason),
        direction=newest.direction,
        url=newest.ref.url,
        form=newest.ref.form,
        date=newest.ref.date,
        filings=tuple(_to_filing(hit) for hit in hits),
    )


def _load_cluster_prompt() -> str:
    return CLUSTER_PROMPT_PATH.read_text(encoding="utf-8").strip()


def _brief_key(accessions: Sequence[str]) -> str | None:
    """Hash of sorted accessions + model + cluster prompt. None skips the store."""
    try:
        raw = f"{get_settings().llm_model}\n{_load_cluster_prompt()}\n" + "\n".join(sorted(accessions))
    except Exception as exc:
        logger.warning("SEC cluster brief: prompt unavailable for the store: %s", exc)
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_brief(key: str) -> Brief | None:
    """A persisted combined read for ``key``, else None. Never raises."""
    try:
        with _session_factory() as db:
            row = db.get(SecFilingBrief, key)
            if row is None:
                return None
            return Brief(text=row.text, generated_at=row.generated_at)
    except Exception as exc:
        logger.warning("SEC cluster brief: stored lookup failed: %s", exc)
        return None


def _save_brief(key: str, text: str, generated_at: datetime) -> None:
    try:
        with _session_factory() as db:
            db.merge(SecFilingBrief(filings_key=key, text=text, generated_at=generated_at))
            db.commit()
    except Exception as exc:
        logger.warning("SEC cluster brief: storing failed: %s", exc)


def _build_cluster_prompt(hits: Sequence[_Hit], *, session: Any) -> str:
    blocks = [_load_cluster_prompt(), ""]
    for index, hit in enumerate(hits, start=1):
        doc = edgar.fetch_document_text(
            hit.ref.doc_url, session=session, max_chars=CLUSTER_DOC_CHARS
        )
        text = (doc.text or "")[:CLUSTER_DOC_CHARS] or "(filing text unavailable)"
        blocks.append(
            "\n".join(
                [
                    f"Filing {index}",
                    f"Form: {hit.ref.form}",
                    f"Filing date: {hit.ref.date}",
                    f"Individual score: {hit.score}/100 ({hit.direction})",
                    f"Individual reason: {hit.reason}",
                    "",
                    "Filing text (truncated):",
                    text,
                ]
            )
        )
    return "\n\n".join(blocks)


def cluster_brief(
    symbol: str,
    *,
    session: Any = None,
    llm_client: Any = None,
) -> Brief:
    """Combined read of the current in-window qualifiers. Not used by snapshots.

    Raises ``InsufficientFilings`` when fewer than two filings clear the bar.
    Raises ``BriefUnavailable`` when the LLM call fails; that path does not
    write a row. A stored brief for the same accessions and prompt is returned
    without another model call.
    """
    info = edgar.get_filings(symbol)
    hits = _collect(info.candidates, session=session, llm_client=llm_client)
    if len(hits) < 2:
        raise InsufficientFilings(symbol)

    key = _brief_key(hit.ref.accession for hit in hits)
    if key is not None:
        stored = _load_brief(key)
        if stored is not None:
            return stored

    owned: httpx.Client | None = None
    active = session
    if active is None:
        owned = httpx.Client(timeout=30.0)
        active = owned
    try:
        prompt = _build_cluster_prompt(hits, session=active)
        try:
            text = llm.run(prompt, client=llm_client).strip()
        except Exception as exc:
            logger.warning("SEC cluster brief failed for %s: %s", symbol, exc)
            raise BriefUnavailable(str(exc)) from exc
        if not text:
            raise BriefUnavailable("empty analysis")
        generated_at = datetime.now(timezone.utc)
        if key is not None:
            _save_brief(key, text, generated_at)
        return Brief(text=text, generated_at=generated_at)
    finally:
        if owned is not None:
            owned.close()


def _load_review_prompt() -> str:
    return REVIEW_PROMPT_PATH.read_text(encoding="utf-8").strip()


def _will_show(score: int) -> bool:
    return score > get_settings().sec_significance_min


def _candidate_for_url(symbol: str, url: str, *, session: Any) -> FilingRef:
    """The in-window candidate whose index URL is ``url``."""
    info = edgar.get_filings(symbol, session=session)
    settings = get_settings()
    today = date.today()
    for cand in info.candidates:
        try:
            age = (today - date.fromisoformat(cand.date)).days
        except (TypeError, ValueError):
            continue
        if age > settings.sec_significance_recent_days:
            break
        if cand.url == url:
            return cand
    raise FilingNotFound(url)


def _parse_review(text: str) -> tuple[bool, int, str, str] | None:
    """Extract ``{agrees, score, direction, reason}`` from model output, or None."""
    cleaned = _FENCE_RE.sub("", text).strip()
    match = _OBJ_RE.search(cleaned)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "score" not in obj or "agrees" not in obj:
        return None
    agrees_raw = obj.get("agrees")
    if isinstance(agrees_raw, bool):
        agrees = agrees_raw
    elif isinstance(agrees_raw, str) and agrees_raw.strip().lower() in ("true", "false", "yes", "no"):
        agrees = agrees_raw.strip().lower() in ("true", "yes")
    else:
        return None
    reason = str(obj.get("reason") or "").strip()[:500]
    if not reason:
        return None
    return agrees, _clamp_score(obj.get("score")), _normalize_direction(obj.get("direction")), reason


def _build_review_prompt(cand: FilingRef, current: tuple[int, str, str], text: str) -> str:
    score, direction, reason = current
    header = "\n".join(
        [
            f"Form: {cand.form}",
            f"Filing date: {cand.date}",
            f"Current score: {score}/100",
            f"Current direction: {direction}",
            f"Current reason: {reason}",
            "",
            "Filing text (truncated):",
            text,
        ]
    )
    return f"{_load_review_prompt()}\n\n{header}"


def review_filing(
    symbol: str,
    url: str,
    *,
    session: Any = None,
    llm_client: Any = None,
) -> Review:
    """Second read of one filing. Does not change the stored score.

    Raises ``FilingNotFound`` when ``url`` is not an in-window candidate.
    Raises ``ReviewUnavailable`` when the document or the model call fails.
    """
    symbol = symbol.strip().upper()
    owned: httpx.Client | None = None
    active = session
    if active is None:
        owned = httpx.Client(timeout=30.0)
        active = owned
    try:
        cand = _candidate_for_url(symbol, url, session=active)
        current = _score_one(cand, session=active, llm_client=llm_client)
        doc = edgar.fetch_document_text(cand.doc_url, session=active)
        if not doc.text:
            raise ReviewUnavailable("filing text unavailable")
        prompt = _build_review_prompt(cand, current, doc.text)
        try:
            raw = llm.run(prompt, client=llm_client)
        except Exception as exc:
            logger.warning("SEC filing review failed for %s: %s", cand.accession, exc)
            raise ReviewUnavailable(str(exc)) from exc
        parsed = _parse_review(raw)
        if parsed is None:
            logger.warning("SEC filing review: unparseable LLM output for %s", cand.accession)
            raise ReviewUnavailable("unparseable review")
        agrees, score, direction, reason = parsed
        return Review(
            agrees=agrees,
            score=score,
            direction=direction,
            reason=reason,
            will_show=_will_show(score),
        )
    finally:
        if owned is not None:
            owned.close()


def apply_review(
    symbol: str,
    url: str,
    result: tuple[int, str, str],
    *,
    session: Any = None,
) -> Review:
    """Replace the stored score for the filing at ``url``.

    The row keeps the significance prompt key, so the next snapshot serves
    this score instead of re-reading the filing. Raises ``FilingNotFound``
    when the URL is not an in-window candidate, and ``ReviewUnavailable``
    when the score cannot be stored.
    """
    symbol = symbol.strip().upper()
    score, direction, reason = result
    score = _clamp_score(score)
    direction = _normalize_direction(direction)
    reason = reason.strip()[:500]
    if not reason:
        raise ReviewUnavailable("empty reason")
    owned: httpx.Client | None = None
    active = session
    if active is None:
        owned = httpx.Client(timeout=30.0)
        active = owned
    try:
        cand = _candidate_for_url(symbol, url, session=active)
    finally:
        if owned is not None:
            owned.close()

    key = _prompt_key()
    if key is None:
        raise ReviewUnavailable("score store unavailable")
    stored = (score, direction, reason)
    if not _save_stored(cand.accession, key, stored):
        raise ReviewUnavailable("could not store the score")
    _cache.put(cand.accession, stored)

    from app.services import market_data

    market_data.drop_snapshot(symbol)
    return Review(
        agrees=False,
        score=score,
        direction=direction,
        reason=reason,
        will_show=_will_show(score),
    )
