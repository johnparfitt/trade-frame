"""LLM-scored significance of a stock's most recent *meaningful* SEC filing.

The watchlist SEC column is deliberately low-noise: it stays blank unless a
genuinely significant filing exists. Given the newest-first significant filing
candidates from ``app.services.edgar``, this module scores each (an LLM reads
the filing text and rates it 0-100) and surfaces the newest one that scores
above ``TF_SEC_SIGNIFICANCE_MIN`` (default 50). Worthless filings are skipped
entirely, so a real filing is never hidden behind a junk one filed moments
later; if nothing clears the bar, the column is blank.

Scoring is cached per accession (filings are immutable, so each is analyzed at
most once) and the scan stops at the first qualifier, so steady state is about
one LLM call per genuinely new filing. Everything is resilient: a missing key,
disabled scoring, an outage, or an unparseable response falls back to a
deterministic form/item heuristic, and nothing here ever raises.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

import httpx

from app.config import BACKEND_DIR, get_settings
from app.services import edgar, llm
from app.services.edgar import FilingRef

logger = logging.getLogger(__name__)

PROMPT_PATH = BACKEND_DIR.parent / "prompts" / "sec" / "significance.md"

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
class SigResult:
    """The surfaced filing: its significance score, note, and link metadata."""

    score: int | None = None
    note: str | None = None
    direction: str | None = None  # "bullish" | "bearish" | "neutral"
    url: str | None = None
    form: str | None = None
    date: str | None = None


EMPTY = SigResult()

# accession -> (score, direction, reason) for already-scored filings. Immutable
# filings, so this never expires within a process.
_cache: dict[str, tuple[int, str, str]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Test helper: drop the per-accession score cache."""
    with _cache_lock:
        _cache.clear()


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
    with _cache_lock:
        hit = _cache.get(cand.accession)
    if hit is not None:
        return hit

    result = _score_one_uncached(cand, session=session, llm_client=llm_client)

    with _cache_lock:
        _cache[cand.accession] = result
    return result


def _score_one_uncached(
    cand: FilingRef, *, session: Any, llm_client: Any
) -> tuple[int, str, str]:
    settings = get_settings()
    if not settings.sec_significance_enabled:
        return _heuristic(cand.form, cand.items)
    try:
        text = edgar.fetch_document_text(cand.doc_url, session=session)
        if not text:
            return _heuristic(cand.form, cand.items)
        prompt = _build_prompt(cand, text)
        raw = llm.run(prompt, client=llm_client)
        parsed = _parse_llm_json(raw)
        if parsed is None:
            logger.warning(
                "SEC significance: unparseable LLM output for %s (%s)",
                cand.accession,
                cand.form,
            )
            return _heuristic(cand.form, cand.items)
        return parsed
    except Exception as exc:  # LLM/key/outage -> deterministic fallback
        logger.warning("SEC significance scoring failed for %s: %s", cand.accession, exc)
        return _heuristic(cand.form, cand.items)


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


def evaluate(
    symbol: str,
    candidates: Sequence[FilingRef],
    *,
    session: Any = None,
    llm_client: Any = None,
) -> SigResult:
    """Surface the newest filing scoring above the threshold, else blank.

    Scans ``candidates`` newest-first and returns the first whose significance
    clears ``sec_significance_min``; worthless filings above it are skipped.
    """
    settings = get_settings()
    threshold = settings.sec_significance_min
    today = date.today()
    recent_days = settings.sec_significance_recent_days

    owned: httpx.Client | None = None
    active = session
    try:
        for cand in candidates:
            # Recency gate: candidates are newest-first, so the first one older
            # than the window means every later one is too -> stop (blank). This
            # also avoids spending an LLM call on a stale filing.
            try:
                age = (today - date.fromisoformat(cand.date)).days
            except (TypeError, ValueError):
                continue  # unparseable date: skip, don't crash
            if age > recent_days:
                break

            with _cache_lock:
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
                return SigResult(
                    score=score,
                    note=_build_note(score, direction, cand, reason),
                    direction=direction,
                    url=cand.url,
                    form=cand.form,
                    date=cand.date,
                )
        return EMPTY
    finally:
        if owned is not None:
            owned.close()
