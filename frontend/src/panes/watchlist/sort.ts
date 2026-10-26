import type { WatchlistRow } from "../../api";

export type SortKey =
  | "symbol"
  | "company"
  | "industry"
  | "last"
  | "pct_change"
  | "avg_volume"
  | "rvol"
  | "adr_pct"
  | "atr_pct_50"
  | "range_pct_52w"
  | "float_shares"
  | "float_pct"
  | "short_pct_float"
  | "days_to_cover"
  | "gamma_magnet"
  | "dilution_risk"
  | "sec_significance"
  | "next_earnings";

export type SortDir = "asc" | "desc";

export function compare(a: WatchlistRow, b: WatchlistRow, key: SortKey, dir: SortDir): number {
  const av = a[key];
  const bv = b[key];
  // Nulls sort last regardless of direction.
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  let result: number;
  if (typeof av === "number" && typeof bv === "number") {
    result = av - bv;
  } else {
    result = String(av).localeCompare(String(bv));
  }
  return dir === "asc" ? result : -result;
}
