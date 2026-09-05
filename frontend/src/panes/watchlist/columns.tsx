import type { ReactNode } from "react";
import type { WatchlistRow } from "../../api";
import type { SortKey } from "./sort";

export interface Column {
  key: SortKey;
  label: string;
  numeric?: boolean;
  truncate?: boolean;
  width: number; // relative weight for proportional column sizing
  render: (row: WatchlistRow) => ReactNode;
  cellClassName?: (row: WatchlistRow) => string;
  title?: (row: WatchlistRow) => string | undefined;
}

export const REMOVE_COL_WIDTH = 44;

function pctClass(v: number | null): string {
  if (v == null) return "text-slate-500";
  if (v > 0) return "text-emerald-400";
  if (v < 0) return "text-rose-400";
  return "text-slate-300";
}

function fmtPct(v: number | null): string {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function fmtRvol(v: number | null): string {
  if (v == null) return "—";
  return `${v.toFixed(2)}×`;
}

function fmtRange(v: number | null): string {
  if (v == null) return "—";
  return `${v.toFixed(0)}%`;
}

function fmtVolume(v: number | null): string {
  if (v == null) return "—";
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return String(v);
}

function fmtLast(v: number | null): string {
  if (v == null) return "—";
  if (v >= 1000) {
    return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (v >= 1) return v.toFixed(2);
  return v.toFixed(4);
}

function fmtAtrPct50(v: number | null): string {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}`;
}

function fmtFloatShares(v: number | null): string {
  if (v == null) return "—";
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(v);
}

function fmtPctOne(v: number | null): string {
  if (v == null) return "—";
  return `${v.toFixed(1)}%`;
}

function fmtDaysToCover(v: number | null): string {
  if (v == null) return "—";
  return v.toFixed(1);
}

function fmtEarnings(v: string | null): { label: string; soon: boolean } {
  if (!v) return { label: "—", soon: false };
  const parts = v.split("-");
  if (parts.length !== 3) return { label: "—", soon: false };
  const [y, m, d] = parts.map((p) => parseInt(p, 10));
  // Days until the earnings date (local midnight comparison).
  const target = new Date(y, m - 1, d).getTime();
  const today = new Date();
  const todayMidnight = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const days = Math.round((target - todayMidnight) / 86_400_000);
  if (days < 0) return { label: "—", soon: false };
  return { label: `${days}d`, soon: days <= 7 };
}

export const ALL_COLUMNS: Column[] = [
  {
    key: "symbol",
    label: "Symbol",
    width: 76,
    render: (r) => r.symbol,
    cellClassName: () => "font-semibold text-slate-100",
  },
  {
    key: "company",
    label: "Company",
    truncate: true,
    width: 220,
    render: (r) =>
      r.error ? <span className="text-amber-400">{r.error}</span> : (r.company ?? "—"),
    cellClassName: () => "text-slate-300",
    title: (r) => r.company ?? undefined,
  },
  {
    key: "industry",
    label: "Industry",
    truncate: true,
    width: 190,
    render: (r) => r.industry ?? "—",
    cellClassName: () => "text-slate-400",
    title: (r) => r.industry ?? undefined,
  },
  {
    key: "last",
    label: "Last",
    numeric: true,
    width: 88,
    render: (r) => fmtLast(r.last),
    cellClassName: () => "text-slate-100",
  },
  {
    key: "pct_change",
    label: "%Chg",
    numeric: true,
    width: 96,
    render: (r) => fmtPct(r.pct_change),
    cellClassName: (r) => pctClass(r.pct_change),
  },
  {
    key: "avg_volume",
    label: "AvgVol",
    numeric: true,
    width: 96,
    render: (r) => fmtVolume(r.avg_volume),
    cellClassName: () => "text-slate-300",
  },
  {
    key: "rvol",
    label: "RVOL",
    numeric: true,
    width: 84,
    render: (r) => fmtRvol(r.rvol),
    cellClassName: () => "text-slate-300",
  },
  {
    key: "adr_pct",
    label: "ADR%",
    numeric: true,
    width: 84,
    render: (r) => fmtPctOne(r.adr_pct),
    cellClassName: () => "text-slate-300",
  },
  {
    key: "atr_pct_50",
    label: "%ATR 50",
    numeric: true,
    width: 84,
    render: (r) => fmtAtrPct50(r.atr_pct_50),
    cellClassName: (r) => pctClass(r.atr_pct_50),
  },
  {
    key: "range_pct_52w",
    label: "%52w",
    numeric: true,
    width: 76,
    render: (r) => fmtRange(r.range_pct_52w),
    cellClassName: () => "text-slate-300",
  },
  {
    key: "float_shares",
    label: "Float",
    numeric: true,
    width: 84,
    render: (r) => fmtFloatShares(r.float_shares),
    cellClassName: () => "text-slate-300",
  },
  {
    key: "float_pct",
    label: "Float%",
    numeric: true,
    width: 84,
    render: (r) => fmtPctOne(r.float_pct),
    cellClassName: () => "text-slate-300",
  },
  {
    key: "short_pct_float",
    label: "Float Short",
    numeric: true,
    width: 92,
    render: (r) => fmtPctOne(r.short_pct_float),
    cellClassName: () => "text-slate-300",
  },
  {
    key: "days_to_cover",
    label: "DTC",
    numeric: true,
    width: 72,
    render: (r) => fmtDaysToCover(r.days_to_cover),
    cellClassName: () => "text-slate-300",
    title: () => "Days to Cover",
  },
  {
    key: "next_earnings",
    label: "Earnings",
    numeric: true,
    width: 92,
    render: (r) => fmtEarnings(r.next_earnings).label,
    cellClassName: (r) => (fmtEarnings(r.next_earnings).soon ? "text-amber-400" : "text-slate-300"),
  },
];

export const ALL_KEYS = ALL_COLUMNS.map((c) => c.key);
