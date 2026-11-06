import { handle } from "./lib/http";

export interface SecFiling {
  score: number;
  direction: string;
  reason: string;
  form: string;
  date: string;
  url: string;
}

export interface SecBrief {
  text: string;
  generated_at: string;
}

export interface SecReview {
  agrees: boolean;
  score: number;
  direction: string;
  reason: string;
  will_show: boolean;
}

export interface SecScoreApplied {
  score: number;
  direction: string;
  reason: string;
  will_show: boolean;
}

export interface WatchlistRow {
  symbol: string;
  company: string | null;
  industry: string | null;
  last: number | null;
  pct_change: number | null;
  rvol: number | null;
  range_pct_52w: number | null;
  next_earnings: string | null;
  avg_volume: number | null;
  atr_pct_50: number | null;
  adr_pct: number | null;
  float_shares: number | null;
  float_pct: number | null;
  short_pct_float: number | null;
  days_to_cover: number | null;
  gamma_magnet: number | null;
  sec_filing_url: string | null;
  sec_filing_form: string | null;
  sec_filing_date: string | null;
  sec_significance: number | null;
  sec_significance_note: string | null;
  sec_significance_direction: string | null;
  sec_filings: SecFiling[];
  added_at: string;
  error: string | null;
}

export async function getWatchlist(): Promise<WatchlistRow[]> {
  return handle(await fetch("/api/watchlist"));
}

export async function addSymbol(symbol: string): Promise<WatchlistRow> {
  return handle(
    await fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol }),
    }),
  );
}

export async function secBrief(symbol: string): Promise<SecBrief> {
  return handle(
    await fetch(`/api/watchlist/${encodeURIComponent(symbol)}/sec-brief`, {
      method: "POST",
    }),
  );
}

export async function secReview(symbol: string, url: string): Promise<SecReview> {
  return handle(
    await fetch(`/api/watchlist/${encodeURIComponent(symbol)}/sec-review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }),
  );
}

export async function applySecReview(
  symbol: string,
  review: { url: string; score: number; direction: string; reason: string },
): Promise<SecScoreApplied> {
  return handle(
    await fetch(`/api/watchlist/${encodeURIComponent(symbol)}/sec-review/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(review),
    }),
  );
}

export async function removeSymbol(symbol: string): Promise<void> {
  return handle(
    await fetch(`/api/watchlist/${encodeURIComponent(symbol)}`, {
      method: "DELETE",
    }),
  );
}
