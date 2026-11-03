"""Bounded LRU used by the per-accession caches."""

from __future__ import annotations

import pytest

from app.services.lru import BoundedLRU


def test_get_put_and_contains():
    cache: BoundedLRU[str, int] = BoundedLRU(2)
    cache.put("a", 1)
    assert cache.get("a") == 1
    assert "a" in cache
    assert cache.get("missing") is None
    assert "missing" not in cache


def test_evicts_least_recently_used():
    cache: BoundedLRU[str, int] = BoundedLRU(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")  # "b" is now least recent
    cache.put("c", 3)
    assert "b" not in cache
    assert cache.get("a") == 1 and cache.get("c") == 3
    assert len(cache) == 2


def test_put_existing_key_refreshes_without_growing():
    cache: BoundedLRU[str, int] = BoundedLRU(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)  # "b" is now least recent
    cache.put("c", 3)
    assert cache.get("a") == 10
    assert "b" not in cache


def test_clear():
    cache: BoundedLRU[str, int] = BoundedLRU(2)
    cache.put("a", 1)
    cache.clear()
    assert len(cache) == 0


def test_rejects_non_positive_size():
    with pytest.raises(ValueError):
        BoundedLRU(0)
