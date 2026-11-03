"""SQLite persistence for SEC lookups that don't change: filing checks and
parsed company facts.

The in-memory caches in ``filing_checks`` and ``company_facts`` sit in front of
this store, so it is read only on a memory miss (mainly the first load after a
restart) and spares those EDGAR fetches. Each row carries a ``version`` (a
hash of the rules that produced it, see ``rules_version``); a row under another
version is a miss. Never raises: a database error is a miss or a skipped save.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.models import SecCachedResult
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_session_factory: Callable[[], Session] = SessionLocal


def set_session_factory(factory: Callable[[], Session]) -> None:
    """Point the store at another database (tests)."""
    global _session_factory
    _session_factory = factory


def rules_version(*parts: Any) -> str:
    """A short, stable hash of the constants a stored result depends on."""
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:16]


def load(kind: str, key: str, version: str, *, max_age: timedelta | None = None) -> dict[str, Any] | None:
    """The stored payload for (``kind``, ``key``) under ``version``, else None."""
    try:
        with _session_factory() as db:
            row = db.get(SecCachedResult, (kind, key))
            if row is None or row.version != version:
                return None
            if max_age is not None:
                stored = row.stored_at
                if stored.tzinfo is None:  # SQLite drops the offset
                    stored = stored.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - stored > max_age:
                    return None
            payload = json.loads(row.payload)
            return payload if isinstance(payload, dict) else None
    except Exception as exc:
        logger.warning("SEC store: lookup failed for %s %s: %s", kind, key, exc)
        return None


def save(kind: str, key: str, version: str, payload: dict[str, Any]) -> None:
    """Store ``payload`` (JSON-serializable) for (``kind``, ``key``), replacing any older row."""
    try:
        with _session_factory() as db:
            db.merge(
                SecCachedResult(
                    kind=kind,
                    key=key,
                    version=version,
                    payload=json.dumps(payload, separators=(",", ":")),
                    stored_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
    except Exception as exc:
        logger.warning("SEC store: saving failed for %s %s: %s", kind, key, exc)
