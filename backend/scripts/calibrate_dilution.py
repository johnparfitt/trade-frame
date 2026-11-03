"""Dilution Risk calibration: a one-off dev script, not a product feature.

Two modes, over a hand-picked ticker list (no screening, no scheduler):

- ``sanity``: today's live watchlist score per ticker (the real
  ``market_data.get_snapshot`` path). Known serial diluters should land above
  60 and buyback-heavy large caps below 15.
- ``backtest``: score each ticker as of past dates T using only data public on
  or before T — filings filed by T, company facts by their ``filed`` date, UW
  statements whose period ended at least 45 days (quarterly) or 90 days
  (annual) before T (UW rows carry no filing date), and splits and price up to
  T — then compare with realized share growth over the next 6 and 12 months
  (Spearman rank correlation).

Known backtest limits: UW counts are on today's split basis, so warrant
ratios for a company that split after T are slightly off; EDGAR's
``filings.recent`` may not reach T minus three years for frequent filers.

Run from ``backend/``::

    ./.venv/bin/python -m scripts.calibrate_dilution sanity
    ./.venv/bin/python -m scripts.calibrate_dilution backtest
    ./.venv/bin/python -m scripts.calibrate_dilution tune   # compare VARIANTS
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
import yfinance as yf

from app.config import get_settings
from app.services import company_facts, dilution, edgar, market_data
from app.services.edgar import FilingInfo
from app.services.uw import UnusualWhalesClient

DILUTERS = [
    "ONDS", "WULF", "RDW", "SMR", "IONQ", "ASST", "PLUG", "QUBT", "KULR", "FCEL",
    "SOUN", "RGTI", "NVAX", "OKLO", "IREN", "CIFR", "MARA", "RIOT", "LCID", "BBAI",
    "ACHR", "JOBY", "ASTS", "LUNR", "OPEN",
]
BUYBACKS = [
    "AAPL", "MSFT", "GOOGL", "META", "NVDA", "AVGO", "CRM", "QCOM", "DELL", "AMZN",
    "ORCL", "JPM", "V", "MA", "HD",
]
OTHERS = ["AMD", "INTC", "TSLA", "CRWV", "NBIS", "SMCI", "RIVN", "AFRM", "RKLB", "COIN"]

AS_OF = (date(2025, 4, 30), date(2025, 10, 31))
HORIZONS = (182, 365)
_QUARTERLY_LAG_DAYS = 45
_ANNUAL_LAG_DAYS = 90
_GROUP_RE = re.compile(r"^(Behavior|Overhang|Filings|Distress) (\d+):")
GROUPS = ("Behavior", "Overhang", "Filings", "Distress")


# --- helpers -----------------------------------------------------------------------


def _groups(note: str) -> dict[str, int]:
    out = dict.fromkeys(GROUPS, 0)
    for line in note.splitlines():
        m = _GROUP_RE.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else None


# --- sanity ----------------------------------------------------------------------------


def sanity() -> None:
    def show(label: str, symbols: list[str]) -> list[float]:
        print(f"\n{label}")
        scores = []
        for sym in symbols:
            snap = market_data.get_snapshot(sym)
            risk = snap.dilution_risk
            print(f"  {sym:6} {'-' if risk is None else f'{risk:.0f}':>4}  " + (snap.dilution_note or "").replace("\n", " | "))
            if risk is not None:
                scores.append(risk)
        return scores

    d = show("Serial diluters (target > 60)", DILUTERS)
    b = show("Buyback large caps (target < 15)", BUYBACKS)
    print(f"\nDiluters above 60: {sum(s > 60 for s in d)}/{len(d)}; large caps below 15: {sum(s < 15 for s in b)}/{len(b)}")


# --- backtest ----------------------------------------------------------------------------


@dataclass
class Raw:
    symbol: str
    cik: int | None
    filings: list[dict[str, Any]]
    uw: dict[str, Any]
    facts: dict[str, Any] | None
    history: Any  # yfinance daily bars (split-adjusted to today's basis)


def _load(symbol: str, session: httpx.Client, uw: UnusualWhalesClient) -> Raw | None:
    cik = edgar._get_cik(symbol, session)
    if cik is None:
        return None
    submissions = edgar.http_get_json(edgar.SUBMISSIONS_URL.format(cik10=edgar.cik10(cik)), session=session)
    try:
        facts = edgar.http_get_json(company_facts.COMPANY_FACTS_URL.format(cik10=edgar.cik10(cik)), session=session)
    except httpx.HTTPStatusError:
        facts = None
    return Raw(
        symbol=symbol,
        cik=cik,
        filings=edgar._parse_recent_filings(submissions),
        uw=uw.financials(symbol),
        facts=facts,
        history=yf.Ticker(symbol).history(period="5y", interval="1d"),
    )


def _uw_as_of(data: dict[str, Any], as_of: date) -> dict[str, Any]:
    def public(row: dict[str, Any]) -> bool:
        try:
            end = date.fromisoformat(str(row.get("fiscal_date_ending"))[:10])
        except ValueError:
            return False
        lag = _QUARTERLY_LAG_DAYS if row.get("report_type") == "quarterly" else _ANNUAL_LAG_DAYS
        return end + timedelta(days=lag) <= as_of

    return {k: [r for r in data.get(k) or [] if isinstance(r, dict) and public(r)] for k in ("balance_sheets", "cash_flows")}


def _facts_as_of(payload: dict[str, Any] | None, as_of: date) -> company_facts.CompanyFacts:
    if payload is None:
        return company_facts.NO_FACTS
    trimmed = copy.deepcopy(payload)
    for taxonomy in trimmed.get("facts", {}).values():
        for node in taxonomy.values():
            for unit, rows in node.get("units", {}).items():
                node["units"][unit] = [r for r in rows if str(r.get("filed", "9999")) <= as_of.isoformat()]
    return company_facts.parse(trimmed, today=as_of)


def _splits(history: Any) -> list[tuple[date, float]]:
    return market_data._splits_from_history(history) or []


def _price_at(history: Any, as_of: date) -> float | None:
    closes = history["Close"].dropna()
    closes = closes[[ts.date() <= as_of for ts in closes.index]]
    return float(closes.iloc[-1]) if len(closes) else None


def _uw_shares_at(data: dict[str, Any], when: date) -> tuple[date, float] | None:
    rows = dilution._share_history(dilution._statements(data.get("balance_sheets") or [])[0])
    rows = [r for r in rows if r[0] <= when]
    return rows[-1] if rows else None


def realized_growth(raw: Raw, as_of: date, horizon: int, splits: list[tuple[date, float]]) -> float | None:
    base, end = _uw_shares_at(raw.uw, as_of), _uw_shares_at(raw.uw, as_of + timedelta(days=horizon))
    if base is None or end is None or (end[0] - base[0]).days < horizon - 45:
        return None
    growth = (end[1] - base[1]) / base[1] * 100.0
    if abs(growth) > 40 and any(base[0] < d <= end[0] for d, _ in splits):
        return None  # same split guard as the score
    return growth


def score_as_of(raw: Raw, as_of: date, session: httpx.Client) -> tuple[int, str]:
    records = tuple(r for r in edgar._build_records(raw.filings, raw.cik, as_of) if r.filed <= as_of)
    filings = FilingInfo(ok=True, found=True, records=records, cik=raw.cik)
    all_splits = _splits(raw.history)
    splits = [(d, r) for d, r in all_splits if d <= as_of]
    facts = _facts_as_of(raw.facts, as_of)
    fin = dilution.financial_signals(_uw_as_of(raw.uw, as_of))
    # UW and yfinance are on today's split basis, so the cover count is
    # adjusted for every split after its date, through today.
    fin = dilution.verify_share_growth(dilution.apply_cover_count(fin, facts, all_splits), all_splits)
    checks = dilution.run_document_checks(records, session=session, today=as_of)
    price = _price_at(raw.history, as_of)
    mcap = dilution.resolve_market_cap(None, price, fin.latest_shares)
    return dilution.score(
        filings, fin, market_cap=mcap, splits=splits, facts=facts, checks=checks, price=price, today=as_of
    )


def _load_all(symbols: list[str], session: httpx.Client) -> list[Raw]:
    uw = UnusualWhalesClient((get_settings().uw_api_key or "").strip())
    raws = []
    for sym in symbols:
        try:
            raw = _load(sym, session, uw)
        except Exception as exc:  # one bad ticker shouldn't stop the run
            print(f"  {sym}: load failed ({exc})", file=sys.stderr)
            continue
        if raw is not None:
            raws.append(raw)
    return raws


def _fmt(rho: float | None) -> str:
    return "n/a" if rho is None else f"{rho:+.2f}"


def backtest(symbols: list[str]) -> None:
    session = httpx.Client(timeout=60.0)
    raws = _load_all(symbols, session)

    for as_of in AS_OF:
        rows = []
        for raw in raws:
            risk, note = score_as_of(raw, as_of, session)
            splits = _splits(raw.history)
            realized = {h: realized_growth(raw, as_of, h, splits) for h in HORIZONS}
            rows.append((raw.symbol, risk, _groups(note), realized, note))
        rows.sort(key=lambda r: -r[1])
        print(f"\n=== as of {as_of} ===")
        print(f"  {'sym':6} {'score':>5} {'B':>3} {'O':>3} {'F':>3} {'D':>3} " + " ".join(f"{f'+{h}d %':>9}" for h in HORIZONS))
        for sym, risk, groups, realized, _ in rows:
            cells = " ".join(f"{'-' if realized[h] is None else f'{realized[h]:.0f}':>9}" for h in HORIZONS)
            print(f"  {sym:6} {risk:>5} " + " ".join(f"{groups[g]:>3}" for g in GROUPS) + f" {cells}")
        for h in HORIZONS:
            pairs = [(r[1], r[2], r[3][h]) for r in rows if r[3][h] is not None]
            if len(pairs) < 3:
                continue
            ys = [p[2] for p in pairs]
            parts = [f"score {_fmt(spearman([p[0] for p in pairs], ys))}"]
            parts += [f"{g} {_fmt(spearman([p[1][g] for p in pairs], ys))}" for g in GROUPS]
            print(f"  Spearman vs realized +{h}d (n={len(pairs)}): " + ", ".join(parts))


# Candidate constant sets for ``tune`` (module attribute -> value).
VARIANTS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "mid-cap x0.8": {"_SIZE_MULTIPLIERS": ((300e6, 1.25), (2e9, 1.0), (10e9, 0.8))},
    "mid x0.8, large x0.4": {
        "_SIZE_MULTIPLIERS": ((300e6, 1.25), (2e9, 1.0), (10e9, 0.8)),
        "_LARGE_CAP_MULTIPLIER": 0.4,
    },
    "behavior cap 50": {"_BEHAVIOR_CAP": 50},
    "growth 40, cap 50": {"_GROWTH_POINTS": 40.0, "_BEHAVIOR_CAP": 50},
    "filings cap 35": {"_FILINGS_CAP": 35},
    "mid x0.8 + filings cap 35": {
        "_SIZE_MULTIPLIERS": ((300e6, 1.25), (2e9, 1.0), (10e9, 0.8)),
        "_FILINGS_CAP": 35,
    },
    "overhang cap 10": {"_OVERHANG_CAP": 10},
    "distress cap 10": {"_DISTRESS_CAP": 10},
    "overhang 10 + distress 10": {"_OVERHANG_CAP": 10, "_DISTRESS_CAP": 10},
}


def tune(symbols: list[str]) -> None:
    session = httpx.Client(timeout=60.0)
    raws = _load_all(symbols, session)
    today = date.today()
    diluters = set(DILUTERS)
    buybacks = set(BUYBACKS)
    for name, overrides in VARIANTS.items():
        saved = {k: getattr(dilution, k) for k in overrides}
        for k, v in overrides.items():
            setattr(dilution, k, v)
        try:
            parts = []
            for as_of in AS_OF:
                for h in HORIZONS:
                    rows = []
                    for raw in raws:
                        realized = realized_growth(raw, as_of, h, _splits(raw.history))
                        if realized is not None:
                            rows.append((raw.symbol, score_as_of(raw, as_of, session)[0], realized))
                    if len(rows) < 10:
                        continue
                    rho_all = spearman([r[1] for r in rows], [r[2] for r in rows])
                    dil = [r for r in rows if r[0] in diluters]
                    rho_dil = spearman([r[1] for r in dil], [r[2] for r in dil])
                    parts.append(f"{as_of:%y-%m}+{h}d all {_fmt(rho_all)} dil {_fmt(rho_dil)}")
            now = {raw.symbol: score_as_of(raw, today, session)[0] for raw in raws}
            d_hi = sum(now[s] > 60 for s in now if s in diluters)
            b_lo = sum(now[s] < 15 for s in now if s in buybacks)
            print(f"{name:28} | " + " | ".join(parts) + f" | today: diluters>60 {d_hi}, large caps<15 {b_lo}/{len(buybacks)}")
        finally:
            for k, v in saved.items():
                setattr(dilution, k, v)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("sanity", "backtest", "tune"))
    args = parser.parse_args()
    if args.mode == "sanity":
        sanity()
    elif args.mode == "backtest":
        backtest(DILUTERS + BUYBACKS + OTHERS)
    else:
        tune(DILUTERS + BUYBACKS + OTHERS)


if __name__ == "__main__":
    main()
