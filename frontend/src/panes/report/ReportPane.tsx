import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generateReport, getReports, getReportStatus } from "./api";
import ReportBody from "./ReportBody";

export default function ReportPane() {
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);
  const wasRunning = useRef(false);

  const reportsQuery = useQuery({
    queryKey: ["report"],
    queryFn: getReports,
  });

  const statusQuery = useQuery({
    queryKey: ["report-status"],
    queryFn: getReportStatus,
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
      void queryClient.invalidateQueries({ queryKey: ["report"] });
    }
  }, [statusQuery.data?.state, queryClient]);

  const generateMutation = useMutation({
    mutationFn: generateReport,
    onSuccess: (status) => {
      setActionError(null);
      queryClient.setQueryData(["report-status"], status);
      void queryClient.invalidateQueries({ queryKey: ["report-status"] });
    },
    onError: (err: Error) => setActionError(err.message),
  });

  const running = statusQuery.data?.state === "running" || generateMutation.isPending;
  const status = statusQuery.data;
  const reports = reportsQuery.data ?? [];
  const latest = reports[0];
  const missingKey = status && (!status.has_uw_key || !status.has_llm_key);

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => generateMutation.mutate()}
          disabled={running}
          className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {running ? "Generating…" : "Generate"}
        </button>
        <p className="text-sm text-slate-400">
          {running && status?.detail
            ? status.detail
            : status?.as_of
              ? `As of ${status.as_of}`
              : "No report yet"}
          {!running && latest != null && ` · ${latest.model}`}
        </p>
      </div>

      {actionError && <p className="mb-3 text-sm text-rose-400">{actionError}</p>}
      {status?.state === "error" && status.detail && (
        <p className="mb-3 text-sm text-rose-400">{status.detail}</p>
      )}
      {missingKey && (
        <p className="mb-3 text-sm text-amber-400">
          Set{" "}
          <code className="text-amber-200">TF_UW_API_KEY</code> and{" "}
          <code className="text-amber-200">TF_ANTHROPIC_API_KEY</code> in{" "}
          <code className="text-amber-200">backend/.env</code> to generate the daily report.
        </p>
      )}

      {reportsQuery.isLoading ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : reportsQuery.isError ? (
        <p className="text-sm text-rose-400">
          {(reportsQuery.error as Error)?.message ?? "Failed to load reports."}
        </p>
      ) : latest == null ? (
        <p className="text-sm text-slate-400">
          No report yet. Generate one after the close to pull the Unusual Whales digest.
        </p>
      ) : (
        <article className="max-w-2xl">
          <div className="mb-4 flex flex-wrap items-baseline gap-3">
            <h2 className="text-xs font-medium uppercase tracking-wide text-slate-500">
              {latest.session_date}
            </h2>
            <a
              href={`/api/report/${latest.session_date}/raw`}
              target="_blank"
              rel="noreferrer"
              className="text-xs text-slate-400 hover:text-slate-200"
            >
              Source JSON
            </a>
          </div>
          <ReportBody analysis={latest.analysis} />
        </article>
      )}
    </div>
  );
}
