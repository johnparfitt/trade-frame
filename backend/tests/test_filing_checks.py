"""Filing-text keyword checks — canned text and a fake session, no network."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.services import edgar, filing_checks
from app.services.edgar import FilingRecord

_URL = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/doc.htm"
_FILLER = "Lorem ipsum dolor sit amet. " * 400  # ~11,200 chars: pushes text past the cover


class _Session:
    def __init__(self, body):
        self.body = body
        self.gets: list[str] = []

    def get(self, url, *, headers=None):  # noqa: ARG002
        self.gets.append(url)
        request = httpx.Request("GET", url)
        if isinstance(self.body, int):
            return httpx.Response(self.body, request=request)
        if isinstance(self.body, Exception):
            raise self.body
        return httpx.Response(200, text=self.body, request=request)


def _rec(form="424B5", accession="0000000001-26-000001", doc_url=_URL) -> FilingRecord:
    return FilingRecord(form=form, filed=date(2026, 10, 1), accession=accession, doc_url=doc_url)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(edgar, "_throttle", lambda: None)


# --- cover pages: ATM and debt takedowns ----------------------------------------


def test_atm_keyword_on_cover_counts():
    cover = filing_checks.classify_cover(
        "PROSPECTUS SUPPLEMENT Up to $50,000,000 Shares of Common Stock. We have entered "
        "into an equity distribution agreement for sales in at-the-market offerings."
    )
    assert cover.atm is True
    assert cover.debt is False


def test_atm_keywords_outside_cover_page_do_not_count():
    text = "PROSPECTUS SUPPLEMENT 5,000,000 shares of common stock. " + _FILLER + " sales agreement"
    assert filing_checks.classify_cover(text).atm is False


def test_notes_only_cover_is_debt_takedown():
    cover = filing_checks.classify_cover(
        "$2,000,000,000 aggregate principal amount of 4.500% Senior Notes due 2036. "
        "Interest is payable semi-annually."
    )
    assert cover.debt is True
    assert cover.atm is False


@pytest.mark.parametrize(
    "text",
    [
        "$500,000,000 aggregate principal amount of 0.75% Convertible Senior Notes due 2031",
        "$300,000,000 aggregate principal amount of Exchangeable Senior Notes due 2030",
        "$400,000,000 Senior Notes due 2030 and 10,000,000 shares of common stock (concurrent offerings)",
    ],
    ids=["convertible", "exchangeable", "mixed-notes-and-stock"],
)
def test_dilutive_or_mixed_notes_covers_are_not_debt_takedowns(text):
    assert filing_checks.classify_cover(text).debt is False


def test_plain_equity_cover_is_neither():
    cover = filing_checks.classify_cover("12,500,000 Shares of Common Stock offered at $4.00 per share")
    assert cover == filing_checks.OfferingCover(atm=False, debt=False)


# --- proxies: authorized-share increases ------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "to increase the number of authorized shares of common stock",
        "approve an increase in the number of authorized shares of Class A Common Stock",
        "a proposal to increase our authorized shares of capital stock",
    ],
)
def test_authorized_increase_phrases(phrase):
    assert filing_checks.proposes_authorized_increase(f"Proposal 2: {phrase}.") is True


@pytest.mark.parametrize(
    "text",
    [
        # Corporate-law comparison boilerplate (reincorporation proxies).
        "such amendment may increase the number of authorized shares of such class of stock up to an "
        "amount proportionate to the subdivision",
        # An equity-plan share increase is stock comp, not a charter change.
        "approve the 2023 Plan to increase the number of authorized shares that can be issued to employees",
        "to increase the number of authorized shares of common stock reserved under the 2021 Stock Incentive Plan",
    ],
    ids=["boilerplate", "plan", "plan-common-stock"],
)
def test_authorized_increase_ignores_boilerplate_and_plans(text):
    assert filing_checks.proposes_authorized_increase(text) is False


def test_authorized_from_to_pattern_counts_only_when_increasing():
    up = "amend the charter to change the authorized shares of common stock from 800,000,000 to 1,200,000,000"
    down = "amend the charter to change the authorized shares of common stock from 500,000,000 to 100,000,000"
    assert filing_checks.proposes_authorized_increase(up) is True
    assert filing_checks.proposes_authorized_increase(down) is False


# --- reverse splits ----------------------------------------------------------------


@pytest.mark.parametrize(
    "ratio",
    ["1-for-20", "1 for 20", "1:20", "one-for-twenty", "one (1) for twenty (20)", "1 -for-5", "1\u2013for\u201320"],
)
def test_reverse_split_ratio_forms(ratio):
    assert filing_checks.mentions_reverse_split(f"effect a reverse stock split at a ratio of {ratio}.") is True


def test_reverse_split_without_ratio_does_not_count():
    assert filing_checks.mentions_reverse_split("We may effect a reverse split in the future.") is False


def test_reverse_split_ratio_far_from_phrase_does_not_count():
    text = "We may need a reverse stock split. " + "x" * 320 + " a 1-for-20 exchange of units"
    assert filing_checks.mentions_reverse_split(text) is False


def test_proposed_reverse_split_needs_proposal_wording():
    proposal = (
        "Proposal 3: to approve an amendment to effect a reverse stock split at a ratio "
        "in the range of 1-for-5 to 1-for-30, at the discretion of the board."
    )
    assert filing_checks.mentions_reverse_split(proposal, proposed=True) is True
    plain = "The board may effect a reverse stock split at a ratio of 1-for-10."
    assert filing_checks.mentions_reverse_split(plain, proposed=True) is False


@pytest.mark.parametrize(
    "past",
    [
        "On August 29, 2025, the company effected a reverse stock split at a ratio of one-for-ten, "
        "as approved by the board and stockholders.",
        "Share amounts reflect the 1-for-8 reverse stock split. The proposal to approve the plan follows.",
    ],
)
def test_proxy_recounting_a_past_split_is_not_a_proposal(past):
    assert filing_checks.mentions_reverse_split(past) is True
    assert filing_checks.mentions_reverse_split(past, proposed=True) is False


# --- fetching and caching -------------------------------------------------------------


def test_check_offering_reads_cover_and_caches_per_accession():
    session = _Session("<p>$1,000,000,000 aggregate principal amount of 5% Notes due 2035</p>")

    first = filing_checks.check_offering(_rec(), session=session)
    second = filing_checks.check_offering(_rec(), session=session)

    assert first == filing_checks.OfferingCover(atm=False, debt=True)
    assert second == first
    assert session.gets == [_URL]


@pytest.mark.parametrize(
    "body", [403, 429, 503, httpx.ReadTimeout("slow")], ids=["403", "429", "503", "timeout"]
)
def test_transient_failure_is_unknown_and_not_cached(body):
    session = _Session(body)

    assert filing_checks.check_offering(_rec(), session=session) is None
    assert filing_checks.check_offering(_rec(), session=session) is None
    assert len(session.gets) == 2  # retried, not cached


@pytest.mark.parametrize("status", [404, 410, 400])
def test_permanent_failure_is_not_found_and_cached(status):
    session = _Session(status)

    assert filing_checks.check_offering(_rec(), session=session) == filing_checks.OfferingCover()
    assert filing_checks.check_reverse_split_8k(_rec("8-K", "acc-8k"), session=session) is False
    filing_checks.check_offering(_rec(), session=session)
    assert len(session.gets) == 2  # the repeat offering check was cached


def test_missing_document_url_is_not_found_without_fetching():
    session = _Session("unused")

    assert filing_checks.check_proxy(_rec("DEF 14A", doc_url=""), session=session) == filing_checks.ProxyProposals()
    assert session.gets == []


def test_check_proxy_reads_both_proposals_from_one_fetch():
    session = _Session(
        "<p>Proposal 1: to approve an increase in the number of authorized shares of common stock.</p>"
        "<p>Proposal 2: to approve a reverse stock split at a ratio of 1-for-10 to 1-for-50.</p>"
    )

    proposals = filing_checks.check_proxy(_rec("DEF 14A"), session=session)

    assert proposals == filing_checks.ProxyProposals(authorized_increase=True, reverse_split=True)
    assert len(session.gets) == 1


def test_check_reverse_split_8k():
    session = _Session("Item 5.03. The Company filed an amendment to effect a 1-for-25 reverse stock split.")
    assert filing_checks.check_reverse_split_8k(_rec("8-K"), session=session) is True


# --- SQLite persistence ---------------------------------------------------------------


def test_results_persist_across_a_restart():
    cover = _Session("<p>We entered into an equity distribution agreement for shares of common stock.</p>")
    proxy = _Session("<p>Proposal 2: to approve a reverse stock split at a ratio of 1-for-10.</p>")
    eight_k = _Session("Item 5.03. A 1-for-25 reverse stock split.")
    filing_checks.check_offering(_rec(), session=cover)
    filing_checks.check_proxy(_rec("DEF 14A", "acc-proxy"), session=proxy)
    filing_checks.check_reverse_split_8k(_rec("8-K", "acc-8k"), session=eight_k)

    filing_checks.clear_cache()  # a restart drops memory, not SQLite
    unused = _Session(503)
    assert filing_checks.check_offering(_rec(), session=unused) == filing_checks.OfferingCover(atm=True)
    assert filing_checks.check_proxy(_rec("DEF 14A", "acc-proxy"), session=unused) == filing_checks.ProxyProposals(
        reverse_split=True
    )
    assert filing_checks.check_reverse_split_8k(_rec("8-K", "acc-8k"), session=unused) is True
    assert unused.gets == []


def test_changed_rules_recheck_persisted_results(monkeypatch):
    filing_checks.check_offering(_rec(), session=_Session("<p>shares of common stock</p>"))
    filing_checks.clear_cache()
    edgar.clear_cache()
    monkeypatch.setattr(filing_checks, "_RULES_VERSION", "edited-rules")

    session = _Session("<p>shares of common stock</p>")
    filing_checks.check_offering(_rec(), session=session)
    assert len(session.gets) == 1


def test_unknown_results_are_not_persisted():
    filing_checks.check_offering(_rec(), session=_Session(503))
    filing_checks.clear_cache()

    session = _Session("<p>shares of common stock</p>")
    filing_checks.check_offering(_rec(), session=session)
    assert len(session.gets) == 1
