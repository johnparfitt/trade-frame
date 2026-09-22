import { handle } from "./lib/http";

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

export async function removeSymbol(symbol: string): Promise<void> {
  return handle(
    await fetch(`/api/watchlist/${encodeURIComponent(symbol)}`, {
      method: "DELETE",
    }),
  );
}
