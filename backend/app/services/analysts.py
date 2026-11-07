"""Net analyst upgrades for the watchlist Analysts column.

One Unusual Whales call per symbol (``/api/screener/analysts``) covers the last
60 days. Only ``upgraded`` and ``downgraded`` from the fixed name list count;
each event counts, so two upgrades from the same person both count. Net is
upgrades minus downgrades.

Results are cached for ``TF_ANALYST_CACHE_TTL`` (default one hour) so the short
snapshot cache does not repeat the call. Lookups never raise: a missing key or
a UW outage leaves the score empty (``None``), which the column shows as a dash
rather than an even split.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.config import get_settings
from app.services.uw import UnusualWhalesClient, UnusualWhalesClientProtocol

logger = logging.getLogger(__name__)

WINDOW_DAYS = 60
PAGE_LIMIT = 500

# The names the column listens to. Matching is case-insensitive after trim
# and apostrophe normalization, so the spelling here is the canonical form.
ANALYST_NAMES: tuple[str, ...] = (
    "Jeff Zekauskas",
    "Josh Spector",
    "Vincent Andrews",
    "Eric Luebchow",
    "Jonathan Atkin",
    "Michael Rollins",
    "Ed Hyman",
    "Krishna Guha",
    "Chris Schott",
    "Elizabeth Anderson",
    "Larry Biegelsen",
    "Umer Raffat",
    "Joe O'Dea",
    "Adam Jonas",
    "Andrew Obin",
    "David Raso",
    "Deane Dray",
    "Joshua Pokrzywinski",
    "Julian Mitchell",
    "Nigel Coe",
    "Scott Davis",
    "Stephen Volkmann",
    "Tami Zakaria",
    "Tom Narayan",
    "Brian Nowak",
    "Doug Anmuth",
    "Justin Post",
    "Mark Mahaney",
    "Arun Jayaram",
    "Betty Jiang",
    "Bob Brackett",
    "Devin McDermott",
    "Doug Leggate",
    "Jeremy Tonet",
    "John Royall",
    "Neil Mehta",
    "Roger Read",
    "Stephen Richardson",
    "Aaron Rakers",
    "Blayne Curtis",
    "C.J. Muse",
    "Christopher Danely",
    "Christopher Rolland",
    "Harlan Sur",
    "Jonah Cheng",
    "Joseph Moore",
    "Mark Lipacis",
    "Ross Seymore",
    "Stacy Rasgon",
    "Timothy Arcuri",
    "Toshiya Hari",
    "Vijay Rakesh",
    "Vivek Arya",
    "Gil Luria",
    "Ben Reitzes",
    "Dan Ives",
    "Amit Daryanani",
    "Erik Woodring",
    "Keith Bachman",
    "Meta Marshall",
    "Samik Chatterjee",
    "Simon Leopold",
    "Tim Long",
    "Toni Sacconaghi",
    "Wamsi Mohan",
    "Adam Turnquist",
    "Katie Stockton",
    "Rich Ross",
    "Haim Israel",
    "Brian Ossenbeck",
    "Ken Hoexter",
    "Scott Group",
    "Tom Wadewitz",
    "Durgesh Chopra",
    "Julien Dumoulin-Smith",
    "Steven Fleishman",
    "Stephen Byrd",
    "Edward Stanley",
    "Simon Powell",
    "William Bird",
    "Peter Oppenheimer",
    "Savita Subramanian",
    "Jared Woodard",
    "Michael Kantrowitz",
    "Aniket Shah",
)

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "`": "'", "\u00b4": "'"})
_UPGRADE = "upgraded"
_DOWNGRADE = "downgraded"


def _norm_name(name: str) -> str:
    return " ".join(name.translate(_APOSTROPHES).split()).casefold()


_ALLOW = frozenset(_norm_name(name) for name in ANALYST_NAMES)


@dataclass(frozen=True)
class AnalystScore:
    """Net upgrades for one symbol. ``net`` is None when the lookup failed."""

    net: int | None = None
    upgrades: tuple[str, ...] = ()


EMPTY_SCORE = AnalystScore()

# symbol -> (expires_at, score)
_cache: dict[str, tuple[float, AnalystScore]] = {}
_cache_lock = threading.Lock()


def _cache_get(symbol: str) -> AnalystScore | None:
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        expires_at, score = entry
        if time.monotonic() >= expires_at:
            _cache.pop(symbol, None)
            return None
        return score


def _cache_put(symbol: str, score: AnalystScore) -> None:
    with _cache_lock:
        _cache[symbol] = (time.monotonic() + get_settings().analyst_cache_ttl, score)


def clear_cache() -> None:
    """Test helper: drop every cached score."""
    with _cache_lock:
        _cache.clear()


def has_uw_key() -> bool:
    key = get_settings().uw_api_key
    return bool(key and key.strip())


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def score_ratings(symbol: str, rows: list, *, today: date) -> AnalystScore:
    """Score one ticker's rating rows. ``rows`` is the UW ``data`` list."""
    symbol = symbol.strip().upper()
    cutoff = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) - timedelta(
        days=WINDOW_DAYS
    )
    upgrades = 0
    downgrades = 0
    upgrade_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "").strip().casefold()
        if action not in (_UPGRADE, _DOWNGRADE):
            continue
        name = row.get("analyst_name")
        if not isinstance(name, str) or _norm_name(name) not in _ALLOW:
            continue
        raw_ts = row.get("timestamp")
        parsed = _parse_ts(raw_ts)
        if parsed is None or parsed < cutoff:
            continue
        if action == _UPGRADE:
            upgrades += 1
            upgrade_ids.append(f"{symbol}|{_norm_name(name)}|{str(raw_ts).strip()}")
        else:
            downgrades += 1
    return AnalystScore(net=upgrades - downgrades, upgrades=tuple(upgrade_ids))


def get_score(
    symbol: str,
    *,
    client: UnusualWhalesClientProtocol | None = None,
    today: date | None = None,
) -> AnalystScore:
    """Return the 60-day net for ``symbol``. Empty when the lookup fails."""
    symbol = symbol.strip().upper()
    cached = _cache_get(symbol)
    if cached is not None:
        return cached

    if client is None and not has_uw_key():
        return EMPTY_SCORE

    as_of = today or date.today()
    newer_than = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
    owned: UnusualWhalesClient | None = None
    try:
        active = client
        if active is None:
            owned = UnusualWhalesClient((get_settings().uw_api_key or "").strip())
            active = owned
        rows = active.analyst_ratings(symbol, newer_than=newer_than, limit=PAGE_LIMIT)
        if len(rows) >= PAGE_LIMIT:
            logger.warning(
                "Analyst ratings for %s hit the %s-row cap; the 60-day net may be short",
                symbol,
                PAGE_LIMIT,
            )
        score = score_ratings(symbol, rows, today=as_of)
    except Exception as exc:
        logger.warning("Analyst rating lookup failed for %s: %s", symbol, exc)
        return EMPTY_SCORE
    finally:
        if owned is not None:
            owned.close()

    _cache_put(symbol, score)
    return score
