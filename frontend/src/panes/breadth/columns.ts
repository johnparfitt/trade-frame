import type { BreadthDay } from "./api";

export type SortDir = "asc" | "desc";
export type SortKey = keyof Pick<
  BreadthDay,
  | "session_date"
  | "up_4"
  | "down_4"
  | "ratio_5d"
  | "ratio_10d"
  | "up_25_q"
  | "down_25_q"
  | "up_50_m"
  | "down_50_m"
  | "ma_stack"
>;

export type Column = {
  key: SortKey;
  label: string;
  width: number;
  format?: (row: BreadthDay) => string;
  cellClassName?: (row: BreadthDay) => string;
};

function dash(n: number | null): string {
  return n == null ? "—" : String(n);
}

function ratio(n: number | null): string {
  return n == null ? "—" : n.toFixed(2);
}

function ratioClass(n: number | null): string {
  if (n == null) return "text-slate-500";
  if (n >= 2) return "text-emerald-400";
  if (n <= 0.5) return "text-rose-400";
  return "text-slate-300";
}

export const ALL_COLUMNS: Column[] = [
  {
    key: "session_date",
    label: "Date",
    width: 12,
    format: (row) => row.session_date,
    cellClassName: () => "text-slate-200",
  },
  { key: "up_4", label: "Up 4%", width: 8, format: (row) => dash(row.up_4) },
  { key: "down_4", label: "Down 4%", width: 8, format: (row) => dash(row.down_4) },
  {
    key: "ratio_5d",
    label: "5d ratio",
    width: 8,
    format: (row) => ratio(row.ratio_5d),
    cellClassName: (row) => ratioClass(row.ratio_5d),
  },
  {
    key: "ratio_10d",
    label: "10d ratio",
    width: 8,
    format: (row) => ratio(row.ratio_10d),
    cellClassName: (row) => ratioClass(row.ratio_10d),
  },
  { key: "up_25_q", label: "Up 25% Q", width: 9, format: (row) => dash(row.up_25_q) },
  { key: "down_25_q", label: "Down 25% Q", width: 10, format: (row) => dash(row.down_25_q) },
  { key: "up_50_m", label: "Up 50% M", width: 9, format: (row) => dash(row.up_50_m) },
  { key: "down_50_m", label: "Down 50% M", width: 10, format: (row) => dash(row.down_50_m) },
  { key: "ma_stack", label: "MA stack", width: 9, format: (row) => dash(row.ma_stack) },
];

function cmp(a: BreadthDay, b: BreadthDay, key: SortKey): number {
  const av = a[key];
  const bv = b[key];
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  if (typeof av === "number" && typeof bv === "number") return av - bv;
  return String(av).localeCompare(String(bv));
}

export function compare(a: BreadthDay, b: BreadthDay, key: SortKey, dir: SortDir): number {
  const n = cmp(a, b, key);
  return dir === "asc" ? n : -n;
}
