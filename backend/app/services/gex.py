"""Gamma magnet lookups for the watchlist Gamma Magnet column.

The strike comes from one Unusual Whales call: ``/gex-levels?source=oi`` returns
the level UW derives from open interest — the gamma magnet (largest-magnitude
net gamma, the strongest pin).

Unlike the Report pane this is read on every watchlist refresh, so results are
cached for ``TF_GEX_CACHE_TTL`` seconds (much longer than the yfinance snapshot
TTL) to keep UW calls per symbol down to one cache window. Lookups never raise:
a missing key or a UW outage yields an empty ``GexLevels`` so the rest of the
row still renders.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from app.config import get_settings
from app.services.uw import UnusualWhalesClient, UnusualWhalesClientProtocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GexLevels:
    """UW's open-interest gamma magnet for one symbol; may be absent."""

    gamma_magnet: float | None = None


EMPTY_LEVELS = GexLevels()

# symbol -> (expires_at, levels)
_cache: dict[str, tuple[float, GexLevels]] = {}
_cache_lock = threading.Lock()


def _cache_get(symbol: str) -> GexLevels | None:
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry is None:
            return None
        expires_at, levels = entry
        if time.monotonic() >= expires_at:
            _cache.pop(symbol, None)
            return None
        return levels


def _cache_put(symbol: str, levels: GexLevels) -> None:
    with _cache_lock:
        _cache[symbol] = (time.monotonic() + get_settings().gex_cache_ttl, levels)


def clear_cache() -> None:
    """Test helper: drop every cached level set."""
    with _cache_lock:
        _cache.clear()


def has_uw_key() -> bool:
    key = get_settings().uw_api_key
    return bool(key and key.strip())


def _as_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def levels_from_payload(data: dict) -> GexLevels:
    """Pull the gamma magnet out of a ``/gex-levels`` ``data`` object."""
    return GexLevels(gamma_magnet=_as_float(data.get("gamma_magnet")))


def get_levels(
    symbol: str, *, client: UnusualWhalesClientProtocol | None = None
) -> GexLevels:
    """Return the OI gamma magnet for ``symbol``; empty when unavailable."""
    symbol = symbol.strip().upper()
    cached = _cache_get(symbol)
    if cached is not None:
        return cached

    if client is None and not has_uw_key():
        return EMPTY_LEVELS

    owned: UnusualWhalesClient | None = None
    try:
        active = client
        if active is None:
            owned = UnusualWhalesClient((get_settings().uw_api_key or "").strip())
            active = owned
        levels = levels_from_payload(active.gex_levels(symbol))
    except Exception as exc:
        # A UW outage must not blank out the rest of the watchlist row.
        logger.warning("GEX level lookup failed for %s: %s", symbol, exc)
        return EMPTY_LEVELS
    finally:
        if owned is not None:
            owned.close()

    _cache_put(symbol, levels)
    return levels
