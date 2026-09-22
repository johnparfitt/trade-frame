import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getBreadth, getBreadthStatus, refreshBreadth } from "./api";
import BreadthTable from "./BreadthTable";
import { compare, type SortDir, type SortKey } from "./columns";

export default function BreadthPane() {
  const queryClient = useQueryClient();
  const [sortKey, setSortKey] = useState<SortKey>("session_date");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [actionError, setActionError] = useState<string | null>(null);
  const wasRunning = useRef(false);

  const daysQuery = useQuery({
    queryKey: ["breadth"],
    queryFn: getBreadth,
  });

  const statusQuery = useQuery({
    queryKey: ["breadth-status"],
    queryFn: getBreadthStatus,
    refetchInterval: (query) => (query.state.data?.state === "running" ? 2000 : false),
  });

  useEffect(() => {
    const state = statusQuery.data?.state;
    if (state === "running") {
      wasRunning.current = true;
      return;
    }
    if (wasRunning.current && (state === "idle" || state === "error")) {
      wasRunning.current = false;
      void queryClient.invalidateQueries({ queryKey: ["breadth"] });
    }
  }, [statusQuery.data?.state, queryClient]);

  const refreshMutation = useMutation({
    mutationFn: refreshBreadth,
    onSuccess: (status) => {
      setActionError(null);
      queryClient.setQueryData(["breadth-status"], status);
      void queryClient.invalidateQueries({ queryKey: ["breadth-status"] });
    },
    onError: (err: Error) => setActionError(err.message),
  });

  const running = statusQuery.data?.state === "running" || refreshMutation.isPending;

  const rows = useMemo(() => {
    const list = [...(daysQuery.data ?? [])];
    list.sort((a, b) => compare(a, b, sortKey, sortDir));
    return list;
  }, [daysQuery.data, sortKey, sortDir]);

  function onSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const status = statusQuery.data;
  const progress =
    status && status.progress.total > 0
      ? `${status.progress.done} / ${status.progress.total}`
      : null;

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => refreshMutation.mutate()}
          disabled={running}
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "Refreshing…" : "Refresh"}
        </button>
        <p className="text-sm text-slate-400">
          {running && status?.detail
            ? status.detail
            : status?.as_of
              ? `As of ${status.as_of}`
              : "No EOD snapshot yet"}
          {!running && rows[0] != null && ` · universe ${rows[0].universe_size.toLocaleString()}`}
          {running && progress && ` · ${progress}`}
        </p>
      </div>

      {actionError && <p className="mb-3 text-sm text-rose-400">{actionError}</p>}
      {status?.state === "error" && status.detail && (
        <p className="mb-3 text-sm text-rose-400">{status.detail}</p>
      )}
      {status && !status.has_api_key && (
        <p className="mb-3 text-sm text-amber-400">
          Set <code className="text-amber-200">TF_MASSIVE_API_KEY</code> in{" "}
          <code className="text-amber-200">backend/.env</code> to pull EOD breadth.
        </p>
      )}

      <BreadthTable
        rows={rows}
        sortKey={sortKey}
        sortDir={sortDir}
        isLoading={daysQuery.isLoading}
        isError={daysQuery.isError}
        error={daysQuery.isError ? (daysQuery.error as Error) : null}
        emptyMessage="No breadth history yet. Refresh after the close to pull Massive EOD bars."
        onSort={onSort}
      />
    </div>
  );
}
