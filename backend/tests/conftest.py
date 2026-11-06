"""Shared fixtures."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, SessionLocal
from app.services import edgar, sec_significance


@pytest.fixture(autouse=True)
def _clear_sec_caches():
    """Document texts and filing lists are cached across calls."""
    edgar.clear_cache()
    yield
    edgar.clear_cache()


@pytest.fixture(autouse=True)
def significance_store():
    """Keep persisted SEC scores and briefs in a throwaway in-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sec_significance.set_session_factory(factory)
    yield factory
    sec_significance.set_session_factory(SessionLocal)
    engine.dispose()
