import type { WatchlistRow } from "../../api";
import { REMOVE_COL_WIDTH, type Column } from "./columns";
import type { SortDir, SortKey } from "./sort";

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

export default function WatchlistTable({
  rows,
  visibleColumns,
  sortKey,
  sortDir,
  isLoading,
  isError,
  error,
  onSort,
  onRemove,
}: {
  rows: WatchlistRow[];
  visibleColumns: Column[];
  sortKey: SortKey;
  sortDir: SortDir;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  onSort: (key: SortKey) => void;
  onRemove: (symbol: string) => void;
}) {
  const totalWeight = visibleColumns.reduce((sum, c) => sum + c.width, 0) + REMOVE_COL_WIDTH;
  const colPct = (weight: number) => `${(weight / totalWeight) * 100}%`;
  const emptySpan = visibleColumns.length + 1;

  return (
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
                {error?.message ?? "Failed to load watchlist."}
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
                  onClick={() => onRemove(row.symbol)}
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
  );
}
