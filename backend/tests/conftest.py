"""Shared fixtures."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, SessionLocal
from app.services import company_facts, edgar, filing_checks, sec_significance, sec_store


@pytest.fixture(autouse=True)
def _clear_sec_caches():
    """Document texts, filing checks, and company facts are cached across calls."""
    for module in (edgar, filing_checks, company_facts):
        module.clear_cache()
    yield
    for module in (edgar, filing_checks, company_facts):
        module.clear_cache()


@pytest.fixture(autouse=True)
def significance_store():
    """Keep persisted SEC results (significance scores, filing checks, company
    facts) in a throwaway in-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sec_significance.set_session_factory(factory)
    sec_store.set_session_factory(factory)
    yield factory
    sec_significance.set_session_factory(SessionLocal)
    sec_store.set_session_factory(SessionLocal)
    engine.dispose()
