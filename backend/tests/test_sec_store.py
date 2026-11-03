from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import SecCachedResult
from app.services import sec_store


def test_round_trip_under_same_version():
    sec_store.save("kind", "key", "v1", {"a": 1, "b": [True, None]})
    assert sec_store.load("kind", "key", "v1") == {"a": 1, "b": [True, None]}


def test_other_version_or_kind_is_a_miss():
    sec_store.save("kind", "key", "v1", {"a": 1})
    assert sec_store.load("kind", "key", "v2") is None
    assert sec_store.load("other", "key", "v1") is None


def test_save_replaces_older_row():
    sec_store.save("kind", "key", "v1", {"a": 1})
    sec_store.save("kind", "key", "v2", {"a": 2})
    assert sec_store.load("kind", "key", "v2") == {"a": 2}


def test_max_age_expires_old_rows(significance_store):
    sec_store.save("kind", "key", "v1", {"a": 1})
    with significance_store() as db:
        db.get(SecCachedResult, ("kind", "key")).stored_at = datetime.now(timezone.utc) - timedelta(days=31)
        db.commit()

    assert sec_store.load("kind", "key", "v1") == {"a": 1}
    assert sec_store.load("kind", "key", "v1", max_age=timedelta(days=30)) is None


def test_rules_version_is_stable_and_sensitive():
    assert sec_store.rules_version(1, ("a", "b")) == sec_store.rules_version(1, ("a", "b"))
    assert sec_store.rules_version(1, ("a", "b")) != sec_store.rules_version(1, ("a", "c"))
    assert len(sec_store.rules_version(1)) == 16


def test_database_errors_never_raise():
    def broken():
        raise RuntimeError("db down")

    sec_store.set_session_factory(broken)  # the autouse fixture restores it
    sec_store.save("kind", "key", "v1", {"a": 1})
    assert sec_store.load("kind", "key", "v1") is None
