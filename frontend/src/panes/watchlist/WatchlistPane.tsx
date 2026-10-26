import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { addSymbol, getWatchlist, removeSymbol, type WatchlistRow } from "../../api";
import ColumnPicker from "./ColumnPicker";
import WatchlistTable from "./WatchlistTable";
import { compare, type SortDir, type SortKey } from "./sort";
import { useColumnVisibility } from "./useColumnVisibility";

export default function WatchlistPane() {
  const queryClient = useQueryClient();
  const [symbolInput, setSymbolInput] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("symbol");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [formError, setFormError] = useState<string | null>(null);
  const { visibleKeys, visibleColumns, toggleColumn } = useColumnVisibility();

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
    onMutate: async (symbol) => {
      await queryClient.cancelQueries({ queryKey: ["watchlist"] });
      const previous = queryClient.getQueryData<WatchlistRow[]>(["watchlist"]);
      const upper = symbol.trim().toUpperCase();
      queryClient.setQueryData<WatchlistRow[]>(["watchlist"], (old) =>
        old?.filter((r) => r.symbol !== upper) ?? [],
      );
      return { previous };
    },
    onError: (_err, _symbol, context) => {
      if (context?.previous !== undefined) {
        queryClient.setQueryData(["watchlist"], context.previous);
      }
    },
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

      <WatchlistTable
        rows={rows}
        visibleColumns={visibleColumns}
        sortKey={sortKey}
        sortDir={sortDir}
        isLoading={isLoading}
        isError={isError}
        error={isError ? (error as Error) : null}
        onSort={onSort}
        onRemove={(symbol) => removeMutation.mutate(symbol)}
      />
    </div>
  );
}
