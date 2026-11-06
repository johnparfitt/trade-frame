import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  applySecReview,
  secBrief,
  secReview,
  type SecFiling,
  type SecReview,
  type WatchlistRow,
} from "../../api";
import type { SortKey } from "./sort";
import { useDismissedFilings, useSeenFilings } from "./useSeenFilings";

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

// SEC cell: blank unless a filing cleared the significance bar. Shows the newest
// score (color-coded) and a pulsing dot while any filing from the last 7 days
// is unseen. Clicking the score opens the list and does not call the model.
// Review, inside that list, sends one filing for a second read. Mark as read
// (and a successful Analyze together) marks every filing in the cell seen and
// removes the dot. Remove hides those filings from the column until a new one
// arrives.
function SecCell({ row }: { row: WatchlistRow }) {
  const { isSeen, markSeen, markSeenMany } = useSeenFilings();
  const { isDismissed, dismissMany } = useDismissedFilings();
  const filings = row.sec_filings ?? [];
  if (row.sec_significance == null || !row.sec_filing_url) return <>—</>;

  const listed = filings.length > 0 ? filings : [loneFiling(row)];
  if (isDismissed(row.sec_filing_url) || listed.every((filing) => isDismissed(filing.url))) {
    return <span className="text-slate-500">—</span>;
  }

  function markRowRead() {
    markSeenMany([row.sec_filing_url!, ...listed.map((filing) => filing.url)]);
  }

  function removeFilings() {
    dismissMany([row.sec_filing_url!, ...listed.map((filing) => filing.url)]);
  }

  const alerting = listed.some((filing) => {
    const age = daysSinceFiling(filing.date);
    return age != null && age >= 0 && age <= RECENT_DAYS && !isSeen(filing.url);
  });

  const dot = alerting ? (
    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-sky-400" aria-hidden />
  ) : null;

  return (
    <SecFilingsMenu
      row={row}
      filings={listed}
      score={row.sec_significance}
      note={row.sec_significance_note}
      adornment={dot}
      markSeen={markSeen}
      markRowRead={markRowRead}
      onRemove={removeFilings}
    />
  );
}

