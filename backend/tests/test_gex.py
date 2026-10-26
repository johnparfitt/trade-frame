"""Watchlist gamma level lookups — fake UW client, no live network."""

from __future__ import annotations

import httpx
import pytest

from app.services import gex


class _FakeClient:
    """Stands in for UnusualWhalesClient; records calls, replays canned data."""

    def __init__(self, levels=None, error: Exception | None = None):
        self._levels = levels if levels is not None else {}
        self._error = error
        self.levels_calls: list[str] = []

    def gex_levels(self, ticker: str, *, source: str = "oi"):
        self.levels_calls.append(ticker)
        if self._error is not None:
            raise self._error
        return self._levels


@pytest.fixture(autouse=True)
def _clear_cache():
    gex.clear_cache()
    yield
    gex.clear_cache()


def test_parses_gamma_magnet_from_full_payload():
    """call_wall, put_wall, gamma_flip ride along in the payload; we don't read them."""
    client = _FakeClient(
        levels={
            "call_wall": "210",
            "put_wall": "190",
            "gamma_flip": "197.5",
            "gamma_magnet": "200",
            "nearby_flips": ["197.5", "201"],
        }
    )

    levels = gex.get_levels("MRNA", client=client)

    assert levels.gamma_magnet == 200.0


def test_missing_gamma_magnet_is_none():
    client = _FakeClient(levels={"call_wall": "210", "gamma_magnet": None})

    levels = gex.get_levels("MRNA", client=client)

    assert levels.gamma_magnet is None


def test_unparseable_gamma_magnet_is_none():
    client = _FakeClient(levels={"gamma_magnet": "n/a", "put_wall": "190"})

    levels = gex.get_levels("MRNA", client=client)

    assert levels.gamma_magnet is None


def test_empty_payload_yields_empty_levels():
    client = _FakeClient(levels={})

    assert gex.get_levels("MRNA", client=client) == gex.EMPTY_LEVELS


def test_returns_empty_without_api_key(monkeypatch):
    monkeypatch.setattr(gex, "has_uw_key", lambda: False)

    assert gex.get_levels("MRNA") == gex.EMPTY_LEVELS


def test_returns_empty_on_http_error():
    client = _FakeClient(error=httpx.HTTPError("boom"))

    assert gex.get_levels("MRNA", client=client) == gex.EMPTY_LEVELS


def test_second_lookup_is_served_from_cache():
    client = _FakeClient(levels={"gamma_magnet": "200"})

    assert gex.get_levels("MRNA", client=client).gamma_magnet == 200.0
    assert gex.get_levels("mrna", client=client).gamma_magnet == 200.0
    assert client.levels_calls == ["MRNA"]


def test_empty_results_are_cached_too():
    client = _FakeClient(levels={})

    assert gex.get_levels("MRNA", client=client) == gex.EMPTY_LEVELS
    assert gex.get_levels("MRNA", client=client) == gex.EMPTY_LEVELS
    assert client.levels_calls == ["MRNA"]


def test_expired_entry_refetches(monkeypatch):
    client = _FakeClient(levels={"gamma_magnet": "200"})
    monkeypatch.setattr(gex, "_cache_get", lambda symbol: None)

    gex.get_levels("MRNA", client=client)
    gex.get_levels("MRNA", client=client)

    assert client.levels_calls == ["MRNA", "MRNA"]
