import type { BreadthDay } from "./api";
import { ALL_COLUMNS, type SortDir, type SortKey } from "./columns";

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

export default function BreadthTable({
  rows,
  sortKey,
  sortDir,
  isLoading,
  isError,
  error,
  emptyMessage,
  onSort,
}: {
  rows: BreadthDay[];
  sortKey: SortKey;
  sortDir: SortDir;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  emptyMessage: string;
  onSort: (key: SortKey) => void;
}) {
  const totalWeight = ALL_COLUMNS.reduce((sum, c) => sum + c.width, 0);
  const colPct = (weight: number) => `${(weight / totalWeight) * 100}%`;

  return (
    <div className="overflow-hidden rounded-lg border border-slate-800">
      <table className="w-full table-fixed text-left text-sm">
        <colgroup>
          {ALL_COLUMNS.map((col) => (
            <col key={col.key} style={{ width: colPct(col.width) }} />
          ))}
        </colgroup>
        <thead className="bg-slate-900 text-slate-400">
          <tr>
            {ALL_COLUMNS.map((col) => (
              <th
                key={col.key}
                onClick={() => onSort(col.key)}
                className="cursor-pointer select-none px-3 py-2 font-medium hover:text-slate-200"
              >
                <span className={`flex items-center ${col.key === "session_date" ? "justify-start" : "justify-end"}`}>
                  <span className="truncate">{col.label}</span>
                  <SortIcon state={sortKey === col.key ? sortDir : "none"} />
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {isLoading && (
            <tr>
              <td colSpan={ALL_COLUMNS.length} className="px-3 py-8 text-center text-slate-500">
                Loading…
              </td>
            </tr>
          )}
          {isError && (
            <tr>
              <td colSpan={ALL_COLUMNS.length} className="px-3 py-8 text-center text-rose-400">
                {error?.message ?? "Failed to load breadth."}
              </td>
            </tr>
          )}
          {!isLoading && !isError && rows.length === 0 && (
            <tr>
              <td colSpan={ALL_COLUMNS.length} className="px-3 py-8 text-center text-slate-500">
                {emptyMessage}
              </td>
            </tr>
          )}
          {rows.map((row) => (
            <tr key={row.session_date} className="hover:bg-slate-900/50">
              {ALL_COLUMNS.map((col) => (
                <td
                  key={col.key}
                  className={`px-3 py-2 tabular-nums whitespace-nowrap ${
                    col.key === "session_date" ? "text-left" : "text-right"
                  } ${col.cellClassName?.(row) ?? "text-slate-300"}`}
                >
                  {col.format ? col.format(row) : String(row[col.key] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