function SecFilingsMenu({
  row,
  filings,
  score,
  note,
  adornment,
  markSeen,
  markRowRead,
  onRemove,
}: {
  row: WatchlistRow;
  filings: SecFiling[];
  score: number;
  note: string | null;
  adornment?: ReactNode;
  markSeen: (id: string) => void;
  markRowRead: () => void;
  onRemove: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ x: number; y: number; above: boolean } | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number } | null>(null);
  const [brief, setBrief] = useState<string | null>(null);
  const [briefError, setBriefError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const colorClass = significanceClass(row.sec_significance_direction, score);
  const noteLines = (note ?? "").split("\n").filter(Boolean);

  useEffect(() => {
    if (!open) return;
    function onPointer(event: MouseEvent) {
      const target = event.target as Node;
      if (buttonRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function showTip(event: React.MouseEvent<HTMLButtonElement>) {
    if (open) return;
    const rect = event.currentTarget.getBoundingClientRect();
    setTip({ x: Math.min(rect.right, window.innerWidth - 8), y: rect.bottom + 4 });
  }

  function toggle() {
    setTip(null);
    const el = buttonRef.current;
    if (el) {
      const rect = el.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      const above = spaceBelow < 320 && rect.top > spaceBelow;
      setPos({
        x: Math.min(rect.right, window.innerWidth - 8),
        y: above ? rect.top - 4 : rect.bottom + 4,
        above,
      });
    }
    setOpen((value) => !value);
  }

  async function analyze() {
    setBusy(true);
    setBriefError(null);
    try {
      const result = await secBrief(row.symbol);
      setBrief(result.text);
      markRowRead();
    } catch (err) {
      setBriefError(err instanceof Error ? err.message : "Analysis failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="inline-flex items-center justify-end gap-1 hover:underline"
        aria-label="Significant SEC filings"
        aria-expanded={open}
        onClick={toggle}
        onMouseEnter={showTip}
        onMouseMove={showTip}
        onMouseLeave={() => setTip(null)}
      >
        {adornment}
        {score}
      </button>
      {!open &&
        tip &&
        noteLines.length > 0 &&
        createPortal(
          <div
            className="pointer-events-none fixed z-50 w-max max-w-xl -translate-x-full rounded-md border border-slate-700 bg-slate-900/95 px-3 py-2 text-left text-xs leading-relaxed shadow-xl"
            style={{ left: tip.x, top: tip.y }}
          >
            {noteLines.map((line, i) =>
              i === 0 ? (
                <div key={i} className={`mb-1 font-semibold ${colorClass}`}>
                  {line}
                </div>
              ) : (
                <div key={i} className="text-slate-300">
                  {line}
                </div>
              ),
            )}
          </div>,
          document.body,
        )}
      {open &&
        pos &&
        createPortal(
          <div
            ref={panelRef}
            className="fixed z-50 flex max-h-[min(24rem,70vh)] w-max max-w-md -translate-x-full flex-col rounded-md border border-slate-700 bg-slate-900/95 text-left text-xs leading-relaxed shadow-xl"
            style={
              pos.above
                ? { left: pos.x, bottom: window.innerHeight - pos.y }
                : { left: pos.x, top: pos.y }
            }
          >
            <div className="min-h-0 overflow-y-auto px-3 py-2">
              <div className="mb-1 font-semibold text-slate-200">Significant filings, 30 days</div>
              <ul className="space-y-2">
                {filings.map((filing) => (
                  <li key={filing.url}>
                    <a
                      href={filing.url}
                      target="_blank"
                      rel="noreferrer"
                      className="hover:underline"
                      onClick={() => markSeen(filing.url)}
                    >
                      <span className={significanceClass(filing.direction, filing.score)}>
                        {filing.score}
                      </span>
                      <span className="text-slate-400">
                        {" "}
                        {filing.form} · {filing.date}
                      </span>
                    </a>
                    <div className="text-slate-300">{filing.reason}</div>
                    <FilingReview symbol={row.symbol} filing={filing} />
                  </li>
                ))}
              </ul>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-slate-800 px-3 py-2">
              {filings.length > 1 &&
                (brief ? (
                  <p className="w-full whitespace-pre-wrap text-slate-200">{brief}</p>
                ) : (
                  <button
                    type="button"
                    className="rounded border border-slate-600 px-2 py-1 text-slate-200 hover:bg-slate-800 disabled:opacity-50"
                    disabled={busy}
                    onClick={analyze}
                  >
                    {busy ? "Analyzing…" : "Analyze together"}
                  </button>
                ))}
              <button
                type="button"
                className="rounded border border-slate-600 px-2 py-1 text-slate-200 hover:bg-slate-800"
                onMouseDown={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  markRowRead();
                  setOpen(false);
                }}
              >
                Mark as read
              </button>
              <button
                type="button"
                className="rounded border border-rose-900 px-2 py-1 text-rose-300 hover:bg-slate-800"
                onMouseDown={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  onRemove();
                  setOpen(false);
                }}
              >
                Remove
              </button>
              {briefError && <p className="w-full text-amber-400">{briefError}</p>}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}

function loneFiling(row: WatchlistRow): SecFiling {
  return {
    score: row.sec_significance ?? 0,
    direction: row.sec_significance_direction ?? "neutral",
    reason: row.sec_significance_note ?? "",
    form: row.sec_filing_form ?? "",
    date: row.sec_filing_date ?? "",
    url: row.sec_filing_url ?? "",
  };
}

function useFilingReview(symbol: string, url: string) {
  const queryClient = useQueryClient();
  const [review, setReview] = useState<SecReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);

  async function run() {
    if (busy) return;
    setBusy(true);
    setError(null);
    setReview(null);
    try {
      setReview(await secReview(symbol, url));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Review failed");
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (!review) return;
    setApplying(true);
    setError(null);
    try {
      await applySecReview(symbol, {
        url,
        score: review.score,
        direction: review.direction,
        reason: review.reason,
      });
      await queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not replace the score");
      setApplying(false);
    }
  }

  return {
    review,
    error,
    busy,
    applying,
    run,
    apply,
    keep() {
      setReview(null);
      setError(null);
    },
  };
}

function FilingReviewState({ state }: { state: ReturnType<typeof useFilingReview> }) {
  const { review, error, busy, applying } = state;
  if (review) {
    return (
      <div className="mt-1 space-y-1">
        <p>
          <span className="text-slate-400">{review.agrees ? "Agrees. " : "Disagrees. "}</span>
          <span className={significanceClass(review.direction, review.score)}>{review.score}</span>
          <span className="text-slate-400"> {review.direction}</span>
        </p>
        <p className="text-slate-200">{review.reason}</p>
        <p className="text-slate-300">Replace the stored score and reason with this?</p>
        {!review.will_show && (
          <p className="text-amber-400">
            This score is at or below the cutoff, so the filing leaves the column.
          </p>
        )}
        <div className="flex gap-2">
          <button
            type="button"
            className="rounded border border-slate-600 px-2 py-1 text-slate-200 hover:bg-slate-800 disabled:opacity-50"
            disabled={applying}
            onClick={state.apply}
          >
            {applying ? "Replacing…" : "Replace"}
          </button>
          <button
            type="button"
            className="rounded border border-slate-600 px-2 py-1 text-slate-200 hover:bg-slate-800 disabled:opacity-50"
            disabled={applying}
            onClick={state.keep}
          >
            Keep current
          </button>
        </div>
        {error && <p className="text-amber-400">{error}</p>}
      </div>
    );
  }

  return (
    <div className="mt-1">
      {busy && <p className="text-slate-400">Reviewing…</p>}
      {error && <p className="text-amber-400">{error}</p>}
      {!busy && error && (
        <button
          type="button"
          className="mt-1 rounded border border-slate-600 px-2 py-1 text-slate-200 hover:bg-slate-800"
          onClick={state.run}
        >
          Try again
        </button>
      )}
    </div>
  );
}

function FilingReview({ symbol, filing }: { symbol: string; filing: SecFiling }) {
  const state = useFilingReview(symbol, filing.url);
  if (state.review || state.busy || state.error) {
    return <FilingReviewState state={state} />;
  }
  return (
    <div className="mt-1">
      <button
        type="button"
        className="rounded border border-slate-600 px-2 py-1 text-slate-200 hover:bg-slate-800"
        onClick={state.run}
      >
        Review
      </button>
    </div>
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
    key: "next_earnings",
    label: "Earnings",
    numeric: true,
    width: 92,
    render: (r) => fmtEarnings(r.next_earnings).label,
    cellClassName: (r) => (fmtEarnings(r.next_earnings).soon ? "text-amber-400" : "text-slate-300"),
  },
  {
    key: "sec_significance",
    label: "SEC",
    numeric: true,
    width: 72,
    render: (r) => <SecCell row={r} />,
    cellClassName: (r) => significanceClass(r.sec_significance_direction, r.sec_significance),
  },
];

export const ALL_KEYS = ALL_COLUMNS.map((c) => c.key);
