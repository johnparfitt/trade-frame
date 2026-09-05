import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  addSymbol,
  getWatchlist,
  removeSymbol,
  type WatchlistRow,
} from "../api";

type SortKey =
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
  | "next_earnings";
type SortDir = "asc" | "desc";

interface Column {
  key: SortKey;
  label: string;
  numeric?: boolean;
  truncate?: boolean;
  width: number; // relative weight for proportional column sizing
  render: (row: WatchlistRow) => ReactNode;
  cellClassName?: (row: WatchlistRow) => string;
  title?: (row: WatchlistRow) => string | undefined;
}

const COLUMN_STORAGE_KEY = "tf.watchlist.columns";
const REMOVE_COL_WIDTH = 44;

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

const ALL_COLUMNS: Column[] = [
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

const ALL_KEYS = ALL_COLUMNS.map((c) => c.key);

function compare(a: WatchlistRow, b: WatchlistRow, key: SortKey, dir: SortDir): number {
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

function SortIcon({ state }: { state: "none" | "asc" | "desc" }) {
  return (
    <span className="ml-1 inline-flex w-2 flex-col items-center justify-center align-middle leading-none">
      {state === "none" ? (
        <>
          <span className="text-[7px] leading-none text-slate-600">▲</span>
          <span className="text-[7px] leading-none text-slate-600">▼</span>
        </>
      ) : (
        <span className="text-[9px] leading-none text-slate-200">
          {state === "asc" ? "▲" : "▼"}
        </span>
      )}
    </span>
  );
}

function ColumnPicker({
  visible,
  onToggle,
}: {
  visible: Set<SortKey>;
  onToggle: (key: SortKey) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="rounded-md border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
      >
        Columns ▾
      </button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 w-48 rounded-md border border-slate-700 bg-slate-900 p-2 shadow-lg">
          {ALL_COLUMNS.map((col) => {
            const isSymbol = col.key === "symbol";
            return (
              <label
                key={col.key}
                className={`flex items-center gap-2 rounded px-2 py-1 text-sm ${
                  isSymbol ? "text-slate-500" : "cursor-pointer text-slate-200 hover:bg-slate-800"
                }`}
              >
                <input
                  type="checkbox"
                  checked={visible.has(col.key)}
                  disabled={isSymbol}
                  onChange={() => onToggle(col.key)}
                  className="accent-sky-600"
                />
                {col.label}
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function Watchlist() {
  const queryClient = useQueryClient();
  const [symbolInput, setSymbolInput] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("symbol");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [formError, setFormError] = useState<string | null>(null);

  const [visibleKeys, setVisibleKeys] = useState<Set<SortKey>>(() => {
    try {
      const raw = localStorage.getItem(COLUMN_STORAGE_KEY);
      if (raw) {
        const arr = JSON.parse(raw) as string[];
        const set = new Set(arr.filter((k): k is SortKey => ALL_KEYS.includes(k as SortKey)));
        // New columns default to visible even if prefs were saved before they existed.
        for (const key of ALL_KEYS) {
          if (!arr.includes(key)) set.add(key);
        }
        set.add("symbol"); // Symbol is always shown.
        return set;
      }
    } catch {
      /* ignore malformed storage */
    }
    return new Set(ALL_KEYS);
  });

  useEffect(() => {
    try {
      localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify([...visibleKeys]));
    } catch {
      /* ignore */
    }
  }, [visibleKeys]);

  const visibleColumns = useMemo(
    () => ALL_COLUMNS.filter((c) => visibleKeys.has(c.key)),
    [visibleKeys],
  );

  function toggleColumn(key: SortKey) {
    if (key === "symbol") return;
    setVisibleKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      next.add("symbol");
      return next;
    });
  }

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ["watchlist"],
    queryFn: getWatchlist,
    refetchInterval: 16 * 60 * 1000, // 16 minutes
  });

  const addMutation = useMutation({
    mutationFn: (symbol: string) => addSymbol(symbol),
    onSuccess: () => {
      setSymbolInput("");
      setFormError(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
    onError: (err: Error) => setFormError(err.message),
  });

  const removeMutation = useMutation({
    mutationFn: (symbol: string) => removeSymbol(symbol),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  const rows = useMemo(() => {
    const list = [...(data ?? [])];
    list.sort((a, b) => compare(a, b, sortKey, sortDir));
    return list;
  }, [data, sortKey, sortDir]);

  function onSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "symbol" || key === "company" || key === "industry" ? "asc" : "desc");
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const sym = symbolInput.trim().toUpperCase();
    if (!sym) return;
    addMutation.mutate(sym);
  }

  const totalWeight =
    visibleColumns.reduce((sum, c) => sum + c.width, 0) + REMOVE_COL_WIDTH;
  const colPct = (weight: number) => `${(weight / totalWeight) * 100}%`;
  const emptySpan = visibleColumns.length + 1;

  return (
    <div>
      <form onSubmit={onSubmit} className="mb-4 flex items-center gap-2">
        <input
          value={symbolInput}
          onChange={(e) => setSymbolInput(e.target.value)}
          placeholder="Add symbol (e.g. AAPL)"
          className="w-56 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm uppercase placeholder:normal-case placeholder:text-slate-500 focus:border-slate-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={addMutation.isPending || !symbolInput.trim()}
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {addMutation.isPending ? "Adding…" : "Add"}
        </button>
        <button
          type="button"
          onClick={() => refetch()}
          className="rounded-md border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
        >
          {isFetching ? "Refreshing…" : "Refresh"}
        </button>
        <div className="ml-auto">
          <ColumnPicker visible={visibleKeys} onToggle={toggleColumn} />
        </div>
      </form>

      {formError && <p className="mb-3 text-sm text-rose-400">{formError}</p>}

      <div className="overflow-hidden rounded-lg border border-slate-800">
        <table className="w-full table-fixed text-left text-sm">
          <colgroup>
            {visibleColumns.map((col) => (
              <col key={col.key} style={{ width: colPct(col.width) }} />
            ))}
            <col style={{ width: colPct(REMOVE_COL_WIDTH) }} />
          </colgroup>
          <thead className="bg-slate-900 text-slate-400">
            <tr>
              {visibleColumns.map((col) => (
                <th
                  key={col.key}
                  onClick={() => onSort(col.key)}
                    className={`cursor-pointer select-none px-3 py-2 font-medium hover:text-slate-200 ${
                      col.truncate ? "max-w-0" : ""
                    }`}
                >
                  <span
                    className={`flex items-center ${col.numeric ? "justify-end" : "justify-start"}`}
                  >
                    <span className="truncate">{col.label}</span>
                    <SortIcon state={sortKey === col.key ? sortDir : "none"} />
                  </span>
                </th>
              ))}
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {isLoading && (
              <tr>
                <td colSpan={emptySpan} className="px-3 py-8 text-center text-slate-500">
                  Loading…
                </td>
              </tr>
            )}
            {isError && (
              <tr>
                <td colSpan={emptySpan} className="px-3 py-8 text-center text-rose-400">
                  {(error as Error)?.message ?? "Failed to load watchlist."}
                </td>
              </tr>
            )}
            {!isLoading && !isError && rows.length === 0 && (
              <tr>
                <td colSpan={emptySpan} className="px-3 py-8 text-center text-slate-500">
                  No symbols yet. Add one above.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <tr key={row.symbol} className="hover:bg-slate-900/50">
                {visibleColumns.map((col) => (
                  <td
                    key={col.key}
                    title={col.title?.(row)}
                    className={`px-3 py-2 ${col.truncate ? "max-w-0 truncate" : "whitespace-nowrap"} ${
                      col.numeric ? "text-right tabular-nums" : ""
                    } ${col.cellClassName?.(row) ?? "text-slate-300"}`}
                  >
                    {col.render(row)}
                  </td>
                ))}
                <td className="px-3 py-2 text-right">
                  <button
                    onClick={() => removeMutation.mutate(row.symbol)}
                    className="text-slate-500 hover:text-rose-400"
                    aria-label={`Remove ${row.symbol}`}
                    title={`Remove ${row.symbol}`}
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
