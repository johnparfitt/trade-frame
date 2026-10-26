import { useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { WatchlistRow } from "../../api";
import type { SortKey } from "./sort";
import { useSeenFilings } from "./useSeenFilings";

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

function fmtDilution(v: number | null): string {
  if (v == null) return "—";
  return v.toFixed(0);
}

function dilutionClass(v: number | null): string {
  if (v == null) return "text-slate-500";
  if (v > 66) return "text-rose-400";
  if (v >= 34) return "text-amber-400";
  return "text-emerald-400";
}

function significanceClass(direction: string | null, score: number | null): string {
  // Color = direction (red bearish / green bullish / amber neutral); brightness
  // ramps with magnitude so a big move is louder than a mild one.
  if (score == null) return "text-slate-500";
  const strong = score > 66;
  switch (direction) {
    case "bearish":
      return strong ? "text-rose-300" : "text-rose-400";
    case "bullish":
      return strong ? "text-emerald-300" : "text-emerald-400";
    default:
      return strong ? "text-amber-300" : "text-amber-400";
  }
}

// Shared score cell: shows a number and, on hover, renders a multi-line note as
// a styled tooltip. A portal is used so the tooltip escapes the table's
// `overflow-hidden` wrapper, and it appears on hover immediately (the native
// `title` tooltip was slow/unreliable). Optionally renders the number as a link
// and shows a leading adornment (e.g. a recency/alert dot).
function HoverScore({
  label,
  colorClass,
  note,
  href,
  adornment,
  onActivate,
}: {
  label: ReactNode;
  colorClass: string;
  note: string | null;
  href?: string;
  adornment?: ReactNode;
  onActivate?: () => void;
}) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const lines = (note ?? "").split("\n").filter(Boolean);

  function show(e: React.MouseEvent<HTMLSpanElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const x = Math.min(r.right, window.innerWidth - 8);
    setPos({ x, y: r.bottom + 4 });
  }

  const inner = (
    <>
      {adornment}
      <span>{label}</span>
    </>
  );

  return (
    <span
      className="inline-flex cursor-default items-center justify-end gap-1"
      onMouseEnter={show}
      onMouseMove={show}
      onMouseLeave={() => setPos(null)}
    >
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 hover:underline"
          onClick={onActivate}
        >
          {inner}
        </a>
      ) : (
        inner
      )}
      {pos &&
        lines.length > 0 &&
        createPortal(
          <div
            className="pointer-events-none fixed z-50 max-w-xs -translate-x-full rounded-md border border-slate-700 bg-slate-900/95 px-3 py-2 text-left text-xs leading-relaxed shadow-xl"
            style={{ left: pos.x, top: pos.y }}
          >
            {lines.map((line, i) => {
              if (i === 0) {
                return (
                  <div key={i} className={`mb-1 font-semibold ${colorClass}`}>
                    {line}
                  </div>
                );
              }
              const info = line.startsWith("-") || line === "No dilution signals";
              return (
                <div key={i} className={info ? "text-slate-500" : "text-slate-300"}>
                  {line}
                </div>
              );
            })}
          </div>,
          document.body,
        )}
    </span>
  );
}

// Dilution Risk cell: the point-breakdown note on hover.
function DilutionCell({ row }: { row: WatchlistRow }) {
  return (
    <HoverScore
      label={fmtDilution(row.dilution_risk)}
      colorClass={dilutionClass(row.dilution_risk)}
      note={row.dilution_note}
    />
  );
}

const RECENT_DAYS = 7;

function daysSinceFiling(iso: string | null): number | null {
  if (!iso) return null;
  const parts = iso.split("-");
  if (parts.length !== 3) return null;
  const [y, m, d] = parts.map((p) => parseInt(p, 10));
  const filed = new Date(y, m - 1, d).getTime();
  const now = new Date();
  const todayMidnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.round((todayMidnight - filed) / 86_400_000);
}

// SEC cell: blank unless a filing cleared the significance bar. Shows the score
// (color-coded), a recency dot for <=7-day-old filings, and a pulsing "look at
// me" dot while a recent filing is unseen. Clicking opens EDGAR and marks it
// seen, calming the pulse.
function SecCell({ row }: { row: WatchlistRow }) {
  const { isSeen, markSeen } = useSeenFilings();
  if (row.sec_significance == null || !row.sec_filing_url) return <>—</>;

  const age = daysSinceFiling(row.sec_filing_date);
  const recent = age != null && age >= 0 && age <= RECENT_DAYS;
  const alerting = recent && !isSeen(row.sec_filing_url);

  const dot = recent ? (
    <span
      className={
        alerting
          ? "inline-block h-2 w-2 animate-pulse rounded-full bg-sky-400"
          : "inline-block h-1.5 w-1.5 rounded-full bg-slate-500"
      }
      aria-hidden
    />
  ) : null;

  return (
    <HoverScore
      label={String(row.sec_significance)}
      colorClass={significanceClass(row.sec_significance_direction, row.sec_significance)}
      note={row.sec_significance_note}
      href={row.sec_filing_url}
      adornment={dot}
      onActivate={() => markSeen(row.sec_filing_url!)}
    />
  );
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
    key: "gamma_magnet",
    label: "Gamma Magnet",
    numeric: true,
    width: 92,
    render: (r) => fmtLast(r.gamma_magnet),
    cellClassName: () => "text-slate-300",
    title: () => "Strike with the largest-magnitude net gamma (the strongest pin)",
  },
  {
    key: "dilution_risk",
    label: "Dilution Risk",
    numeric: true,
    width: 92,
    render: (r) => <DilutionCell row={r} />,
    cellClassName: (r) => dilutionClass(r.dilution_risk),
  },
  {
    key: "sec_significance",
    label: "SEC",
    numeric: true,
    width: 84,
    render: (r) => <SecCell row={r} />,
    cellClassName: (r) => significanceClass(r.sec_significance_direction, r.sec_significance),
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
